# Farever Damage Meter

A small, always-on-top damage tracker for **Farever**. Shows your total damage,
DPS, and a breakdown by damage type in a clean overlay that floats over the
game.

**Made by Brudr.** The meter reads the game's network traffic to figure out
how much damage you're doing. It **does not** modify the game, edit memory,
or send anything to Farever's servers. It's a one-way read of data the game
is already showing you.

---

## What you need

You'll install two things, once. After that you just double-click to run.

1. **Python** — the language the meter is written in
2. **Frida** — a tool that lets the meter eavesdrop on the game's traffic

Don't worry if those names mean nothing to you. The steps below walk through
every click.

---

## Step 1 — Install Python

Python is a free programming language. It's safe and used by millions of
people; it's published by a non-profit foundation.

1. Go to **<https://www.python.org/downloads/>**
2. Click the big yellow **Download Python** button (any 3.10 or newer is fine).
3. Open the installer that downloaded.
4. **VERY IMPORTANT** — on the first screen of the installer, tick the box
   that says **"Add python.exe to PATH"** at the bottom. If you miss this,
   nothing in the next steps will work.

   > If you forgot to tick it, just re-run the installer, choose **Modify**,
   > and tick the box this time.
5. Click **Install Now** and let it finish.
6. When it says "Setup was successful", close it.

### Check it worked

1. Press the **Windows key** on your keyboard.
2. Type **cmd** and press **Enter**. A black window with white text opens.
   That's the Command Prompt.
3. Type this exactly and press **Enter**:
   ```
   python --version
   ```
4. You should see something like `Python 3.12.0`. If you get an error about
   "not recognized", Python wasn't added to PATH — redo Step 1 with the
   tickbox.

Leave that Command Prompt window open, you'll use it in the next step.

---

## Step 2 — Install Frida

Frida is the part that lets the meter peek at the game's traffic. It's free
and open-source.

1. In that Command Prompt window from above, type:
   ```
   pip install frida Pillow
   ```
2. Press **Enter**. You'll see a bunch of text scroll by while it downloads.
   This takes about 30 seconds.
3. When you get the prompt (the blinking cursor on a new line) back, it's done.

