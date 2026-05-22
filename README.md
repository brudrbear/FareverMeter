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
   pip install frida
   ```
2. Press **Enter**. You'll see a bunch of text scroll by while it downloads.
   This takes about 30 seconds.
3. When you get the prompt (the blinking cursor on a new line) back, it's done.

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

### Hotkeys

These work anywhere — even while Farever has focus.

| Key | What it does |
|---|---|
| `\` (backslash) | Show/hide the meter |
| `Shift + \` | Reset the meter to zero |
| `/` (forward slash) | Lock/unlock the meter so you can drag it to a new spot |

To move the meter: press `/`, then click-and-drag the **teal/red header bar**
to wherever you want it. Press `/` again to lock it in place.

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

**"failed to register '\\' hotkey (another app may own it)."**
> Something else on your computer is already using `\`, `Shift+\`, or `/`
> as a global hotkey. The meter still works, you just can't toggle/reset/
> unlock with the keyboard until you close whatever has those keys claimed.

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
