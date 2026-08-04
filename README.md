# Farever+

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

<img width="2559" height="1439" alt="image" src="https://github.com/user-attachments/assets/07feedb5-bd3d-45ff-bc8c-52e53dd67cb2" />

<img width="2559" height="1439" alt="image" src="https://github.com/user-attachments/assets/8fcf0175-3526-4406-b910-910201f51f81" />

<img width="2559" height="1439" alt="image" src="https://github.com/user-attachments/assets/f586f709-20fa-4810-a4ba-aedb8a51f3d0" />





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

* **When the game closes, the meter notices.** The overlay disappears and a
  small window asks whether to exit the meter too — one click and both are
  gone, so the pair behaves like a single program.
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

**Closing Farever itself** hides the overlay and pops a small window asking
whether to exit the meter too — one click there and you're done. "Keep it
running" leaves it in the tray instead (note it can't reconnect to a relaunched
game; start a fresh meter for that).

> ### 🔎 Windows 11 hides new tray icons
>
> First run, click the **`^`** arrow by the clock and **drag the Farever+ icon
> out** onto the taskbar, so it's there when you want it. The meter pops a
> notification on first run to say so.

**Alt-tab away and the whole overlay goes with it**, returning when you click
back into the game — so a damage meter isn't floating over your browser. That
includes the control menu: the game's escape menu stays open behind you, so
without this the largest window the overlay has was the one thing left on
screen. The tray icon stays put, which is how you'd stop the meter from out
there anyway.

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

On startup — and again whenever you go through a loading screen — the meter
asks GitHub whether there's a newer version. If there is, the top of the
control menu (`Esc` in game) turns into a notice. Click it once and the meter
updates itself: the overlay steps aside while a small window downloads the new
installer, then the meter closes, installs the update and starts again as the
new version. Your settings and window positions survive — the installer never
touches them.

When it comes back as the new version, a **what's new** window shows you that
release's notes — dismiss it with **Got it** or `Esc`, and it won't come back
until the next update. It only appears after an update you asked for, never on
an ordinary launch.

The version you're running is in the **top right of the control menu header**,
which is the first thing worth knowing when the meter behaves differently from
what the notes describe.

Nothing downloads or installs until you click. If the automatic route isn't
available (offline, or a run from source), the notice opens the releases page
in your browser instead, like it always did.

It's a request to `api.github.com` and nothing is sent about you. Loading
screens only re-ask at most once every 15 minutes, and once a new version has
been found it stops asking altogether — so a long rift session doesn't turn
into a stream of requests. Set the environment variable
`FAREVER_NO_UPDATE_CHECK=1` to turn it off.

---

## ✅ You're all set

**That's the whole setup — everything below is reference.** Read it if you want
to know what a button does or how the thing works; skip it entirely and the
meter still works fine.

---

## What you'll see

Two overlay windows appear top-right:

* **Meter** — one line per player, with columns for name, class (`War`, `Mag`,
  `Pst`, `Rog`), damage, DPS, %, healing done and `OVER` (the share of that
  healing that restored no health), sorted by damage; your row is
  marked `*`. The columns hold their positions whatever a name is written in —
  a player called 双子星 doesn't shove the numbers along. Under each line sit two
  bars: **blue = damage**, **green = healing**, each scaled against the
  biggest damage/healing number currently on the meter. The healing bar is
  split — **green-teal = healing put on themselves**, always the left segment,
  the muted green for everything else — so a self-healer and a party healer read
  apart at a glance.
