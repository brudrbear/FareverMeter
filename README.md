# Farever+ Party Meter (memory-reading edition)

> ## 📥 Getting it
>
> Open **[Releases](../../releases)** and download the latest version, then
> **extract it anywhere on your PC** — Desktop, Downloads, wherever. It does
> **not** need to go in your Farever folder, and nothing in the game is touched
> or modified.
>
> Then see [Requirements](#requirements) — there's one Python checkbox that
> catches almost everyone.

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

Python 3.10+. Windows. Farever must be running and logged in.

> ### ⚠️ When installing Python, tick **"Add python.exe to PATH"**
>
> It's the checkbox at the **bottom of the first installer screen**, and it is
> **off by default**. Miss it and `python` won't work — not because Python is
> broken, but because Windows ships zero-byte stubs called `python.exe` and
> `python3.exe` that open the Microsoft Store instead. That's what "I installed
> Python and it still doesn't work" almost always turns out to be.
>
> **Already installed it without ticking?** Re-run the same installer →
> **Modify** → Next → tick **"Add Python to environment variables"** → Install.
> Nothing is reinstalled and no reboot is needed — just open a *new* terminal
> afterwards, since an already-open one keeps the old PATH.

Then install the one dependency:

```
pip install frida
```

## Run

```
python meter/farever_meter.py
```

Double-clicking `meter/farever_meter.py` works too — it goes through whatever
Windows associates with `.py`, which on a machine with one Python install is the
right interpreter. The tradeoff is that a startup failure closes the window
before you can read it, so reach for a terminal when something's wrong.

If Farever isn't running yet, the meter waits for it to launch. If several
Farever processes are running it asks which one to attach to, and if it can't
find your `hlboot.dat` it asks for your install folder.

### If it won't start

| What you see | What it means | Fix |
|---|---|---|
| The Microsoft Store opens, or `Python was not found; run without arguments to install from the Microsoft Store` | `python` is hitting Windows' zero-byte alias stub — PATH was never set | Tick **Add to PATH** (see Requirements), or use `py meter/farever_meter.py`, or just double-click the `.py` |
| `'python' is not recognized as an internal or external command` | Same cause, or the terminal predates the install | Same fix — and open a **new** terminal |
| `ModuleNotFoundError: No module named 'frida'` | Python is fine, the dependency isn't installed | `pip install frida` |
| `[!] permission denied attaching` | Farever is running as administrator | Run the meter from an elevated terminal too |
| `[meter] could not initialise the hook after 3 attempts` | A previous run was force-killed and left a half-attached agent | Fully close Farever, reopen it, then start the meter — and stop it with `Ctrl+C` in future, not by closing the window |

`py` works even when `python` doesn't: the launcher installs to its own folder
and is registered separately. Double-clicking the `.py` sidesteps PATH entirely,
since that goes through the file association.

**Stop it with `Ctrl+C` in the console, not by closing the window.** Closing the
console terminates the process outright, skipping the unload/detach — and a
half-attached agent is what destabilises the game across repeated relaunches.
`Ctrl+C` breaks the overlay's mainloop and takes the normal shutdown path.

Only one meter runs at a time. A new instance finds any other through a lock
file in `%LOCALAPPDATA%\FareverMeter` — deliberately outside the project folder,
so a copy of this script launched from a *different* directory is still found —
and asks it to shut down cleanly first. It force-kills only if that instance
doesn't answer within 12 seconds.

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
* a **control menu** fades in over the middle of the game window with the
  settings that used to be hotkeys. Drag it anywhere; its position is remembered
  like the other two windows';
* anything you'd hidden — with a Show/hide tick, or by out-of-combat hiding —
  **comes back for as long as the menu is open**, so you can see what a checkbox
  does while you're clicking it;
* the one surviving keybind is spelled out as floating text at the top of the
  screen.

Close the escape menu and everything fades back to how you had it, click-through
again.

### Control menu

| Section | Button | Does |
|---|---|---|
| Options | Show all players / Show party only | switches between your group and everyone nearby; resets the encounter |
| Options | Theme | `Dynamic` (default) follows the game — rift colours inside a rift, Farever colours everywhere else. `Farever` and `Rift` pin it either way. |
| Show / hide | Damage meter / Breakdown / Healing columns / Rift timer | hides that piece of the overlay (the control menu stays, so you can bring it back) |
| Show / hide | Hide out of combat | fades both windows away a few seconds after the fighting stops |
| Actions | 60s Parse Mode | see below |
| Actions | Parse Screenshots | opens `parses/` in Explorer (created on the spot if you haven't run one yet) |
| Reset | Reset encounter data (`Shift+\`) | the same reset the hotkey fires — the label carries the keybind because that's the one you want mid-fight, when the escape menu isn't an option |
| Reset | Reset window positions | snaps every window back to its default and clears the saved positions |

### 60s Parse Mode

A fixed-length sample, so two runs are comparable in a way "whatever that pull
happened to be" never is. Click it and:

1. an 8-second countdown appears over the top of the game window — long enough
   to close the escape menu and get your hands back on the keyboard;
2. the meter clears and records for **exactly 60 seconds**, ending on time even
   if you're still swinging;
3. the sample then freezes on screen. New hits are ignored and the duration
   clock stops, so the numbers stay readable for as long as you want them;
4. and the result is written to `parses/parse-YYYYmmdd-HHMMSS.png` — the party
   table and the inspected player's skill breakdown, drawn in the meter's own
   palette. `parses/` is gitignored.

The image is drawn from the numbers rather than screenshotted from the overlay:
the live windows are layered and semi-transparent, the breakdown only ever shows
one player, and a capture would pick up whatever the game had drawn behind them.
It needs Pillow (`pip install pillow`); without it you lose the picture, not the
parse.

The cutoff is enforced on the data path rather than by the UI's 250 ms refresh,
so the window is the length asked for regardless of tick timing. The usual
"quiet for 25 s means a new encounter" rule is suspended inside a parse — a lull
mid-run is part of the sample, not the start of a new one.

**DPS in a parse divides by the full 60 seconds**, not by in-combat time the way
live metering does. The window *is* the measurement, so downtime counts against
you — and the game's `isInCombat` flag drops between pulls, which would
otherwise inflate a parse by however much of it the flag happened to miss (a
real 60 s run measured 27 s of "combat"). Reset back to live metering and DPS
goes back to dividing by combat time.

The button reads **Stop 60s Parse** while a parse is live. Pressing it, or
resetting the data (`Shift+\`, the menu button, or a zone change), returns to
normal live metering. Either route clears the sample: resuming capture into a
finished parse would quietly append live hits to the numbers you were reading.

### The only hotkey

| Key | Action |
|---|---|
| `Shift+\` | reset the current encounter (breakdown snaps back to you) |

It fires while Farever has focus, with a global `RegisterHotKey` fallback if the
low-level hook is blocked.

### Rifts

The hook reads `st.GameLayer.isRift` — reached by a pointer walk from the local
hero (`ent.Hero` inherits `st.State.layer`), so it's plain memory reads on the
heartbeat timer rather than an HL call that would have to ride inside the damage
hook. That matters: you enter a rift well before you hit anything in it.

While you're inside a rift the meter and breakdown **re-skin themselves** into
the rift palette — same widget tree, only the colours swap, so it's safe
mid-combat. That's the `Dynamic` theme; the control menu can pin it to `Farever`
or `Rift` instead. The damage and healing bars keep their blue and green, since those
carry meaning the theme shouldn't overwrite.

Entering one raises a rift-styled prompt in the middle of the game window asking
**"Enable 'View All Players'?"**, and leaving raises the mirror of it —
**"Return to viewing party members only?"** — so the wide view you switched on
for the rift doesn't quietly follow you back outside. While it's up it is the *only* overlay window
on screen — meter, breakdown and control menu all fade out. **Yes** switches to
all-players and resets the encounter (the mode button does the same; party-only
and all-players numbers can't share one encounter without the percentages
lying). **No** just dismisses it. It re-arms when you leave the rift, and closes
itself if the rift ends before you answer.

A **rift countdown** floats separately, styled after the rifts rather than the
meter — drag it anywhere, its position is remembered, and it's toggled under
Show / hide. Rifts open on the hour, so it simply counts down the wall clock; for
the first 6 minutes past the hour it reads "No rift upcoming", since the rift
that just opened is the current one. It hides while you're inside a rift, and
it's the one element out-of-combat hiding leaves alone — a countdown you can
only see mid-fight would be useless.

(The game does carry its own schedule in `GameLayer.worldEvents.currentEvents`
as `st.event.WorldEvent` records with `creationTime` / `startTime` / `stopTime`.
Reading it reported the *running* event rather than the next one, and a pending
rift didn't appear there at all, so the clock is both simpler and more accurate.)

The prompt takes clicks even while the overlay is otherwise click-through. If
the game still owns the cursor when it appears, press `Esc` to free it — the
control menu stays hidden while the prompt is up precisely so `Esc` does nothing
but hand the mouse back.

### Also worth knowing

**Click a player's line on the meter** while the escape menu is open to point the
breakdown at them. It snaps back to *you* on encounter reset, zone change, and
party/all mode switches.

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

- The window feed is generic (every `ui.win.*` class reports itself). Only
  `ui.win.EscapeMenu` *unlocks* the overlay (`UNLOCK_ON_WINDOWS`); every other
  class fades it out for as long as that window is up, so the game's own screens
  are never covered. If some always-on HUD class turns out to report itself as
  open — the names are logged as `[meter] game window ...` — add it to
  `MENU_IGNORE_WINDOWS` in `meter/farever_meter.py` and it stops counting.
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
