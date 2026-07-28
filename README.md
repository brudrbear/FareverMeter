# Farever+ Party Meter (memory-reading edition)

> ### 📥 **[Download the installer](../../releases)** — run it, start the game, done.
>
> There is nothing else to install. Python, Frida and Pillow all live inside it.
> It doesn't go in your Farever folder, and nothing in the game is touched or
> modified.

A party/raid damage meter for **Farever** that reads the game's own combat
functions in memory via Frida — giving **real spell IDs, damage elements,
crits, and kills** with a live comparison across all nearby players, instead of
scraping the network protocol.

Made by Brudr. This is the successor to the network-traffic meter (`FareverMeter`),
which broke when a patch changed the wire format. Reading the game's typed
objects is both richer (per-spell breakdowns) and more patch-robust (we hook by
symbol name, then recompute offsets from the shipped bytecode).

## Setup

Two steps, once. **Everything after "You're all set" is optional reading.**

### Step 1 — Install it

Download **`FareverMeter-x.y-Setup.exe`** from
**[Releases](../../releases)** and run it.

It installs for you alone, inside your own user folder, so there's no
administrator prompt — and nothing goes anywhere near your Farever install.

> ### ⚠️ Windows will warn you, and that's expected
>
> The installer isn't code-signed (a certificate is an annual bill this meter
> doesn't earn), so SmartScreen shows **"Windows protected your PC"**. Click
> **More info** → **Run anyway**.
>
> Antivirus tools sometimes object too, for an honest reason: reading another
> program's memory is exactly what a memory-reading damage meter does, and it's
> also what a lot of malware does. See [If it won't start](#if-it-wont-start).

### Step 2 — Start Farever, then the meter

Log in to your character first, then start **Farever+ Meter** from the Start
menu (or the desktop shortcut, if you asked the installer for one).

**There's no window and no console.** It runs in the background, puts an icon in
the **notification area** by the clock, and draws its overlay over the game.

Start it before the game if you like — it waits for Farever to launch, and you
can still stop it from the tray while it's waiting.

### Starting it with the game, from Steam (optional)

Because the meter waits for Farever, you can let Steam start both at once and
stop thinking about it.

In Steam, right-click **Farever** → **Properties** → **General** → **Launch
Options**, and paste this on one line:

```
cmd /c start "" "C:\Users\YOU\AppData\Local\Programs\FareverMeter\FareverMeter.exe" & %command%
```

Replace `YOU` with your Windows username. To get the path exactly right without
typing it: find **Farever+ Meter** in the Start menu, right-click →
**More** → **Open file location**, then right-click the shortcut → **Properties**
and copy the **Target** box.

Now launching Farever from Steam starts the meter first, then the game.

**Why it's shaped like that.** Steam replaces `%command%` with the game and its
arguments, but on Windows it doesn't run launch options through a shell — so
`cmd /c` is what makes the `&` mean anything. `start ""` launches the meter
*without waiting* for it, and the empty `""` is the window title `start` expects
before a quoted path (leave it out and it treats the path as the title). Steam
then waits on `cmd`, `cmd` waits on the game, so **playtime, the overlay and
Rich Presence all keep working** — which they don't in the recipes that leave
`start` off the game as well.

Two things to know:

* **The meter doesn't close when the game does.** Nothing in it watches for
  Farever exiting, so the overlay stays on screen with the last numbers. Stop it
  from the tray icon when you're finished.
* **Launching Farever outside Steam** — from a desktop shortcut, say — skips the
  launch options entirely, so start the meter yourself that time.

If this feels like more machinery than you want, it is entirely optional: the
meter waits for the game, so starting it whenever you like works just as well.

### Stopping it

Two ways, and both shut down cleanly:

* **right-click the tray icon** by the clock → **Stop the meter**; or
* open the game's **`Esc`** menu and use **Stop the meter** at the bottom of the
  Farever+ control menu (it wants a second click to confirm, so a misclick
  mid-fight doesn't end your session).

> ### 🔎 Windows 11 hides new tray icons
>
> First run, click the **`^`** arrow by the clock and **drag the Farever+ icon
> out** onto the taskbar, so it's there when you want it. The meter pops a
> notification on first run to say so.

**Don't end it from Task Manager.** That kills the process before it can unload
its hook and detach, and a half-attached agent is what destabilises Farever
across repeated relaunches. Both buttons above take the proper path.

### If it won't start

The log is the first place to look — **`%LOCALAPPDATA%\FareverMeter\meter.log`**,
which is also the Start menu's **Farever+ log folder** shortcut. The run before
it is kept as `meter.log.1`.

| What you see | What it means | Fix |
|---|---|---|
| "Windows protected your PC" | The installer is unsigned | **More info** → **Run anyway** |
| Antivirus quarantines or silently deletes it | It reads another process's memory, which is a genuine heuristic hit | Add an exclusion for the install folder, or download it again from [Releases](../../releases) and keep the copy you trust |
| No overlay, no tray icon, nothing at all | It failed during startup | Read `meter.log`; the last lines say where it stopped |
| Overlay appears but stays empty, and you got a "couldn't attach" dialog | A previous run was force-killed and left a half-attached agent | Fully close Farever, reopen it, then start the meter — and stop it from the tray in future |
| "permission denied attaching" in the log | Farever is running as administrator | Right-click the Farever+ shortcut → **Run as administrator** so they match |
| The tray icon isn't there | Windows 11 filed it into the overflow flyout | Click the **`^`** arrow by the clock and drag it out |

### Updating

On startup the meter asks GitHub whether there's a newer version. If there is,
the top of the control menu (`Esc` in game) turns into a notice you can click to
open the download page. Nothing installs itself; it just tells you.

It's one request to `api.github.com` at launch and nothing is sent about you.
Set the environment variable `FAREVER_NO_UPDATE_CHECK=1` to turn it off.

---

## ✅ You're all set

**That's the whole setup — everything below is reference.** Read it if you want
to know what a button does or how the thing works; skip it entirely and the
meter still works fine.

---

## What you'll see

Two overlay windows appear top-right:

* **Meter** — one line per player, sorted by damage done, with damage / DPS /
  % / healing-done columns; your row is marked `*`. Under each line sit two
  bars: **blue = damage**, **green = healing**, each scaled against the
  biggest damage/healing number currently on the meter.
* **Breakdown** — the inspected player's (default: you) per-skill damage and
  per-skill healing side by side, each ordered greatest → least with a bar
  under every row (blue for damage, green for healing), plus per-element
  totals.

### Only one at a time

Starting a second copy doesn't give you two meters. A new instance finds any
other through a lock file in `%LOCALAPPDATA%\FareverMeter` — deliberately
outside the project folder, so a copy launched from a *different* directory is
still found — and asks it to shut down cleanly first. It force-kills only if
that instance doesn't answer within 12 seconds.

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
| Options | Minimap | `Rotating` (default) turns the map with the camera, so the top of the map is whatever you're looking at. `Fixed` keeps north up and turns the arrow instead. |
| Options | Map refresh | how often the minimap is updated — `Ultra` (~30/sec), `High` (~16/sec, default), `Medium` (~9/sec), `Low` (~4/sec). Lower costs less CPU in the game; below about 8/sec the dots visibly step. |
| Scaling | Meter / Breakdown / Settings / Minimap | each window sizes independently, so one can be big and another small |
| Show / hide | Damage meter / Breakdown / Rift timer / Minimap | `Show`, `Hide`, or `Show in ESC` — the last keeps it off screen while you play and brings it back with the game's menu |
| Show / hide | Healing columns | columns inside the meter rather than a window, so it stays a tick |
| Show / hide | Hide out of combat | fades both windows away a few seconds after the fighting stops |
| Actions | 60s Parse Mode | see below |
| Actions | Parse Screenshots | opens `parses/` in Explorer (created on the spot if you haven't run one yet) |
| Reset | Reset encounter data (`Shift+\`) | the same reset the hotkey fires — the label carries the keybind because that's the one you want mid-fight, when the escape menu isn't an option |
| Reset | Reset window positions | snaps every window back to its default and clears the saved positions |
| Quit | Stop the meter | shuts down properly — unloads the hook and detaches from the game. Wants a second click to confirm |

Above the buttons sits a single line of text: normally a reminder of how to stop
the meter, replaced by a clickable notice when a [newer version](#updating) is
out. (From a source run it's the `Ctrl+C` warning instead, since that build
still has a console window someone can close.)

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
   palette. See [Where your files live](#where-your-files-live) for where
   `parses/` is; the **Parse Screenshots** button opens it either way.

The image is drawn from the numbers rather than screenshotted from the overlay:
the live windows are layered and semi-transparent, the breakdown only ever shows
one player, and a capture would pick up whatever the game had drawn behind them.
It's drawn with Pillow, which ships inside the installed build — from source
that's `pip install pillow`, and without it you lose the picture, not the parse.

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

### Minimap

A square map of what's around you, drawn from the game's own entity lists:
**players** (group members ringed blue), **mobs**, **chests**, **orbs**,
**obelisks**, **respawn points** and **activities**. Drag it by its header like
the other windows; its position is remembered and it has a Show / hide tick.

`Rotating` (the default) turns the map **with the camera**, so the top of the
map is whatever you're looking at — not where your character happens to be
pointing, which changes constantly as it turns to face things. `Fixed` keeps the
map still and turns the arrow instead. The range is 120 world units from the
centre out.

Other players are drawn as outlined chevrons pointing where they're facing, so
you can read which way a group is heading; group members get a blue ring.

The map turns with the **camera**, and so does the marker in the middle — a
cone and centre line show where you're looking.

The panel is dark on both themes so the markers are the bright thing on it,
which is easier to read at a glance than icons on a pale background.

**Anything on a different floor gets a small caret** above or below it for which
way you'd have to go, and **players and enemies are dimmed** as well — a mob in the tunnel below you is not
the same news as one you can walk to, and on a flat map they'd look identical.
**Enemies more than 60 units up or down are dropped entirely**, since in the
very vertical zones they can't reach you and there are a lot of them. Chests,
obelisks and the rest are kept however far above or below they are, because
those are still somewhere to head for. Pets and summons never appear.

Whenever the game hands the mouse back — **Alt**, or the `Esc` menu —
**hovering a marker rings it in white** and names it in the box under the map,
with its ground distance and how far up or down it is. Players read as
`Name (Party)` or `Name (Player)`, and enemies by their actual name —
`Rice Seedling (Enemy)` — since a name on its own doesn't tell you what
kind of thing you're pointing at. You can drag the map by its header then too.

Only the minimap does this. Freeing the mouse with Alt won't put the control
menu on screen — that still waits for `Esc`.

**Your settings are remembered** — theme, minimap mode and refresh, UI scale,
party/all, and every Show/hide tick. They live in `.meter_settings.json`,
separately from window positions, so **Reset window positions** doesn't wipe
them.

It stays visible out of combat, since telling you what's nearby while you're
travelling is most of the point.

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
the first 6 minutes past the hour there is nothing to count down to, since the
rift that just opened is the current one — so it takes itself off screen rather
than sitting there saying "No rift upcoming" for a tenth of every hour. Open the
escape menu and it comes back, like everything else you've hidden. It also hides
while you're inside a rift, and
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

This works in the installed build too: both generators are bundled inside it,
and it re-runs them by invoking itself in tool mode, writing the refreshed JSON
to your data folder rather than into the install. So a Farever patch does *not*
need a new release of the meter.

To regenerate manually from a source checkout:

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
them (`.meter_position.json`, in your data folder — see [Where your files
live](#where-your-files-live); the old single-window format is still read for
the meter). If a saved position lands off-screen — e.g. the file was copied from a
machine with a different monitor layout — that window snaps back to its default
spot instead of hiding. **Reset window positions** in the control menu clears the
cache and restores all three defaults.

Defaults are: meter top-right, breakdown below it, control menu centred on the
*game's* window (found by enumerating the process's windows) rather than on
whichever monitor Windows calls primary — so on a multi-monitor setup it opens
where you're actually looking. The floating reset hint is centred the same way
and is not draggable or saved.

## Where your files live

The installed build keeps its code and your data apart, because the code is a
read-only bundle that Windows unpacks somewhere different on every launch.

| | Installed build | From source |
|---|---|---|
| Log | `%LOCALAPPDATA%\FareverMeter\meter.log` | the console you ran it in |
| Window positions | `%LOCALAPPDATA%\FareverMeter\.meter_position.json` | `.meter_position.json` in the project |
| Parse screenshots | `%LOCALAPPDATA%\FareverMeter\parses\` | `parses/` in the project |
| Regenerated offsets | `%LOCALAPPDATA%\FareverMeter\analysis_out\` | `analysis_out/` in the project |

Uninstalling leaves `%LOCALAPPDATA%\FareverMeter` alone — your parses and window
positions survive it, and survive upgrades.

## Running from source

You don't need this to *use* the meter; the installer is the supported route.
It's here for hacking on it.

```
pip install frida pillow
python meter/farever_meter.py
```

Behaviour is identical except where a console changes things: output goes to the
terminal instead of a log file, prompts are asked there instead of as dialogs,
and `Ctrl+C` works as a third way to stop it (the control menu's warning line
changes to say so). The tray icon is there either way.

## Building a release

```
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

Draws the icon, freezes the app with PyInstaller, and compiles the Inno Setup
wizard — leaving `dist\FareverMeter-<version>-Setup.exe` to attach to a GitHub
release. One-time prerequisites:

```
py -m pip install pyinstaller pillow frida
winget install JRSoftware.InnoSetup
```

The version comes from the `VERSION` constant in `meter/farever_meter.py` and
nowhere else — the build script reads it for the installer and its filename, and
the update check compares against it. **Tag the repo with the same string when
you publish,** or every user is told they're out of date.

## Layout

```
hltools/     self-contained HashLink bytecode parser + analysis scripts
frida/       Frida scripts: resolver, probes, and the persistent meter hook
meter/       the meter app (aggregation + Tk overlay + Frida host)
packaging/   icon generator, PyInstaller spec, Inno Setup script, build script
assets/      the generated icon (tray, executable, installer)
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

---

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