> **Pillow** is only needed if you want to save 30s Parse screenshots as PNGs
> (see [30s Parse Mode](#30s-parse-mode) below). The rest of the meter works
> without it — if `pip install Pillow` fails, you can ignore the error and
> just skip the screenshot feature.

### A note about antivirus

Some antivirus programs flag Frida as suspicious because it's a debugger tool
— the same kind of thing security researchers and cheaters both use, even
though Frida itself is just a legitimate library. If Windows Defender or
your antivirus quarantines it, you may need to add an exception for the
FareverMeter folder. The meter doesn't do anything harmful, but I understand
if you'd rather not take my word for it. The full source code is in
`live_meter_simple.py` — you can read it.

---

## Step 3 — Run the meter

You only do the install steps once. From now on, this is the only step.

1. **Launch Farever** and log in. Get to the point where you can see your
   character in the world.
2. Open the **FareverMeter** folder (the one this README is in).
3. **Double-click `live_meter_simple.py`**. A black window opens, the meter
   overlay appears on screen.

That's it. Go fight something and watch the numbers.

### If double-clicking the file does nothing

This usually means Windows doesn't know to open `.py` files with Python.
You can run it from a terminal instead:

1. Open the FareverMeter folder.
2. Hold **Shift** and right-click on any empty space inside the folder.
3. Pick **Open PowerShell window here** (or **Open in Terminal**).
4. In the window that opens, type:
   ```
   python live_meter_simple.py
   ```
5. Press **Enter**.

---

## Using the meter

The meter shows a few things:

- **Header** — turns red-orange when you're in combat.
- **ID** — your character's internal entity ID, used for filtering. You don't
  need to do anything with this; it's there for debugging.
- **DAMAGE / Time in Combat** — what's been tracked since combat started.
- **Total / DPS** — running totals.
- **Per-type bars** — your damage broken down by element (Physical, Fire,
  Spark, etc.), with hit counts.

The saved 30s Parse screenshots also include a **Damage Timeline** bar graph
(per-second damage shape) — that one's PNG-only to keep the on-screen
overlay compact.

### Hotkeys

These only fire when **Farever has keyboard focus** — typing into another
app (or into the 30s Parse popup window) won't trigger them.

| Key | What it does |
|---|---|
| `\` (backslash) | Show/hide the meter |
| `Shift + \` | Reset the meter to zero |
| `Ctrl + \` | Toggle **30s Parse Mode** (see below) |
| `/` (forward slash) | Lock/unlock the meter so you can drag it to a new spot |
| `Shift + /` | Reset the meter's position back to top-right |

To move the meter: press `/`, then click-and-drag the **teal/red header bar**
to wherever you want it. Press `/` again to lock it in place.

> **AV note:** the meter uses a low-level Windows keyboard hook so it knows
> when Farever has focus. The hook only ever inspects `\` and `/` keystrokes,
> and only fires actions while Farever is the foreground window — but some
> antivirus tools flag any low-level keyboard hook as a "keylogger" on
> principle. If yours yells, the full source for the hook is in the
> `_run_focused_keyboard_hook` function in `live_meter_simple.py`. If the
> hook can't install (locked-down corporate Windows, etc.) the meter falls
> back to a global hotkey that fires regardless of focus.

### 30s Parse Mode

For target-dummy damage checks where you want a clean 30-second window every
time. Press `Ctrl + \` to toggle it on — the header switches to "Parse Mode"
and the meter waits for your first hit. As soon as combat starts, a 30-second
countdown begins. When it ends, the meter freezes the results so you can read
them. Press `Shift + \` to clear for another parse, or `Ctrl + \` again to
return to normal continuous logging.

**Result popup.** When the 30s timer hits zero, a small window appears
centered on the monitor your meter is on with:

- An optional **Name** field (e.g. "Frostfang dummy, no buffs")
- An optional **Description** field (build notes, conditions, whatever)
- The auto-filled date and time
- A **Save Screenshot** button that writes a meter-styled PNG to a folder
  called `parse_screenshots/` next to the script — and an **Open Folder**
  button to jump there

The popup is modeless: you can alt-tab away or click into Farever without
closing it. Closing it (X button) just dismisses without saving. Each PNG
filename is `YYYY-MM-DD_HH-MM-SS_<name>.png`, so they sort chronologically
in Explorer.

---

## Stopping the meter

- Close the black Command Prompt window the meter is running in, **or**
- Click the black window and press **Ctrl + C**.

The overlay will disappear. Farever keeps running fine.

---

## Troubleshooting

**"Farever.exe not running. Launch the game first."**
> Start Farever and log in before launching the meter.

**The black window flashes and disappears immediately.**
> There's an error you can't read because the window closes too fast.
> Open Command Prompt (Windows key → type `cmd` → Enter), then drag
> `live_meter_simple.py` from the folder into the window and press Enter.
> The error will stay on screen so you can see what went wrong.

**"python: command not found" / "'python' is not recognized"**
> Python wasn't added to PATH. Reinstall Python from python.org and **tick
> the "Add python.exe to PATH" box** during install.

**Meter shows "Awaiting combat data…" forever, even while fighting.**
> The meter might have started before the game finished logging in. Close
> the meter, make sure your character is in the world, then restart the
> meter.

**Meter shows damage from things you're not fighting.**
> This shouldn't happen any more, but if it does, the entity ID showing in
> the top-right of the meter is wrong. Press `Shift + \` to reset, then
> hit a target you're sure of — that re-locks the player ID.

**Antivirus is yelling.**
> See the note in Step 2. Frida is a real debugger; some antivirus programs
> treat all debuggers as suspicious. You can either whitelist the folder or
> not use the meter.

**"LL keyboard hook install failed (errno …); falling back to global RegisterHotKey."**
> The focus-conditional hook couldn't install (often: a security policy
> blocks low-level hooks). The meter falls back to global hotkeys, which
> still work — but pressing `\` while typing into the parse result popup
> will toggle the meter. Workaround: use the popup buttons with the mouse.

**"failed to register '\\' hotkey (another app may own it)."**
> Only shows up in the RegisterHotKey fallback path. Something else on your
> computer is already using `\`, `Shift+\`, `Ctrl+\`, or `/` as a global
> hotkey. The meter still works, you just can't toggle/reset/parse/unlock
> with the keyboard until you close whatever has those keys claimed.

**Saved parse screenshots are missing or "Pillow not installed" shows up.**
> Run `pip install Pillow` in Command Prompt. Screenshots write into a
> folder called `parse_screenshots/` next to `live_meter_simple.py`.

**The 30s Parse popup window doesn't appear when the timer ends.**
> It may have opened on a different monitor — it anchors to whichever
> monitor the meter overlay is on. Look there first. If it still doesn't
> show, check the black Command Prompt window for a "failed to open parse
> popup" error.

---

## What the meter does and doesn't do

**Does:**
- Read the encrypted traffic between Farever and its server, after Farever
  has already decrypted it locally.
- Parse out combat events (who hit whom, for how much, with what damage type).
- Display your own outgoing damage in an overlay.

**Does not:**
- Modify the game.
- Read or write Farever's process memory.
- Send any data to anywhere. Everything stays on your computer.
- Give you any in-game advantage beyond letting you see your own DPS.

---

## Credits

Made by **Brudr** for personal research and Farever community use.

If something breaks after a Farever patch, the protocol probably changed —
ping Brudr.
