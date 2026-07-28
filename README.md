# Farever+ Party Meter (memory-reading edition)

A party/raid damage meter for **Farever** that reads the game's own combat
functions in memory via Frida — giving **real spell IDs, damage elements,
crits, and kills** with a live comparison across all nearby players, instead of
scraping the network protocol.

Made by Brudr. This is the successor to the network-traffic meter (`FareverMeter`),
which broke when a patch changed the wire format. Reading the game's typed
objects is both richer (per-spell breakdowns) and more patch-robust (we hook by
symbol name, then recompute offsets from the shipped bytecode).

## How it works

Farever is a **HashLink JIT** game: `Farever.exe` runs `hlboot.dat` (bytecode,
*with debug symbols*) via `libhl.dll`. That lets us:

1. **Parse `hlboot.dat`** (`hltools/hlbc_parser.py`) to get every class's fields
   and every method's function index — no guessing byte offsets.
2. **Resolve functions at runtime** (`frida/*.js`): find the HL `functions_ptrs`
   table and map any function index → live JIT address. The table is located by
   a pointer walk from libhl's own statics (its loaded-modules registry reaches
   `hl_module.functions_ptrs` in ≤2 hops — sub-second, no layout assumptions:
   candidates are verified against known hdll native exports at their expected
   indices). If the walk misses, a full anchored memory scan runs as fallback,
   with progress heartbeats so the host waits instead of assuming a dead hook.
3. **Hook `ent.Unit.onInflictDamage`** and read the `st.skill.DamageResult`
   argument: `_amount`, `affinity` (element), `_critical`, `_kill`, and
   `baseSkill.kind` (the spell ID). Filter dealers to `ent.Hero` (players) and
   tag each hit with the player's name; the local player is found via
   `ui.Console.getMyHero()`. **Healing** works differently: heals are computed
   server-side only (`receiveHeal`/`computeHeal` never run on clients), so the
   meter reads what *is* replicated — a rise in a unit's health attribute
   (`ent.UnitAttributes.set_health`) is the effective heal amount, and the heal
   FX played on the target (`ent.Unit.playHitHealFX`) names the healing skill
   and its owner. Each health rise is attributed to the most recent heal FX on
   that unit; FX-less in-combat rises count as "Regen" (self), and
   out-of-combat regen / spawn replication is dropped.
4. **Follow the game's own UI.** `ui.BaseUI.displayWindow(ui, win)` and
   `removeWindow(ui, win)` are the game's window manager — every window (escape
   menu, inventory, map, ...) passes through them with the window instance as
   the second argument. Hooking the pair and reading each instance's runtime
   type name gives a live "which game windows are open" feed, which the overlay
   uses to unlock itself while the escape menu is up. (`displayWindow` fires
   twice per open, so instances are tracked by pointer and a class is only
   reported when its live count crosses zero; `ui.win.BaseWindow.onRemove` is
   the safety net for windows torn down without going through `removeWindow`,
   and a zone change clears the whole set.)
5. **Resolve display names** from the game's CastleDB: `baseSkill.inf` is the
   CDB skill row; its `texts.name` is the localized name (e.g. `Warrior_Rage_Strike`
   → "Raging Smash"). Read via libhl's `hl_obj_get_field` + `hl_hash_utf8`,
   cached per skill id.

## Requirements

```
pip install frida
```

Python 3.10+. Windows. Farever must be running and logged in.

## Run

```
python meter/farever_meter.py
```

If Farever isn't running yet, the meter waits for it to launch. If several
Farever processes are running it asks which one to attach to, and if it can't
find your `hlboot.dat` it asks for your install folder.

Two overlay windows appear top-right:

* **Meter** — one line per player, sorted by damage done, with damage / DPS /
  % / healing-done columns; your row is marked `*`. Under each line sit two
  bars: **blue = damage**, **green = healing**, each scaled against the
  biggest damage/healing number currently on the meter.
* **Breakdown** — the inspected player's (default: you) per-skill damage and
  per-skill healing side by side, each ordered greatest → least with a bar
  under every row (blue for damage, green for healing), plus per-element
  totals.

## Controls

There are no hotkeys to memorise — **everything lives behind the game's own
escape menu.** Press `Esc` and the meter notices (the hook watches the game's
window manager, `ui.BaseUI.displayWindow` / `removeWindow`), so the overlay wakes
up exactly when the game has already freed the mouse cursor. Nothing ever pops a
hidden cursor mid-fight.

While the escape menu is open:

* both windows **unlock** — drag either by its header, and a click on a player
  row points the breakdown at them;
* a **control menu** appears in the middle of the game window with the settings
  that used to be hotkeys. Drag it anywhere; its position is remembered like the
  other two windows';
* the one surviving keybind is spelled out as floating text at the top of the
  screen.

Close the escape menu and everything goes back to click-through.

### Control menu