* **Breakdown** — the inspected player's (default: you) per-skill damage and
  per-skill healing side by side, each ordered greatest → least with a bar
  under every row (blue for damage, green for healing, green-teal for the
  self-healed share of each skill — a heal cast only on yourself is a fully
  green-teal bar), plus
  per-element totals.

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
| Options | Auto 'View All Players' in rifts | presses the button above for you at both rift boundaries — all-players going in, party-only coming out — and retires the [rift prompt](#rifts) that would otherwise ask. Same setting as that prompt's **Do this every time** tick. Off by default. Applies from your next crossing, not the rift you're already in |
| Options | Theme | Five choices — see [Themes](#themes). `Dark Dynamic` is the default. |
| Options | Transparency | how see-through the overlay is, 0-80%. It covers the meter, breakdown, minimap, compass and rift timer — panel, header bar and text together, since window opacity is the only kind Windows offers. The control menu and the rift prompt are exempt: one is what you're reading while you drag the slider, the other is a question that has to be answered |
| Options | Minimap | `Rotating` (default) turns the map with the camera, so the top of the map is whatever you're looking at. `Fixed` keeps north up and turns the arrow instead. |
| Options | Map refresh | how often the minimap is updated — `Ultra` (~30/sec), `High` (~16/sec, default), `Medium` (~9/sec), `Low` (~4/sec). Lower costs less CPU in the game; below about 8/sec the dots visibly step. |
| Options | Enable sounds | the audio cues, off by default. One switch covers all three: a boss fight starting, a boss dying, and a **legendary weapon** landing in your bags. Turning it on plays a sample, so you know the audio path works without having to go and find a boss |
| Options | Volume | how loud those cues are, 0-100%. Live while you drag |
| Scaling | Meter / Breakdown / Settings / Minimap | each window sizes independently, so one can be big and another small |
| Show / hide | Damage meter / Breakdown / Rift timer / Minimap | `Show`, `Hide`, or `Show in ESC` — the last keeps it off screen while you play and brings it back with the game's menu |
| Show / hide | Healing columns | columns inside the meter rather than a window, so it stays a tick |
| Show / hide | Hide out of combat | fades both windows away a few seconds after the fighting stops |
| Controls | Reset data | the keybind for resetting the encounter, `Shift + \` by default. Click it and press the combination you want. It needs a modifier (or an F-key, or a mouse button) — the meter *swallows* what it fires on, so a bare letter would cost you that key in game. **Middle click, Mouse 4 and Mouse 5 can be bound**; left and right never can. Takes effect immediately |
| Controls | Auto reset on boss pull | wipes the encounter at the **start of every boss fight**, so the parse you end up with is that fight and nothing else. It keeps the last few seconds rather than wiping flat — the game's healthbar is what the pull is detected from and it refreshes on a timer, so the opening burst has already landed by the time the meter hears about it. Bosses only: elites raise the same bar, and resetting for every elite on the way to a boss would be useless |
| Compass | Collectibles / Party Members | what the bearing strip carries. Separate from the minimap's ticks below — the two panels answer different questions, and wanting chests on one but not the other is ordinary. Soulstones have no tick and always show |
| Minimap | Collectibles / Players / Enemies / Activities | what the map draws, by category — orbs and chests share one tick, since nobody wants one without the other. Obelisks, respawn points and soulstones have no tick and are always drawn: they're the landmarks you navigate *by*. The compass isn't affected |
| Actions | 60s Parse Mode | see below |
| Actions | Parse Screenshots | opens `parses/` in Explorer (created on the spot if you haven't run one yet) |
| Reset | Reset encounter data (`Shift+\`) | the same reset the hotkey fires — the label carries the keybind because that's the one you want mid-fight, when the escape menu isn't an option |
| Reset | Reset window positions | snaps every window back to its default and clears the saved positions |
| Quit | Stop the meter | shuts down properly — unloads the hook and detaches from the game. Wants a second click to confirm |

Above the buttons sits a single line of text: normally a reminder of how to stop
the meter, replaced by a clickable notice when a [newer version](#updating) is
out.

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
**soulstones**, **obelisks**, **respawn points** and **activities**. Collected
orbs drop off the map, and hovering names the state — a chest you can't open yet
reads `Chest · Locked`. Soulstones are drawn as a magenta shard, matching the
crystal in the world.

**Chests you've opened drop off; shut ones stay.** Worth knowing why that took
two goes: a chest's `stateId` doesn't change when you loot it (it reads `Closed`
either way — that means "shut", not "been here"), so what the meter watches is
the chest's *visual* state, measured by opening one with a probe running.

**Markers settle for a moment before they appear.** The game's entity lists
churn at distance — things arrive for a single frame, or drop out for a second
or two and come back — which showed up as icons blinking on the map and
compass. A marker now waits a quarter-second before it's drawn, and lingers a
couple of seconds after it stops being reported, so what you see holds still.
Enemies are exempt: a mob appearing late is worse than a mob flickering.

Also worth knowing: **the game only keeps a handful of chests loaded at a time**
— four or five, in a zone with 47 of them. So a chest appears on the map as you
get near it, not the moment you enter the zone. If nothing is showing, it's
usually because everything currently loaded is already looted.

It has no title bar, and the hover box under it only appears while the mouse is
free — the rest of the time the panel is just the map. **Drag it by that box**
(which is also the only time dragging worked anyway). Its position is remembered
and it has a Show / hide tick like the other windows.

`Rotating` (the default) turns the map **with the camera**, so the top of the
map is whatever you're looking at — not where your character happens to be
pointing, which changes constantly as it turns to face things. `Fixed` keeps the
map still and turns the arrow instead. The range is 250 world units from the
centre out, on a panel 405px square at 100% — the Minimap scale slider takes it
from there.

Other players are drawn as outlined chevrons pointing where they're facing, so
you can read which way a group is heading; group members get a blue ring.

The map turns with the **camera**, and so does the marker in the middle — a
cone and centre line show where you're looking.

The panel is dark on the `Dark` themes so the markers are the bright thing on
it, which is easier to read at a glance than icons on a pale background; the
`Farever` themes paint it parchment to match the meter and darken the markers to
suit. See [Themes](#themes).

**Anything on a different floor gets a small caret** above or below it for which
way you'd have to go, and **players and enemies are dimmed** as well — a mob in the tunnel below you is not
the same news as one you can walk to, and on a flat map they'd look identical.
**Enemies more than 60 units up or down are dropped entirely**, since in the
very vertical zones they can't reach you and there are a lot of them. Chests,
obelisks and the rest are kept however far above or below they are, because
those are still somewhere to head for. Pets and summons never appear.

Whenever the game hands the mouse back — **Alt**, or the `Esc` menu — the map
and the meter both take the pointer: you can **click a player's row** on the
meter to inspect them, and **hovering a marker rings it in white** and names it in the box under the map,
with its ground distance and how far up or down it is. Players read as
`Name (Party)` or `Name (Player)`, and enemies by their actual name —
`Rice Seedling (Enemy)` — since a name on its own doesn't tell you what
kind of thing you're pointing at. You can drag the map by that box then too.

Only the minimap does this. Freeing the mouse with Alt won't put the control
menu on screen — that still waits for `Esc`.

**Your settings are remembered** — theme, minimap mode and refresh, UI scale,
party/all, and every Show/hide tick. They live in `.meter_settings.json`,
separately from window positions, so **Reset window positions** doesn't wipe
them.

It stays visible out of combat, since telling you what's nearby while you're
travelling is most of the point.

### Compass

A strip of bearings across the top of the view, answering the question the
minimap can't: *which way is that*, for things too far away to be on the map.
**There is no range limit** — it reaches to the far side of the zone, where the
minimap stops at 120 units. A chest three thousand units away still has a
bearing, and that's the case the strip exists for.

It carries only what you'd travel toward — **party members**, **available
orbs**, **unopened chests**, **soulstones** and **obelisks**. Enemies, respawn
points and activities are left off on purpose: a compass crowded with things
that are permanently there is a smear.

**Soulstones and obelisks are the exceptions to the no-limit rule** and only
appear within 200 units. Both are worth knowing about when you're near one and
noise when you aren't — there are ten obelisks in a zone and they never move,
so at whole-map range they were most of what the strip was carrying.

**Under each marker is how far away it is** in world units — `44`, `653`,
`2.7k` — with `↑` or `↓` if it's well above or below you. When two markers are
close enough that their numbers would overlap, the nearer one keeps its label.

Icons match the minimap, `N`/`E`/`S`/`W` mark the cardinals along the top, and
the tick in the middle is dead ahead. You aren't drawn on it: you're dead ahead
by definition.

It sits on a **pill-shaped panel** in the map's colours — fully round at both
ends, no border and no shadow, so it reads as a band of colour rather than a
window. The panel stops just above the distances, so **the numbers hang off its
lower edge** onto the game rather than being boxed in with everything else.
Bearings are mapped across the pill's straight section, so nothing is ever drawn
on a rounded end where there'd be no panel under it. Drag it anywhere on itself once the
mouse is free (`Alt` or `Esc`); clicks pass through to the game the rest of the
time.

Hide it or set it to `Show in ESC` like the other windows, and it has its own
entry under Scaling.

### Themes

Five entries under **Theme** in the control menu. `Dark` re-skins the **whole
overlay** — meter, breakdown, minimap and compass — into the map panel's navy;
`Farever` is the parchment original.

| Theme | Outside a rift | Inside a rift |
|---|---|---|
| `Farever Dynamic` | parchment throughout | rift colours |
| `Dark Dynamic` *(default)* | navy throughout | rift colours |
| `Farever` | parchment throughout | unchanged |
| `Dark` | navy throughout | unchanged |
| `Rift` | rift colours | rift colours |

`Rift` stays rift with the escape menu open, too — if you pinned it, you asked
for it. The two `Dynamic` modes still show their own palette there, since in
their case the rift colours are something the game put you in rather than
something you chose.

Three colours never change with the theme: **blue damage**, **green healing**,
and green for "the overlay is unlocked". Those carry meaning, and a theme that
recoloured them would be renaming the language the meter is written in.

The rift look has no Farever or Dark variant on purpose: a rift looks like a
rift, and the two `Dynamic` entries are how you get it — that's what they're
for. `Rift` pins it for anyone who just likes the colours.

If you're upgrading, a saved `Dynamic` becomes `Dark Dynamic` — the same thing
it always drew.

### The only hotkey

| Key | Action |
|---|---|
| `Shift+\` *(rebindable)* | reset the current encounter (breakdown snaps back to you) — change it under **Controls** in the menu |

It fires while Farever has focus, with a global `RegisterHotKey` fallback if the
low-level hook is blocked. A **mouse** binding needs that low-level hook —
`RegisterHotKey` can't see mouse buttons — so on the fallback path the log says
so rather than leaving you wondering.

### Rifts

The hook reads `st.GameLayer.isRift` — reached by a pointer walk from the local
hero (`ent.Hero` inherits `st.State.layer`), so it's plain memory reads on the
heartbeat timer rather than an HL call that would have to ride inside the damage
hook. That matters: you enter a rift well before you hit anything in it.

While you're inside a rift the meter and breakdown **re-skin themselves** into
the rift palette — same widget tree, only the colours swap, so it's safe
mid-combat. That's what the two `Dynamic` themes do; the others pin it. The
damage and healing bars keep their blue and green, since those carry meaning the
theme shouldn't overwrite.

Entering one raises a rift-styled prompt in the middle of the game window asking
**"Enable 'View All Players'?"**, and leaving raises the mirror of it —
**"Return to viewing party members only?"** — so the wide view you switched on
for the rift doesn't quietly follow you back outside. While it's up it is the *only* overlay window
on screen — meter, breakdown and control menu all fade out. **Yes** switches to
all-players and resets the encounter (the mode button does the same; party-only
and all-players numbers can't share one encounter without the percentages
lying). **No** just dismisses it. It re-arms when you leave the rift, and closes
itself if the rift ends before you answer.

If your answer is always the same, tick **Do this every time** before pressing
Yes — or turn on **Auto 'View All Players' in rifts** under Options. They are one
setting reached two ways, so opting in from the prompt shows up ticked in the
menu. With it on the prompt stops appearing and the meter switches for you in
both directions: all-players on the way in, party-only on the way out. The box
only counts alongside **Yes** — pressing No is the answer that says the meter
shouldn't be touching the view, so ticking it and then declining discards the
tick. Turning the setting on from the menu applies to your next crossing rather
than the rift you're standing in, since each switch resets the encounter.

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
It's here for hacking on it — see **[TESTING.md](TESTING.md)** for how the
pieces fit together, how to probe the running game, and the field names that
mean the opposite of what they say.

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
  are never covered — **including the ones reached from the escape menu**
  (options, feedback, the back-to-menu and exit confirmations), which sit on
  top of it and take the screen back. If some always-on HUD class turns out to
  report itself as open — the names are logged as `[meter] game window ...` —
  add it to `MENU_IGNORE_WINDOWS` in `meter/farever_meter.py` and it stops
  counting.
- `Shift+\` is the last keybind. It survives because a reset is wanted *mid-fight*,
  which is exactly when the escape menu isn't an option.
- Summon/pet damage (non-`ent.Hero` dealers) is not yet attributed to its owner.
- Healing is *raw* healing — every heal counts at its full size whether or not
  the target had health to restore, and `OVER` says how much of it was wasted.
  No client is ever told how much a heal healed for (measured — see
  TESTING.md), so the size is computed the way the game computes it, from
  `data.cdb`: most heal skills carry their amount in `BaseSkill.dynVal1-3`
  (which the server replicates) and the rest are a ratio on the caster's Faith,
  Intellect, Strength or Dexterity. That covers 39 of the game's 44 heal
  skills exactly, from the first cast, with nothing landing. The other five
  scale on values a client can't read (`maxHealth` reads 0) and fall back to
  the largest amount that skill has been seen to restore.
- Natural regen is exempt from that estimate: it has no heal FX and is only
  ever observed as the health rise itself.
- Heal attribution matches each health rise to the oldest unmatched heal FX on
  that target within 1.5s.
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
   `ui.Console.getMyHero()`. **Healing** works differently: no heal amount is
   ever sent to a client. All fifteen heal entry points were hooked at once and
   only `ent.Unit.playHitHealFX` fires; its `HitData.amount` reads 0. So the
   meter reads what *is* replicated — the heal FX played on the target
   (`ent.Unit.playHitHealFX`) names the healing skill and its owner and is the
   heal EVENT, and a rise in the unit's health attribute
   (`ent.UnitAttributes.set_health`) is how much of it LANDED. Each rise is
   matched to the oldest unmatched FX on that unit within 1.5s; an FX that
   never gets one landed on a full-health target and is reported with
   `landed: 0`. The host estimates the heal's real size from the high-water
   mark of what that player's casts of that skill have restored. FX-less
   in-combat rises count as "Regen" (self) and are never estimated;
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