| Button | Does |
|---|---|
| Hide / Show meter windows | hides both overlay windows (the control menu stays, so you can bring them back) |
| Show all players / Show party only | switches between your group and everyone nearby; resets the encounter |
| Reset window positions | snaps all three windows back to their defaults and clears the saved positions |

### The only hotkey

| Key | Action |
|---|---|
| `Shift+\` | reset the current encounter (breakdown snaps back to you) |

It fires while Farever has focus, with a global `RegisterHotKey` fallback if the
low-level hook is blocked.

### Also available any time

**Ctrl+click a player's line on the meter** points the breakdown at them without
opening anything — a global mouse hook hit-tests the click, so it works while the
overlay is locked and click-through. The breakdown snaps back to *you* on
encounter reset, zone change, and party/all mode switches.

**By default the meter shows only your party** (read from your group's roster in
memory); the control menu switches it to all nearby players.

## After a Farever patch

Function indices and field offsets shift between builds, so the two data files
(`analysis_out/resolver_data.json`, `analysis_out/meter_offsets.json`) go stale.
**The meter self-heals:** on every launch it regenerates the data from the
`hlboot.dat` sitting next to the *running* game's exe (so a multi-install
mismatch is impossible), skipping the reparse when the file hasn't changed
since last time. If the hook still can't find the function table, it
regenerates once more and retries — no action needed.

To regenerate manually (e.g. setting up on another PC before first run):

```
py hltools\build_targets.py     # -> resolver_data.json  (findexes, anchors, map fn)
py hltools\emit_offsets.py      # -> meter_offsets.json   (field byte-offsets)
```

Both auto-detect Farever's `hlboot.dat` across common Steam library paths; pass
the path explicitly if needed: `py hltools\build_targets.py "D:\...\Farever\hlboot.dat"`.
Neither needs the game running or frida — they just parse the `.dat`. Names are
stable across patches, so regenerating by name recovers the new indices/offsets.
(`hltools/find_damage.py` re-locates the damage fn/types if a method were renamed.)

## Window positions

All three windows — meter, breakdown and control menu — remember where you drag
them (`.meter_position.json`; the old single-window format is still read for the
meter). If a saved position lands off-screen — e.g. the file was copied from a
machine with a different monitor layout — that window snaps back to its default
spot instead of hiding. **Reset window positions** in the control menu clears the
cache and restores all three defaults.

Defaults are: meter top-right, breakdown below it, control menu centred on the
*game's* window (found by enumerating the process's windows) rather than on
whichever monitor Windows calls primary — so on a multi-monitor setup it opens
where you're actually looking. The floating reset hint is centred the same way
and is not draggable or saved.

## Layout

```
hltools/     self-contained HashLink bytecode parser + analysis scripts
frida/       Frida scripts: resolver, probes, and the persistent meter hook
meter/       the meter app (aggregation + Tk overlay + Frida host)
analysis_out/ generated: resolver_data.json, meter_offsets.json, reports
```

## Capture timing

The duration clock (and therefore DPS) only advances while at least one
*captured* player is actually in combat, read from the game's `ent.Unit.isInCombat`
flag. The hook reports each seen player's combat state ~2.5×/sec; the meter
sums "active" seconds only. In party mode the clock runs while any group member
is in combat; in all-players mode while anyone captured is. Brief lulls pause
the clock (so DPS reflects active fighting), and a hit after a long lull starts
a fresh encounter. Heals are recorded but never drive encounter boundaries —
an out-of-combat potion or regen tick won't roll the meter into a new
encounter (the next *damage* hit does that).

## Party filtering

Party membership is read from the local player's group roster:
`Hero.player.group.players` → the list of member `st.Player` names. (The group's
numeric `groupId` reads 0 and is unreliable, so we match by roster name.) The
roster is refreshed every few seconds, so joining/leaving a group is picked up
live. Solo (no group) shows just you in party mode.

## Known limitations / next steps

- The window feed is generic (every `ui.win.*` class reports itself), but only
  `ui.win.EscapeMenu` currently wakes the overlay — add more class names to
  `UNLOCK_ON_WINDOWS` in `meter/farever_meter.py` to extend it (inventory, map,
  ...).
- `Shift+\` is the last keybind. It survives because a reset is wanted *mid-fight*,
  which is exactly when the escape menu isn't an option.
- Summon/pet damage (non-`ent.Hero` dealers) is not yet attributed to its owner.
- Healing is *effective* healing (health actually restored) — overhealing is
  invisible to clients and therefore not counted.
- Heal attribution matches each health rise to the latest heal FX on that
  target within 1.5s; two HoTs landing the same tick merge into whichever FX
  came last.
- Party matching is by player name (fine unless two nearby players share a name).
- Skills the CDB has no display name for fall back to the prettified id
  (underscores → spaces). All base attacks share the CDB name "Attack".
- DPS is computed over the shared encounter window (fair cross-player compare).
- No persistence/logging of past encounters yet.
- Offsets are for the current build; see "After a Farever patch".
