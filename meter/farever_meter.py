"""
farever_meter.py — Farever+ party damage meter (memory-reading edition).

Attaches to Farever via Frida, injects meter_hook.js (which hooks the game's
own ent.Unit.onInflictDamage / ent.Unit.receiveHeal and streams every player's
damage and healing with real spell IDs), and renders two overlay windows:

  * the METER: every player sorted by damage done, with damage/DPS/% and
    healing-done columns, and
  * the BREAKDOWN: the inspected player's per-skill damage and per-skill
    healing side by side, plus per-element totals.

Everything is driven from the game itself rather than from hotkeys. The hook
watches the game's own window manager (ui.BaseUI.displayWindow/removeWindow), so
opening the game's escape menu — the moment the game frees the mouse cursor —
unlocks both windows for dragging, lets a click on a player row point the
breakdown at them, and pops up a small CONTROL MENU (centred on the game window,
draggable, position remembered) holding what used to be hotkeys. Closing the
escape menu puts everything back to click-through.

The breakdown snaps back to *your* hero on encounter reset, zone change, and
party/all mode switches.

Run:  python meter/farever_meter.py   (with Farever running)

Shipped as a windowed executable (see packaging/), which has no console — so it
logs to %LOCALAPPDATA%\\FareverMeter\\meter.log, asks its startup questions as
dialogs, and puts a tray icon in the notification area whose menu holds the
clean shutdown. Run from source it keeps the console and all three still work.

Stopping it matters: both the tray icon's "Stop the meter" and the control
menu's Quit button return from the Tk mainloop so the Frida hook is unloaded and
detached on the way out. Force-killing the process skips that, and a
half-attached agent is what destabilises the game across relaunches.

The only surviving hotkey (fires while Farever has focus):
  Shift+\\   reset the current encounter
"""
from __future__ import annotations

import ctypes
import json
import math
import os
import sys
import tempfile
import threading
import time
import tkinter as tk
from ctypes import wintypes
from tkinter import font as tkfont
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import frida

# ---------------------------------------------------------------------------
# Where things live
# ---------------------------------------------------------------------------
# Two layouts, one codebase.
#
#   From source — everything stays in the project folder, exactly as it always
#   has, so the developer workflow is untouched.
#
#   From the installed build — the code and the data part company. PyInstaller
#   unpacks the bundle into a temporary directory that is a *different path on
#   every launch* and is deleted on exit, so anything we WRITE (regenerated
#   offsets, window positions, parse images, the log) has to go somewhere
#   durable and user-writable instead. Everything we only READ — the agent JS,
#   the shipped JSON — comes out of the bundle.
FROZEN = bool(getattr(sys, "frozen", False))
# Bundled resources go under res/ rather than at the bundle root, so our own
# "frida" folder of agent JS can't collide with the frida *package* PyInstaller
# unpacks alongside it. Inside res/ the layout is the project's, unchanged.
ROOT = (Path(sys._MEIPASS) / "res") if FROZEN else Path(__file__).resolve().parent.parent

# %LOCALAPPDATA%\FareverMeter. Already the home of the single-instance lock, so
# the installed build isn't inventing a location — just keeping more there.
DATA_HOME = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "FareverMeter"
_WRITABLE = DATA_HOME if FROZEN else ROOT

FRIDA_DIR = ROOT / "frida"                  # read-only: the agent's JS
SHIPPED_ANALYSIS = ROOT / "analysis_out"    # read-only: the JSON we ship with
# Regenerated on nearly every launch, so it has to be writable — which the
# bundle isn't (usefully). Seeded from SHIPPED_ANALYSIS on first run.
ANALYSIS = (_WRITABLE / "analysis_out") if FROZEN else SHIPPED_ANALYSIS
POSITION_CACHE = _WRITABLE / ".meter_position.json"
PARSES_DIR = _WRITABLE / "parses"   # finished-parse images land here (gitignored)
LOG_FILE = DATA_HOME / "meter.log"
TARGET_PROCESS = "Farever.exe"

# One meter at a time. The lock deliberately lives OUTSIDE the project folder:
# copies of this script run from different directories have to find each other,
# and that's the case that actually bites — an old copy left running while a
# newer one is launched from somewhere else, with only the old overlay on screen
# to show for it. A per-project lock would miss exactly that.
LOCK_DIR = DATA_HOME
LOCK_FILE = LOCK_DIR / "instance.json"
# Process images that count as "another meter" when the lock file names them.
# The installed executable's name is here as a literal rather than read from
# sys.executable, so a source run recognises an installed one and vice versa.
EXE_NAME = "FareverMeter.exe"
METER_IMAGE_NAMES = frozenset({EXE_NAME.lower(), "python.exe", "pythonw.exe"})

# Set once at startup, before stderr is redirected: is there a console for a
# human to read? False under the installed (windowed) build and under
# pythonw.exe. Decides whether a prompt is a terminal question or a dialog, and
# whether the control menu still warns about closing the console window.
HAS_CONSOLE = sys.stderr is not None and sys.stdout is not None
# How long to let a running instance shut itself down before forcing it. It has
# to unload the hook and detach, which is the whole point of asking nicely.
QUIT_WAIT_SECS = 12.0
# Serialises the claim in claim_single_instance(). "Local\" scopes it to the
# logon session, which is the right boundary — two users on one machine each get
# their own meter, their own lock file and their own game.
CLAIM_MUTEX = "Local\\FareverMeterClaim"
CLAIM_WAIT_MS = 30000       # comfortably longer than a full QUIT_WAIT_SECS wait

COMBAT_TIMEOUT_SECS = 25.0
REFRESH_MS = 250
MAX_PLAYER_ROWS = 8
MAX_SKILL_ROWS = 8

# Game windows whose presence unlocks the overlay. The game already frees the
# mouse cursor for these, so grabbing it costs nothing and the overlay becomes
# draggable/clickable exactly when the player is in "UI mode" — no hotkey.
UNLOCK_ON_WINDOWS = ("ui.win.EscapeMenu",)

# Any OTHER game window (inventory, map, vendor, ...) hides the overlay while
# it's up: those screens are what the player is reading, and a meter floating
# over them is just clutter. The escape menu is excluded because that's the
# overlay's own unlock/settings moment — it has to stay visible then.
#
# The hook's window feed names every ui.win.* class it sees (each one is logged
# as "[meter] game window ..."), so if some always-on HUD class turns out to be
# reported as open, add it here and the overlay stops treating it as a menu.
MENU_IGNORE_WINDOWS = frozenset(UNLOCK_ON_WINDOWS)

# Grace period for "hide out of combat": the game's isInCombat flag drops
# between pulls, so hiding the instant it clears would make the overlay flicker
# through a trash pack.
HIDE_OOC_LINGER_SECS = 5.0

# Show/hide is a fade rather than a pop — a window blinking out of existence
# mid-fight reads as a crash. FADE_SECS is the full 0 -> OVERLAY_ALPHA travel;
# the driver ticks every FADE_STEP_MS on its own timer, not on the 250 ms
# refresh, which would be far too coarse to look like a fade.
# Vertical stack for the floating text over the top of the game window. The
# game draws the zone name across the very top, so everything starts below it —
# the keybind hint used to sit at +24 and land right on top of it.
TOP_STRIP_HINT = 96
TOP_STRIP_PARSE = 140
TOP_STRIP_RIFT = 190

OVERLAY_ALPHA = 0.94
FADE_SECS = 0.45
# The control menu and its hint answer to a keypress, so they want to feel
# immediate; the meter and breakdown fade on their own schedule, where a slower
# fade reads as deliberate rather than sluggish.
MENU_FADE_SECS = 0.15
FADE_STEP_MS = 25

# 60s Parse Mode: a fixed-length sample, so two runs are comparable in a way
# "whatever that pull happened to be" never is. The pre-roll exists because the
# button is clicked from the escape menu — you need those seconds to close it
# and get your hands back on the keyboard.
PARSE_PREROLL_SECS = 8
PARSE_LENGTH_SECS = 60

# Overlay elements the control menu can show/hide, as (key, label). This drives
# the menu's SHOW / HIDE section: add a row here and a checkbox appears for it,
# backed by Overlay._show[key].
# A key that names a window in Overlay._element_win is mapped/unmapped wholesale;
# any other key is a content toggle handled inside the render pass.
TOGGLEABLE_ELEMENTS = (
    ("meter", "Damage meter"),
    ("detail", "Breakdown"),
    ("healing", "Healing columns"),
    ("rift", "Rift timer"),
    ("minimap", "Minimap"),
)

# Elements the out-of-combat rule doesn't touch. The rift countdown is most use
# exactly when you're standing around between pulls, so hiding it out of combat
# would hide it for its whole useful life. The minimap is the same case only
# more so: its whole job is telling you what's around while you're travelling,
# which is by definition out of combat.
OOC_EXEMPT = ("rift", "minimap")

# ---------------------------------------------------------------------------
# Minimap
# ---------------------------------------------------------------------------
# Two orientations:
#
#   Rotating (default) — the map turns under you, so you are always facing the
#   top of it. Matches how you're actually looking at the world, which is what
#   most people want from a minimap while moving.
#
#   Fixed — north is always up and the arrow turns instead. Landmarks stay
#   where you last saw them, which is better for learning a zone.
#
# Rotating costs one extra rotate per entity per frame; at a few dozen entities
# that is nothing next to the canvas work.
MINIMAP_MODES = ("Rotating", "Fixed")
MINIMAP_SIZE = 270          # square, in pixels at 100% UI scale
# World units from centre to edge. Measured against a live fight rather than
# guessed, because the guess was out by a factor of four: a group and the pack
# it's fighting sit 1-12 units apart, nearby chests and orbs 10-80, and the
# next activity several hundred. Anything above ~150 collapses a whole fight
# into a couple of pixels; much below ~80 loses the interactibles.
MINIMAP_RANGE = 120
MINIMAP_TICK_MS = 100       # redraw cadence; the hook feeds us at ~6.7/sec

# The hook culls to 600 units, so the range above can grow without touching it.
MINIMAP_RANGE_MIN, MINIMAP_RANGE_MAX = 80, 600

# How each category is drawn: colour, radius in pixels, and shape. Kept in one
# table so the legend, the draw pass and any future re-skin can't disagree.
# Drawn in list order, so the things you most need to see land on top.
MINIMAP_STYLE = (
    ("activity", {"fill": "#E8C15A", "r": 4.0, "shape": "diamond"}),
    ("obelisk",  {"fill": "#B07BD8", "r": 3.5, "shape": "square"}),
    ("respawn",  {"fill": "#7FD8C0", "r": 3.0, "shape": "square"}),
    ("chest",    {"fill": "#E0A33C", "r": 3.5, "shape": "square"}),
    ("orb",      {"fill": "#6FC9E8", "r": 3.5, "shape": "dot"}),
    ("foe",      {"fill": "#C0392B", "r": 3.0, "shape": "dot"}),
    ("hero",     {"fill": "#5279B5", "r": 3.5, "shape": "dot"}),
)
MINIMAP_STYLE_MAP = dict(MINIMAP_STYLE)
MINIMAP_ORDER = [k for k, _ in MINIMAP_STYLE]

MINIMAP_ME = "#F2E1CB"          # the player arrow — brightest thing on the map
MINIMAP_PARTY_RING = "#57C7FF"  # the ring that marks a group member

# rotationZ is measured from +x (east), not +y (north) — established by the
# arrow pointing 90 degrees off the player's real facing until it was corrected.
# So the facing vector is (cos r, sin r), and screen-up corresponds to r = pi/2.

# A flat map can't tell you that a mob is on the gantry above you or in the
# tunnel below, and those are very different news. Anything further than this
# in elevation is drawn faded toward the background rather than hidden, so it
# still reads as present but not as something you can walk to.
# 30 rather than something tighter because the measured distribution is
# bimodal, not gradual: everything on your own floor came in at 0-12 units of
# elevation (slopes and ledges), and everything genuinely on another level at
# 154-173. Anywhere in that gap gives the same answer, so this sits clear of
# terrain rather than close to it.
MINIMAP_Z_FADE = 30.0       # world units of elevation before dimming kicks in
MINIMAP_Z_DIM = 0.4         # how much of the original colour survives

# Rifts open on the hour. The countdown is just the wall clock — reading the
# game's own world-event schedule turned out to report the running event rather
# than the next one, and this needs no hook at all. For the first few minutes
# past the hour the rift that just opened is the current one, so there's nothing
# to count down to yet.
RIFT_QUIET_MINS = 6

# ---- palette (Farever-style, matches original meter) ----
BG_BORDER = "#2C1A0E"
BG_BODY = "#F2E1CB"
BG_BODY_SOFT = "#E8D5B8"
BG_HEADER = "#54A4A9"
BG_HEADER_COMBAT = "#C9612A"
BG_HEADER_UNLOCKED = "#5E9C4A"   # green — the escape menu is open / draggable
BTN_ON_BG = BG_HEADER_UNLOCKED   # a control-menu button whose mode is active
BTN_ON_BG_ACTIVE = "#4E8340"     # ...the same button, hovered/pressed
BG_BAR_TRACK = "#D9C09A"
FG_HEADER = "#FFFFFF"
FG_HEADER_DIM = "#DCE9EA"   # captions in a header bar — readable on all tints
FG_TEXT = "#3D2817"
FG_VALUE = "#1F1208"
FG_DIM = "#7B5A3A"
FG_WARN = "#A32B1C"         # red that still reads on the cream body

# ---- rift palette (matches the rifts themselves: hot magenta rim, near-black
# maroon interior) — deliberately nothing like the rest of the overlay, so the
# countdown reads as belonging to the game's event rather than to the meter.
RIFT_EDGE = "#FF2E92"
RIFT_GLOW = "#8A1048"
RIFT_BODY = "#2C0A1E"
RIFT_TITLE = "#FF7BC0"
RIFT_TIME = "#FFE0F0"
RIFT_PEAK = "#FFB3D9"       # the top of the pulse, a hotter rim
# The countdown pulses once it's close, so it catches the eye without needing to
# be read. Its own timer, because the 250 ms refresh would make it a stutter.
RIFT_PULSE_SECS = 300       # start pulsing at 5 minutes left
RIFT_STYLE_SECS = 900       # ...and turn the box rift-coloured at 15
RIFT_PULSE_PERIOD = 1.4     # seconds per full pulse
RIFT_PULSE_MS = 40
RIFT_RIPPLE_MARGIN = 26     # transparent room around the panel for it
RIFT_RIPPLE_FADEOUT = 0.8   # ripple is gone by this much of the cycle

# Big mono while counting; smaller and quieter for the idle placeholder, which
# is only ever on screen so the window can be dragged into place.

ACCENT = "#3D7C7C"
DMG_BAR = "#5279B5"       # blue — damage bars
HEAL_BAR = "#5E9C4A"      # green — healing bars
TRANSPARENT_KEY = "#010101"

# The countdown box escalates as the rift approaches: ordinary Farever colours
# while it's far off, rift colours inside 15 minutes, then pulsing inside 5.
RIFT_BOX_FAR = {"glow": BG_BORDER, "edge": BG_BORDER, "body": BG_BODY,
                "title": ACCENT, "time": FG_VALUE}
RIFT_BOX_NEAR = {"glow": RIFT_GLOW, "edge": RIFT_EDGE, "body": RIFT_BODY,
                 "title": RIFT_TITLE, "time": RIFT_TIME}

# The meter and breakdown re-skin themselves while you're inside a rift, so the
# overlay matches what's on screen around it. Same widget tree either way — only
# the colours are swapped, by _apply_theme.
# Theme choices offered in the control menu. "Dynamic" is the interesting one:
# it follows the game, so the overlay matches whatever you're standing in.
THEME_MODES = ("Dynamic", "Farever", "Rift")

# Every font in the overlay is a *named* Tk font. That's what makes the scale
# slider possible: reconfiguring a named font resizes every widget using it and
# triggers a relayout, with no rebuild and no hunting down font tuples.
FONT_SPECS = {
    "ui_sm_b":    ("Segoe UI", 8, "bold"),
    "ui_b":       ("Segoe UI", 10, "bold"),
    "ui":         ("Segoe UI", 9),
    "ui_10":      ("Segoe UI", 10),
    "ui_tiny_i":  ("Segoe UI", 7, "italic"),
    "ui_lg_b":    ("Segoe UI", 13, "bold"),
    "ui_hint_b":  ("Segoe UI", 11, "bold"),
    "ui_parse_b": ("Segoe UI", 15, "bold"),
    "ui_idle_i":  ("Segoe UI", 10, "italic"),
    "mono":       ("Consolas", 9),
    "mono_10":    ("Consolas", 10),
    "mono_sm":    ("Consolas", 8),
    "mono_xl_b":  ("Consolas", 18, "bold"),
}
UI_SCALE_MIN, UI_SCALE_MAX = 75, 175      # percent
# Minimum widths, at 100%. They're pixel values, so the scale slider has to
# scale them too or scaling down just hits the floor and nothing moves.
MIN_W = {"meter": 360, "detail": 320, "menu": 230, "prompt": 320}
WARN_WRAP = 460                            # the red banner's wrap, at 100%

THEME_DEFAULT = {
    "border": BG_BORDER, "body": BG_BODY, "soft": BG_BODY_SOFT,
    "header": BG_HEADER, "header_combat": BG_HEADER_COMBAT,
    "header_unlocked": BG_HEADER_UNLOCKED, "track": BG_BAR_TRACK,
    "fg_header": FG_HEADER, "fg_header_dim": FG_HEADER_DIM,
    "fg_text": FG_TEXT, "fg_value": FG_VALUE, "fg_dim": FG_DIM,
    "accent": ACCENT, "dmg": DMG_BAR, "heal": HEAL_BAR,
    "header_off": "#4A4441",
}
THEME_RIFT = {
    "border": RIFT_EDGE, "body": RIFT_BODY, "soft": "#3D0F28",
    "header": RIFT_GLOW, "header_combat": "#C41E6E",
    # Green still means "unlocked", and it reads fine on the dark body — the
    # bars keep their meanings too (blue damage, green healing) rather than
    # being recoloured into the theme and losing what they stand for.
    "header_unlocked": BG_HEADER_UNLOCKED, "track": "#4A1030",
    "fg_header": RIFT_TIME, "fg_header_dim": "#F0A8CC",
    "fg_text": "#FFC9E4", "fg_value": "#FFFFFF", "fg_dim": "#C77AA0",
    "accent": RIFT_TITLE, "dmg": DMG_BAR, "heal": HEAL_BAR,
    "header_off": "#3B3036",
}

ELEMENT_COLORS = {
    "Physical": "#B68A4E", "Magic": "#5279B5", "Fire": "#C9612A",
    "Spark": "#D9B43C", "Earth": "#7C5A2E", "Water": "#4B8FB5",
    "Faith": "#C8B280", "Light": "#E5C95A", "Raw": "#8A6A4A",
    "Cheese": "#D8C25E", "Chaos": "#8E4FB5", "None": "#9A8B7A",
}

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000

# Win11 rounds a window's corners on request, and does it for these borderless
# layered popups too. DWM owns the shape from then on, so — unlike SetWindowRgn —
# nothing needs re-applying as the meter grows and shrinks with the party.
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2                   # 3 = DWMWCP_ROUNDSMALL, a tighter radius


# ---------------------------------------------------------------------------
# Running without a console
# ---------------------------------------------------------------------------
# The installed build is a windowed executable: no console, so nothing to press
# Ctrl+C in, nowhere for a print() to land, and no stdin to answer a prompt on.
# Each of those needs a replacement rather than a removal — the diagnostics in
# particular are the entire support process ("send me the log").

# Re-invoking ourselves to run one of the hltools generators. Frozen, there is
# no python.exe to call and sys.executable is *this* program, so the exe has to
# be able to act as its own interpreter for the two bundled tool scripts.
TOOL_FLAG = "--run-hltool"
CREATE_NO_WINDOW = 0x08000000   # ...or every regenerate flashes a console up


def run_bundled_tool(name, argv_rest):
    """Entry point for `FareverMeter.exe --run-hltool build_targets.py ...`.

    The tools are plain top-level scripts that do their work on import and exit
    via SystemExit, so they're run as scripts rather than imported — which also
    keeps them in their own process, as they are when run from source."""
    tool = ROOT / "hltools" / name
    if not tool.is_file():
        sys.exit(f"[!] bundled tool missing: {tool}")
    import runpy
    sys.argv = [str(tool), *argv_rest]
    sys.path.insert(0, str(tool.parent))
    runpy.run_path(str(tool), run_name="__main__")


def seed_analysis():
    """Make ANALYSIS exist and hold something usable.

    Only the installed build needs this: its writable data directory starts
    empty, while the JSON it should start from is inside the read-only bundle.
    Copying rather than symlinking means the first launch after an install has
    working data even if the game is mid-patch and regeneration fails."""
    if not FROZEN:
        return
    try:
        ANALYSIS.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"[meter] can't create {ANALYSIS}: {e}", file=sys.stderr)
        return
    import shutil
    for src in SHIPPED_ANALYSIS.glob("*.json"):
        dst = ANALYSIS / src.name
        if not dst.exists():
            try:
                shutil.copyfile(src, dst)
            except OSError as e:
                print(f"[meter] couldn't seed {dst.name}: {e}", file=sys.stderr)


def setup_logging():
    """Point stdout/stderr at a log file when there's no console behind them.

    Without this the windowed build is silent in the one situation where output
    matters most — it failed to start and the user wants to know why. The
    previous run is kept as meter.log.1, because "it worked yesterday" is
    usually asked after today's run has already overwritten the evidence."""
    if HAS_CONSOLE:
        return
    try:
        DATA_HOME.mkdir(parents=True, exist_ok=True)
        prev = LOG_FILE.with_suffix(".log.1")
        if LOG_FILE.exists():
            try:
                LOG_FILE.replace(prev)
            except OSError:
                # Windows won't rename a file another process still has open,
                # which is exactly the case where two meters overlap — during a
                # handover, or when one is displacing another. Appending below
                # rather than truncating means the outgoing instance's last
                # lines (the ones explaining the handover) survive it.
                pass
        # Append, not truncate: see above. After a successful rotation the file
        # is gone, so this creates a fresh one and the two are equivalent.
        # Line-buffered, so a crash mid-write still leaves the lines before it —
        # which is exactly the log you want to read after a crash.
        f = open(LOG_FILE, "a", buffering=1, encoding="utf-8", errors="replace")
    except OSError:
        return          # nowhere to log => run silently rather than not at all
    sys.stdout = sys.stderr = f
    print(f"[meter] log started {time.strftime('%Y-%m-%d %H:%M:%S')} "
          f"(frozen={FROZEN}, pid {os.getpid()})", file=sys.stderr)

    def hook(exc_type, exc, tb):
        import traceback
        print("[meter] unhandled exception:", file=sys.stderr)
        traceback.print_exception(exc_type, exc, tb, file=sys.stderr)
        f.flush()
    sys.excepthook = hook
    # Overlay work happens on the Tk thread but the hook, hotkeys and tray all
    # run on their own; a thread dying quietly would otherwise take a feature
    # with it and leave no trace.
    def thook(args):
        hook(args.exc_type, args.exc_value, args.exc_traceback)
    threading.excepthook = thook


# The meter can be asked to stop long before there's an overlay to stop — most
# obviously while it sits waiting for Farever to launch, which with no console
# is a stretch where the tray icon is the only sign of life and so must be the
# way out too. STOP is what the pre-overlay waits watch; once the overlay
# exists it takes over, because only it can unload the hook on the way down.
STOP = threading.Event()
_OVERLAY = {"ref": None}


def request_stop():
    """Stop the meter. Safe from any thread and at any point in startup."""
    STOP.set()
    ov = _OVERLAY["ref"]
    if ov is not None:
        ov.request_quit()


def message_box(text, title="Farever+ Meter", flags=0x40):
    """A dialog is the only way to reach a user who has no console. Used for
    the failures that stop the meter starting at all — anything softer belongs
    in the log."""
    try:
        ctypes.windll.user32.MessageBoxW(None, str(text), str(title),
                                         flags | 0x1000)   # MB_SETFOREGROUND
    except Exception:
        pass


def _hidden_tk():
    """A throwaway root so the startup dialogs can exist before the overlay
    does. Destroyed by the caller — the overlay builds its own."""
    r = tk.Tk()
    r.withdraw()
    r.attributes("-topmost", True)
    return r


def ask_choice(title, prompt, labels):
    """Pick one of `labels`, returning its index (or 0 if the user just closes
    the dialog — the same default the console prompt uses for a bare Enter)."""
    root = _hidden_tk()
    picked = {"i": 0}
    win = tk.Toplevel(root)
    win.title(title)
    win.configure(bg=BG_BODY, padx=14, pady=12)
    win.attributes("-topmost", True)
    win.resizable(False, False)
    tk.Label(win, text=prompt, bg=BG_BODY, fg=FG_TEXT, justify="left",
             anchor="w", font=("Segoe UI", 10)).pack(fill="x", pady=(0, 10))

    def choose(i):
        picked["i"] = i
        win.destroy()

    for i, label in enumerate(labels):
        tk.Button(win, text=label, command=lambda i=i: choose(i), anchor="w",
                  bg=BG_BODY_SOFT, fg=FG_TEXT, relief="flat", bd=0,
                  padx=10, pady=6, cursor="hand2",
                  font=("Segoe UI", 9)).pack(fill="x", pady=2)
    win.protocol("WM_DELETE_WINDOW", lambda: choose(0))
    win.update_idletasks()
    win.geometry(f"+{(win.winfo_screenwidth() - win.winfo_width()) // 2}"
                 f"+{(win.winfo_screenheight() - win.winfo_height()) // 3}")
    win.grab_set()
    root.wait_window(win)
    root.destroy()
    return picked["i"]


def ask_directory(title):
    """Folder picker, for when the game install can't be found automatically.
    Returns a Path or None."""
    from tkinter import filedialog
    root = _hidden_tk()
    try:
        d = filedialog.askdirectory(title=title, parent=root)
    finally:
        root.destroy()
    return Path(d) if d else None


def _version_tuple(s):
    """(2, 1) from "2.1" or "v2.1"; None for anything that isn't a plain
    numeric version. The repo also carries a `farever` tag, and a name-shaped
    tag is not something to compare a version against."""
    parts = (s or "").strip().lstrip("vV").split(".")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def _fetch_json(url):
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": f"FareverMeter/{VERSION}",
        "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=UPDATE_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _latest_version():
    """The newest published version as (tuple, name, url), or None.

    Releases first, tags as the fallback: the repo has so far shipped tags
    without a Release object behind them, and /releases/latest answers 404 in
    that state — so tags-only has to work or the check never fires."""
    try:
        rel = _fetch_json(UPDATE_API_RELEASE)
        v = _version_tuple(rel.get("tag_name"))
        if v:
            return v, rel.get("tag_name"), rel.get("html_url") or REPO_URL
    except Exception:
        pass            # no published release yet — normal, fall through
    try:
        tags = _fetch_json(UPDATE_API_TAGS)
    except Exception as e:
        print(f"[update] check skipped: {e}", file=sys.stderr)
        return None
    best = None
    for t in tags or []:
        v = _version_tuple(t.get("name"))
        # The tag list is not ordered by version, so this takes the highest
        # rather than trusting the first entry.
        if v and (best is None or v > best[0]):
            best = (v, t.get("name"), f"{REPO_URL}/releases")
    return best


def check_for_update():
    """Ask GitHub whether there's a newer version, on a background thread.

    Never blocks startup and never fails loudly: being offline, rate-limited or
    caught in a GitHub outage should cost the notice, not the meter. Set
    FAREVER_NO_UPDATE_CHECK to skip the request entirely."""
    if os.environ.get("FAREVER_NO_UPDATE_CHECK"):
        print("[update] check disabled by FAREVER_NO_UPDATE_CHECK.",
              file=sys.stderr)
        return

    def work():
        mine = _version_tuple(VERSION)
        found = _latest_version()
        if not found or mine is None:
            return
        v, name, url = found
        if v > mine:
            UPDATE["latest"], UPDATE["url"] = name, url
            print(f"[update] {name} is available (running {VERSION}) — {url}",
                  file=sys.stderr)
        else:
            print(f"[update] up to date (running {VERSION}).", file=sys.stderr)

    threading.Thread(target=work, daemon=True, name="update-check").start()


def _pretty_id(sid: str) -> str:
    """Readable fallback for skills the CDB has no display name for."""
    return sid.replace("_", " ") if sid and sid != "?" else sid


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
@dataclass
class PlayerAgg:
    name: str
    is_me: bool = False
    in_party: bool = False
    total: float = 0.0
    hits: int = 0
    crits: int = 0
    kills: int = 0
    heal_total: float = 0.0
    heal_hits: int = 0
    # skill -> [hits, total, crits]  (damage)
    skills: dict[str, list] = field(default_factory=lambda: defaultdict(lambda: [0, 0.0, 0]))
    # skill -> [hits, total, crits]  (healing)
    heals: dict[str, list] = field(default_factory=lambda: defaultdict(lambda: [0, 0.0, 0]))
    # element -> [hits, total]
    elements: dict[str, list] = field(default_factory=lambda: defaultdict(lambda: [0, 0.0]))
    first_time: float = 0.0
    last_time: float = 0.0

    def record(self, skill, element, amount, crit, kill, now):
        if self.first_time == 0.0:
            self.first_time = now
        self.last_time = now
        self.total += amount
        self.hits += 1
        if crit:
            self.crits += 1
        if kill:
            self.kills += 1
        s = self.skills[skill]
        s[0] += 1; s[1] += amount; s[2] += crit
        e = self.elements[element]
        e[0] += 1; e[1] += amount

    def record_heal(self, skill, amount, crit):
        self.heal_total += amount
        self.heal_hits += 1
        s = self.heals[skill]
        s[0] += 1; s[1] += amount; s[2] += crit


class PartySession:
    """Tracks the current encounter across all players.

    Encounter boundaries are hit-driven (a hit after > timeout of no damage
    starts a fresh encounter). The *duration* clock, however, only advances
    while at least one captured player is in combat — set_active() is driven by
    the caller with the game's isInCombat state, so DPS averages over active
    combat time rather than wall-clock.
    """

    def __init__(self, combat_timeout=COMBAT_TIMEOUT_SECS):
        self.lock = threading.Lock()
        self.timeout = combat_timeout
        self.players: dict[str, PlayerAgg] = {}
        self.skill_names: dict[str, str] = {}   # skill id -> display name
        self.combat: dict[str, int] = {}        # name -> isInCombat (from hook)
        self.enc_start = 0.0
        self.last_hit = 0.0
        self.active_accum = 0.0                  # accumulated in-combat seconds
        self.active_since = None                 # ts when current active run began
        self.in_combat = False
        self.epoch = 0          # bumped on explicit/zone reset (UI watches it)
        self.capture_until = None   # parse mode's hard cutoff (None = no limit)
        self.capture_start = None   # ...and when that window opened

    def set_capture_window(self, seconds):
        """Parse mode: take data for exactly `seconds` from now, then stop.
        Enforced here on the data path rather than by the UI tick, so the sample
        is the length asked for however the 250 ms refresh happens to land.
        None clears the limit."""
        with self.lock:
            now = time.time()
            self.capture_start = None if seconds is None else now
            self.capture_until = None if seconds is None else now + seconds

    def _capturing(self, now):
        return self.capture_until is None or now <= self.capture_until

    def _effective_duration(self, now):
        """Seconds to divide by for DPS.

        Normally that's in-combat time, so a pull's DPS isn't diluted by the
        walk to it. Inside a parse window it's wall-clock elapsed instead: the
        window *is* the measurement, so downtime has to count against you or two
        runs aren't comparable — and the game's isInCombat flag drops between
        pulls, which would otherwise inflate a 60 s parse by however much of it
        the flag happened to miss."""
        if self.capture_start is not None:
            return max(0.001, min(now, self.capture_until) - self.capture_start)
        return max(0.001, self._duration(now)) if self.enc_start else 0.0

    def _reset(self, now):
        self.players.clear()
        self.enc_start = 0.0
        self.active_accum = 0.0
        self.active_since = None

    def _player_for(self, ev):
        name = ev.get("player") or "?"
        p = self.players.get(name)
        if p is None:
            p = PlayerAgg(name=name, is_me=bool(ev.get("is_me")))
            self.players[name] = p
        if ev.get("is_me"):
            p.is_me = True
        if ev.get("in_party"):
            p.in_party = True
        return p

    def _skill_of(self, ev):
        sid = ev.get("skill", "?")
        nm = ev.get("name")
        if nm and sid not in self.skill_names:
            self.skill_names[sid] = nm
        return sid

    def record(self, ev: dict):
        with self.lock:
            now = time.time()
            if not self._capturing(now):
                return
            # The lull-reset is suppressed inside a parse window: a quiet
            # stretch mid-parse is part of the sample, not the start of a new
            # encounter, and wiping 40 seconds in would ruin the run.
            if (self.capture_until is None and self.last_hit
                    and (now - self.last_hit) > self.timeout):
                self._reset(now)          # new encounter after a long lull
            if self.enc_start == 0.0:
                self.enc_start = now
            self.last_hit = now
            p = self._player_for(ev)
            p.record(self._skill_of(ev), ev.get("element", "?"),
                     float(ev.get("amount", 0.0)), int(ev.get("crit", 0)),
                     int(ev.get("kill", 0)), now)

    def record_heal(self, ev: dict):
        # Heals are recorded but never drive encounter boundaries: an
        # out-of-combat potion/regen must not roll the meter into a fresh
        # encounter (damage does that), so last_hit stays untouched.
        with self.lock:
            if not self._capturing(time.time()):
                return
            p = self._player_for(ev)
            p.record_heal(self._skill_of(ev),
                          float(ev.get("amount", 0.0)), int(ev.get("crit", 0)))

    def set_combat(self, state: dict):
        with self.lock:
            self.combat = state

    def set_active(self, active: bool, now: float):
        """Advance/pause the duration clock based on whether a captured player
        is currently in combat. Called each UI tick with a mode-aware value."""
        with self.lock:
            # Past a parse window's cutoff the clock stops even mid-fight, so
            # the duration the meter shows is the sample's, not the pull's.
            if not self._capturing(now):
                active = False
            if active and self.active_since is None:
                self.active_since = now
            elif not active and self.active_since is not None:
                self.active_accum += now - self.active_since
                self.active_since = None
            self.in_combat = active

    def reset(self):
        with self.lock:
            self._reset(time.time())
            self.last_hit = 0.0
            self.in_combat = False
            # A reset always returns to live capture — and so to in-combat DPS.
            self.capture_until = self.capture_start = None
            self.epoch += 1

    def _duration(self, now):
        d = self.active_accum
        if self.active_since is not None:
            d += now - self.active_since
        return d

    def combat_of(self, name):
        return bool(self.combat.get(name))

    def current(self):
        """(duration, in_combat) — cheap, reflects the latest clock state."""
        with self.lock:
            return self._effective_duration(time.time()), self.in_combat

    def snapshot(self):
        """Return (duration, in_combat, [PlayerAgg sorted by total desc])."""
        with self.lock:
            duration = self._effective_duration(time.time())
            rows = sorted(self.players.values(), key=lambda p: -p.total)
            import copy
            return duration, self.in_combat, [copy.copy(p) for p in rows]


class WorldSnapshot:
    """The latest sweep of nearby entities, for the minimap.

    Deliberately last-wins rather than accumulating: this is a live picture of
    where things are *now*, and a stale entity is worse than a missing one. The
    hook has already culled to radius and dropped everything not worth drawing,
    so this just holds what arrived."""

    def __init__(self):
        self._lock = threading.Lock()
        self.me = {"x": 0, "y": 0, "r": 0.0}
        self.ents = []
        self.stamp = 0.0
        # Both come from the hook's `hero` message, which already carries the
        # group roster it reads for the meter's party filter. Reusing it means
        # "who is in my group" has one answer, and it covers group members who
        # haven't dealt damage yet — which the meter's own per-player flag
        # can't, since that's only set when someone lands a hit.
        self.party = frozenset()
        self.local = None

    def update(self, payload):
        with self._lock:
            self.me = payload.get("me") or self.me
            self.ents = payload.get("ents") or []
            self.stamp = time.monotonic()

    def set_hero(self, name, party):
        with self._lock:
            if name:
                self.local = name
            self.party = frozenset(party or ())

    def read(self):
        """A snapshot for the draw pass. Copied under the lock because the
        overlay iterates it on the Tk thread while the hook thread replaces it."""
        with self._lock:
            return self.me, list(self.ents), self.stamp

    def who(self):
        with self._lock:
            return self.local, self.party

    def fresh(self, max_age=2.0):
        with self._lock:
            return self.stamp > 0 and (time.monotonic() - self.stamp) < max_age


class GameUIState:
    """Which of the game's own UI windows are open, streamed by the hook.

    The hook watches ui.BaseUI.displayWindow/removeWindow — the game's window
    manager — and reports each window class as it opens and closes. That lets
    the overlay react to the game's UI (escape menu open => unlock) instead of
    making the player remember a key."""

    def __init__(self):
        self._lock = threading.Lock()
        self._open: set[str] = set()
        self._rift = False

    def set_rift(self, state: bool):
        with self._lock:
            self._rift = bool(state)

    def in_rift(self) -> bool:
        with self._lock:
            return self._rift

    def set_window(self, name: str, is_open: bool):
        if not name:
            return
        with self._lock:
            if is_open:
                self._open.add(name)
            else:
                self._open.discard(name)

    def clear(self):
        with self._lock:
            self._open.clear()

    def any_open(self, names) -> bool:
        with self._lock:
            return any(n in self._open for n in names)

    def any_open_except(self, names) -> bool:
        """True while any game window *other* than `names` is open — i.e. the
        player is looking at one of the game's own screens."""
        with self._lock:
            return bool(self._open - set(names))


# ---------------------------------------------------------------------------
# Windows click-through + hotkeys (adapted from the original meter)
# ---------------------------------------------------------------------------
def _set_rounded_corners(hwnd):
    """Ask DWM to round this window's corners. Silently does nothing anywhere
    but Windows 11 (older builds don't know the attribute and just fail)."""
    if sys.platform != "win32":
        return
    try:
        pref = ctypes.c_int(DWMWCP_ROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd),
            ctypes.c_uint(DWMWA_WINDOW_CORNER_PREFERENCE),
            ctypes.byref(pref), ctypes.sizeof(pref))
    except Exception:
        pass


def _set_clickthrough(hwnd, enabled):
    if sys.platform != "win32":
        return
    u = ctypes.windll.user32
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        get, setl = u.GetWindowLongPtrW, u.SetWindowLongPtrW
    else:
        get, setl = u.GetWindowLongW, u.SetWindowLongW
    ex = get(hwnd, GWL_EXSTYLE) | WS_EX_LAYERED | WS_EX_NOACTIVATE
    if enabled:
        ex |= WS_EX_TRANSPARENT
    else:
        ex &= ~WS_EX_TRANSPARENT
    setl(hwnd, GWL_EXSTYLE, ex)


def _window_rect_of_pid(pid):
    """(left, top, right, bottom) of a process's largest visible top-level
    window, or None. Used to centre the control menu on the game rather than on
    whichever monitor Windows calls primary."""
    if sys.platform != "win32":
        return None
    from ctypes import wintypes
    u = ctypes.windll.user32
    u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                           ctypes.POINTER(wintypes.DWORD)]
    best = {"area": 0, "rect": None}
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                     wintypes.LPARAM)

    def visit(hwnd, _lparam):
        wpid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value != pid or not u.IsWindowVisible(hwnd):
            return True
        r = wintypes.RECT()
        if u.GetWindowRect(hwnd, ctypes.byref(r)):
            area = (r.right - r.left) * (r.bottom - r.top)
            if area > best["area"]:
                best["area"] = area
                best["rect"] = (r.left, r.top, r.right, r.bottom)
        return True

    try:
        u.EnumWindows(WNDENUMPROC(visit), 0)
    except Exception:
        return None
    return best["rect"] if best["area"] > 0 else None


VK_OEM_5 = 0xDC                      # the \ key
VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12
WH_KEYBOARD_LL, HC_ACTION = 13, 0
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
WM_HOTKEY = 0x0312
MOD_SHIFT, MOD_NOREPEAT = 0x0004, 0x4000

# Shift+\ (reset the encounter) is the only key the meter still owns — every
# other control moved onto the in-game control menu. Plain \ and / are left
# alone now so the game keeps them.
HK_RESET = 1
# Split out so the floating hint and the menu button can't drift apart.
RESET_HOTKEY_KEYS = "Shift + \\"
RESET_HOTKEY_TEXT = f"Reset FareverPlus - {RESET_HOTKEY_KEYS}"

# ---------------------------------------------------------------------------
# Version / update check
# ---------------------------------------------------------------------------
# Bump this on every release, and tag the repo with the same string — it's the
# left-hand side of the comparison below, so a release that forgets it tells
# everyone they're out of date forever.
VERSION = "2.2"

REPO = "brudrbear/FareverMeter"
REPO_URL = f"https://github.com/{REPO}"
UPDATE_API_RELEASE = f"https://api.github.com/repos/{REPO}/releases/latest"
UPDATE_API_TAGS = f"https://api.github.com/repos/{REPO}/tags"
UPDATE_TIMEOUT = 5.0
# Filled in by the checker thread, read by the overlay's refresh tick.
UPDATE = {"latest": None, "url": REPO_URL}

QUIT_LABEL = "Stop the meter"

# What the top of the control menu says about shutting down, which depends on
# what there is to shut down *with*. Run from a console there's still a window
# someone can close — force-killing the process before it can unload the hook —
# so that build keeps the warning. The installed build has no console to close
# and two proper exits instead, so it gets told where they are.
SHUTDOWN_WARNING = ("ALWAYS CLOSE FAREVER+ BY CTRL+C "
                    "NOT BY CLOSING THE COMMAND WINDOW")
SHUTDOWN_HINT = ("To stop the meter, use the Stop button below — or right-click "
                 "the Farever+ icon in the notification area by the clock.")


def start_hotkeys(callbacks: dict, target_pid):
    """Run the keyboard hook that owns Shift+\\, on its own thread with its own
    message pump."""
    if sys.platform != "win32":
        return

    def pump():
        from ctypes import wintypes
        u = ctypes.windll.user32
        LRESULT = ctypes.c_ssize_t
        HHOOK = ctypes.c_void_p

        class KBD(ctypes.Structure):
            _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.c_void_p)]

        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        u.SetWindowsHookExW.restype = HHOOK
        u.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, ctypes.c_void_p, wintypes.DWORD]
        u.CallNextHookEx.restype = LRESULT
        u.CallNextHookEx.argtypes = [HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        u.GetForegroundWindow.restype = wintypes.HWND
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetAsyncKeyState.restype = ctypes.c_short

        keys_down = set()

        def pressed(vk):
            return bool(u.GetAsyncKeyState(vk) & 0x8000)

        def fg_pid():
            h = u.GetForegroundWindow()
            if not h:
                return 0
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(h, ctypes.byref(pid))
            return pid.value

        def proc(nCode, wParam, lParam):
            if nCode != HC_ACTION:
                return u.CallNextHookEx(None, nCode, wParam, lParam)
            kbd = ctypes.cast(lParam, ctypes.POINTER(KBD))[0]
            vk = kbd.vkCode
            if wParam in (WM_KEYUP, WM_SYSKEYUP):
                keys_down.discard(vk)
                return u.CallNextHookEx(None, nCode, wParam, lParam)
            if wParam not in (WM_KEYDOWN, WM_SYSKEYDOWN) or vk in keys_down:
                return u.CallNextHookEx(None, nCode, wParam, lParam)
            keys_down.add(vk)
            # Shift+\ only. Everything else — including a bare \ — falls through
            # untouched so the game keeps its own bindings.
            if vk != VK_OEM_5 or fg_pid() != target_pid:
                return u.CallNextHookEx(None, nCode, wParam, lParam)
            if (not pressed(VK_SHIFT)) or pressed(VK_CONTROL) or pressed(VK_MENU):
                return u.CallNextHookEx(None, nCode, wParam, lParam)
            cb = callbacks.get(HK_RESET)
            if cb:
                try:
                    cb()
                except Exception as e:
                    print("[hotkey]", e, file=sys.stderr)
            return 1

        cproc = HOOKPROC(proc)
        hMod = ctypes.windll.kernel32.GetModuleHandleW(None)
        hook = u.SetWindowsHookExW(WH_KEYBOARD_LL, cproc, hMod, 0)
        from ctypes import wintypes
        if hook:
            print("[meter] focus-conditional hotkeys active.", file=sys.stderr)
            msg = wintypes.MSG()
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                u.TranslateMessage(ctypes.byref(msg))
                u.DispatchMessageW(ctypes.byref(msg))
            return
        # ---- Fallback: global RegisterHotKey (fires regardless of focus) ----
        print("[meter] LL hook failed; using global RegisterHotKey fallback.",
              file=sys.stderr)
        if not u.RegisterHotKey(None, HK_RESET, MOD_SHIFT | MOD_NOREPEAT,
                                VK_OEM_5):
            print("[meter] Shift+\\ unavailable (another app owns it) — the "
                  "encounter still resets itself on a zone change or after a "
                  "lull, but the manual reset won't fire.", file=sys.stderr)
        msg = wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                cb = callbacks.get(msg.wParam)
                if cb:
                    try:
                        cb()
                    except Exception as e:
                        print("[hotkey]", e, file=sys.stderr)
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))

    threading.Thread(target=pump, daemon=True, name="hotkeys").start()


# ---------------------------------------------------------------------------
# Tray icon
# ---------------------------------------------------------------------------
# With no console there is no Ctrl+C, and the overlay windows are borderless and
# click-through — so without this there would be no way to stop the meter except
# Task Manager, which is exactly the force-kill that leaves a half-attached
# agent in the game. The icon exists to make the clean exit reachable.
#
# Hand-rolled on ctypes rather than pystray: the file already talks to user32
# directly for click-through, hotkeys and window enumeration, and a tray icon is
# one window and one message pump. It also keeps `pip install frida` as the only
# thing a from-source run needs.
ICON_FILE = ROOT / "assets" / "farevermeter.ico"

WM_TRAY = 0x0400 + 1                      # WM_APP + 1
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIIF_INFO = 0x01
WM_DESTROY, WM_CLOSE, WM_COMMAND = 0x0002, 0x0010, 0x0111
WM_LBUTTONUP, WM_RBUTTONUP = 0x0202, 0x0205
MF_STRING, MF_SEPARATOR = 0x0000, 0x0800
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
SM_CXSMICON, SM_CYSMICON = 49, 50

TRAY_QUIT, TRAY_LOG, TRAY_PARSES = 1001, 1002, 1003

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_byte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    """The full (Vista+) layout. cbSize is set to sizeof(), which is what tells
    the shell which version it's being handed."""
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD), ("guidItem", GUID),
                ("hBalloonIcon", wintypes.HICON)]


class TrayIcon:
    """A notification-area icon whose menu holds the clean shutdown.

    Owns a hidden window on its own thread: tray callbacks are window messages,
    and they're delivered to the thread that created the window, so it needs a
    pump of its own rather than sharing Tk's."""

    def __init__(self, on_quit, tip="Farever+ Meter"):
        self.on_quit = on_quit
        self.tip = tip
        self.hwnd = None
        self._ready = threading.Event()
        self._thread = None

    # ---- lifecycle ----
    def start(self):
        if sys.platform != "win32":
            return
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="tray")
        self._thread.start()
        # Bounded: a tray that fails to come up must not stop the meter from
        # starting — the in-game menu's Quit button is the other way out.
        self._ready.wait(timeout=5.0)

    def stop(self):
        """Called from the Tk thread on the way out. PostMessage rather than a
        direct call because the window belongs to the tray thread."""
        if self.hwnd:
            try:
                ctypes.windll.user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
            except Exception:
                pass

    # ---- internals ----
    def _load_icon(self):
        u = ctypes.windll.user32
        if ICON_FILE.is_file():
            h = u.LoadImageW(None, str(ICON_FILE), IMAGE_ICON,
                             u.GetSystemMetrics(SM_CXSMICON),
                             u.GetSystemMetrics(SM_CYSMICON), LR_LOADFROMFILE)
            if h:
                return h
            print(f"[tray] couldn't load {ICON_FILE} — using the stock icon.",
                  file=sys.stderr)
        return u.LoadIconW(None, ctypes.c_wchar_p(32512))   # IDI_APPLICATION

    def _notify(self, action, data):
        return bool(ctypes.windll.shell32.Shell_NotifyIconW(action,
                                                            ctypes.byref(data)))

    def _base_data(self):
        d = NOTIFYICONDATAW()
        d.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        d.hWnd = self.hwnd
        d.uID = 1
        return d

    def _add(self):
        d = self._base_data()
        d.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        d.uCallbackMessage = WM_TRAY
        d.hIcon = self.hicon
        d.szTip = self.tip
        self._notify(NIM_ADD, d)

    def _balloon(self, title, text):
        """Windows 11 files a brand-new tray icon into the overflow flyout by
        default, so a first-run user would never find it. The toast is what
        tells them it's there — and how to get it back."""
        d = self._base_data()
        d.uFlags = NIF_INFO
        d.szInfoTitle = title
        d.szInfo = text
        d.dwInfoFlags = NIIF_INFO
        self._notify(NIM_MODIFY, d)

    def _menu(self):
        u = ctypes.windll.user32
        m = u.CreatePopupMenu()
        u.AppendMenuW(m, MF_STRING, TRAY_PARSES, "Open the parse folder")
        u.AppendMenuW(m, MF_STRING, TRAY_LOG, "Open the log folder")
        u.AppendMenuW(m, MF_SEPARATOR, 0, None)
        u.AppendMenuW(m, MF_STRING, TRAY_QUIT, "Stop the meter")
        pt = wintypes.POINT()
        u.GetCursorPos(ctypes.byref(pt))
        # Required by TrackPopupMenu, or the menu refuses to close when the user
        # clicks away from it.
        u.SetForegroundWindow(self.hwnd)
        cmd = u.TrackPopupMenu(m, TPM_RIGHTBUTTON | TPM_RETURNCMD,
                               pt.x, pt.y, 0, self.hwnd, None)
        u.PostMessageW(self.hwnd, 0x0000, 0, 0)     # WM_NULL, same reason
        u.DestroyMenu(m)
        return cmd

    def _on_command(self, cmd):
        if cmd == TRAY_QUIT:
            self.on_quit()
        elif cmd == TRAY_LOG:
            try:
                DATA_HOME.mkdir(parents=True, exist_ok=True)
                os.startfile(DATA_HOME)
            except Exception as e:
                print(f"[tray] couldn't open {DATA_HOME}: {e}", file=sys.stderr)
        elif cmd == TRAY_PARSES:
            try:
                PARSES_DIR.mkdir(parents=True, exist_ok=True)
                os.startfile(PARSES_DIR)
            except Exception as e:
                print(f"[tray] couldn't open {PARSES_DIR}: {e}", file=sys.stderr)

    @staticmethod
    def _prototypes():
        """Declare every call this class makes.

        Not optional on 64-bit: ctypes defaults an unprototyped argument to C
        int, so any handle or pointer — a module handle, an lParam carrying a
        struct — overflows on the way through. Declared here in one place
        rather than at each call site, because the failure mode is a call that
        looks correct and raises at runtime on some machines and not others."""
        u, k32 = ctypes.windll.user32, ctypes.windll.kernel32
        k32.GetModuleHandleW.restype = ctypes.c_void_p
        k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        u.DefWindowProcW.restype = ctypes.c_ssize_t
        u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                     wintypes.WPARAM, wintypes.LPARAM]
        u.RegisterClassW.restype = wintypes.ATOM
        u.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        u.CreateWindowExW.restype = wintypes.HWND
        u.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        u.DestroyWindow.argtypes = [wintypes.HWND]
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                   wintypes.WPARAM, wintypes.LPARAM]
        u.LoadImageW.restype = ctypes.c_void_p
        u.LoadImageW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                 wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                 wintypes.UINT]
        u.LoadIconW.restype = ctypes.c_void_p
        u.LoadIconW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        u.CreatePopupMenu.restype = ctypes.c_void_p
        u.AppendMenuW.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                  ctypes.c_size_t, wintypes.LPCWSTR]
        u.TrackPopupMenu.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     wintypes.HWND, ctypes.c_void_p]
        u.DestroyMenu.argtypes = [ctypes.c_void_p]
        shell = ctypes.windll.shell32
        shell.Shell_NotifyIconW.restype = wintypes.BOOL
        shell.Shell_NotifyIconW.argtypes = [wintypes.DWORD,
                                            ctypes.POINTER(NOTIFYICONDATAW)]

    def _run(self):
        u = ctypes.windll.user32
        self._prototypes()
        # Explorer drops every tray icon when it restarts and broadcasts this to
        # ask for them back. Without it an explorer crash silently costs the
        # user their only way to stop the meter.
        taskbar_created = u.RegisterWindowMessageW("TaskbarCreated")

        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_TRAY:
                if lparam in (WM_RBUTTONUP, WM_LBUTTONUP):
                    self._on_command(self._menu())
                return 0
            if msg == WM_COMMAND:
                # The popup is read with TPM_RETURNCMD, so a click comes back
                # from _menu() rather than through here. This is the standard
                # route into the same commands for anything that drives the icon
                # by message — and it's how the shutdown path gets tested
                # without a human clicking the menu.
                self._on_command(wparam & 0xFFFF)
                return 0
            if msg == taskbar_created:
                self._add()
                return 0
            if msg == WM_CLOSE:
                d = self._base_data()
                self._notify(NIM_DELETE, d)
                u.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                u.PostQuitMessage(0)
                return 0
            return u.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc = WNDPROC(wndproc)      # kept alive: Windows holds a raw
        cls = WNDCLASSW()                     # pointer to it for the window's life
        cls.lpfnWndProc = self._wndproc
        cls.lpszClassName = "FareverMeterTray"
        cls.hInstance = ctypes.windll.kernel32.GetModuleHandleW(None)
        try:
            if not u.RegisterClassW(ctypes.byref(cls)):
                raise OSError(ctypes.get_last_error())
            u.CreateWindowExW.restype = wintypes.HWND
            self.hwnd = u.CreateWindowExW(0, "FareverMeterTray", "Farever+ Meter",
                                          0, 0, 0, 0, 0, None, None,
                                          cls.hInstance, None)
            if not self.hwnd:
                raise OSError("CreateWindowExW failed")
            self.hicon = self._load_icon()
            self._add()
        except Exception as e:
            print(f"[tray] icon unavailable ({e}) — use the control menu's Quit "
                  "button to stop the meter.", file=sys.stderr)
            self._ready.set()
            return
        print("[meter] tray icon active.", file=sys.stderr)
        self._balloon("Farever+ Meter is running",
                      "Right-click this icon to stop it. If it's hidden, click "
                      "the ^ arrow by the clock and drag it out.")
        self._ready.set()
        msg = wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))


# ---------------------------------------------------------------------------
# Overlay
# ---------------------------------------------------------------------------
class Overlay:
    def __init__(self, session: PartySession, target_pid, ui_state=None,
                 world=None):
        self.session = session
        self.target_pid = target_pid
        self.ui_state = ui_state if ui_state is not None else GameUIState()
        self.world = world if world is not None else WorldSnapshot()
        self.focus_player = None       # drilled-in player name (None => local)
        self.mode = "party"            # "party" (group only) or "all"
        # The overlay is click-through unless the game has freed the cursor for
        # its escape menu. There is no manual lock any more — the game's own UI
        # state is the single source of truth.
        self._menu_unlock = False
        self._hide_ooc = False         # "hide out of combat" setting
        # _show is what the player asked for, _shown is what's actually mapped
        # (they differ while out-of-combat hiding is in effect).
        self._show = {k: True for k, _ in TOGGLEABLE_ELEMENTS}
        self._shown = dict(self._show)
        self._heal_cols_shown = True   # last healing layout pushed to the widgets
        self._combat_seen_at = 0.0     # last moment a tracked player was fighting
        self._header_bg = BG_HEADER    # last tint pushed to the header bars
        self._theme = THEME_DEFAULT    # what's painted right now
        self._theme_mode = THEME_MODES[0]   # what the player asked for
        self._action_q = []
        self._q_lock = threading.Lock()
        self._quit_armed = False       # the Quit button's second-click window
        self._update_shown = False     # the update notice is applied once
        self._map_mode = MINIMAP_MODES[0]   # "Rotating" — you always face up
        # True while the countdown has nothing to count down to. The window is
        # hidden in that state unless the escape menu is open, so it isn't
        # sitting there saying "No rift upcoming" for six minutes of every hour.
        self._rift_idle = True
        self._last_epoch = session.epoch
        # Parse mode: None | "countdown" (pre-roll) | "parsing" | "done" (the
        # finished sample, frozen on screen until it's cleared).
        self._parse_state = None
        self._parse_until = 0.0
        # Rift prompt: modal, and the only overlay window on screen while it's
        # up. _rift_seen is the edge detector — one prompt per rift entry.
        self._prompt_open = False
        self._prompt_kind = "enter"
        self._rift_pulse = False
        self._pulse_job = None
        self._rift_box = None      # which palette the box is wearing
        self._rift_seen = False

        pos = self._load_positions()
        self.root = tk.Tk()
        self._ui_scale = 1.0
        self.fonts = {}
        for key, (family, size, *style) in FONT_SPECS.items():
            self.fonts[key] = tkfont.Font(
                root=self.root, family=family, size=size,
                weight="bold" if "bold" in style else "normal",
                slant="italic" if "italic" in style else "roman")
        self.root.title("Farever+ Party Meter")
        self.detail = tk.Toplevel(self.root)
        self.detail.title("Farever+ Breakdown")
        self.menu = tk.Toplevel(self.root)
        self.menu.title("Farever+ Controls")
        self.hintwin = tk.Toplevel(self.root)
        self.hintwin.title("Farever+ Hint")
        self.parsewin = tk.Toplevel(self.root)
        self.parsewin.title("Farever+ Parse")
        self.promptwin = tk.Toplevel(self.root)
        self.promptwin.title("Farever+ Prompt")
        self.riftwin = tk.Toplevel(self.root)
        self.riftwin.title("Farever+ Rift Timer")
        self.mapwin = tk.Toplevel(self.root)
        self.mapwin.title("Farever+ Minimap")
        for win in (self.root, self.detail, self.menu, self.hintwin,
                    self.parsewin, self.promptwin, self.riftwin, self.mapwin):
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.configure(bg=TRANSPARENT_KEY)
            try:
                win.wm_attributes("-transparentcolor", TRANSPARENT_KEY)
            except tk.TclError:
                pass
            # Rounding has to be (re-)applied on every map, not once here: Tk
            # rebuilds a toplevel's Windows wrapper as it applies the wm
            # attributes above, and the DWM setting dies with the old hwnd.
            win.bind("<Map>", self._on_map_round, add="+")
        for win in (self.root, self.detail, self.menu, self.riftwin,
                    self.mapwin):
            win.attributes("-alpha", OVERLAY_ALPHA)
        # Keys must match TOGGLEABLE_ELEMENTS.
        self._element_win = {"meter": self.root, "detail": self.detail,
                             "rift": self.riftwin, "minimap": self.mapwin}
        # Every window that fades: the two toggleable ones, plus the control
        # menu and its hint, which follow the game's escape menu.
        self._fade_win = dict(self._element_win, menu=self.menu,
                              hint=self.hintwin, prompt=self.promptwin)
        self._shown["menu"] = self._shown["hint"] = False
        self._shown["prompt"] = False
        self._shown["rift"] = False     # nothing to show until a timer arrives
        # Live opacity of each faded window, driven by _step_fade. The menu pair
        # starts at zero: they're withdrawn until the escape menu opens.
        self._alpha = {k: OVERLAY_ALPHA for k in self._fade_win}
        self._alpha["menu"] = self._alpha["hint"] = 0.0
        self._alpha["prompt"] = self._alpha["rift"] = 0.0
        self._fade_secs = {k: FADE_SECS for k in self._fade_win}
        self._fade_secs["menu"] = self._fade_secs["hint"] = MENU_FADE_SECS
        self._fade_secs["prompt"] = MENU_FADE_SECS
        self._fade_job = None          # pending `after` id for the fade driver

        self._build_meter()
        self._build_detail()
        self._build_menu()
        self._build_hint()
        self._build_parse()
        self._build_prompt()
        self._build_rift()
        self._build_minimap()
        self.root.update_idletasks()
        self._place_windows(pos)
        # The control menu and its hint only exist while the game's escape menu
        # is up; _sync_game_ui maps them in. The parse banner is mapped by parse
        # mode itself, and deliberately answers to nothing else — a countdown
        # you can't see is worse than useless.
        self.menu.withdraw()
        self.hintwin.withdraw()
        self.parsewin.withdraw()
        self.promptwin.withdraw()
        self.riftwin.withdraw()
        self.root.after(60, self._apply_clickthrough)
        self._install_hotkeys()

    # ---- persistence ----
    def _load_positions(self):
        """{"meter": (x, y), "detail": (x, y), "menu": (x, y)} — accepts the
        pre-split single-window cache ({"x", "y"}) as the meter position."""
        try:
            d = json.loads(POSITION_CACHE.read_text())
        except Exception:
            return {}
        if "x" in d:
            try:
                return {"meter": (int(d["x"]), int(d["y"]))}
            except Exception:
                return {}
        out = {}
        for key in ("meter", "detail", "menu", "rift", "minimap"):
            try:
                out[key] = (int(d[key]["x"]), int(d[key]["y"]))
            except Exception:
                pass
        return out

    def _pos_visible(self, x, y):
        """True if (x, y) sits within the visible virtual desktop (all monitors),
        so a position saved on a different monitor layout can't hide the window."""
        try:
            if sys.platform == "win32":
                gm = ctypes.windll.user32.GetSystemMetrics
                vx, vy = gm(76), gm(77)          # SM_X/YVIRTUALSCREEN
                vw, vh = gm(78), gm(79)          # SM_CX/CYVIRTUALSCREEN
                return (vx - 10 <= x <= vx + vw - 60 and
                        vy - 10 <= y <= vy + vh - 40)
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            return -10 <= x <= sw - 60 and -10 <= y <= sh - 40
        except Exception:
            return True

    def _place_windows(self, pos):
        m = pos.get("meter")
        if m and self._pos_visible(*m):
            self.root.geometry(f"+{m[0]}+{m[1]}")
        else:
            self._default_meter_pos()
        self.root.update_idletasks()
        d = pos.get("detail")
        if d and self._pos_visible(*d):
            self.detail.geometry(f"+{d[0]}+{d[1]}")
        else:
            self._default_detail_pos()
        rw = pos.get("rift")
        if rw and self._pos_visible(*rw):
            self.riftwin.geometry(f"+{rw[0]}+{rw[1]}")
        else:
            self._default_rift_pos()
        mm = pos.get("minimap")
        if mm and self._pos_visible(*mm):
            self.mapwin.geometry(f"+{mm[0]}+{mm[1]}")
        else:
            self._default_minimap_pos()
        mn = pos.get("menu")
        if mn and self._pos_visible(*mn):
            self.menu.geometry(f"+{mn[0]}+{mn[1]}")
        else:
            self._default_menu_pos()

    def _game_rect(self):
        """Screen rect of the game's own window, so 'centre screen' means the
        monitor the player is actually looking at rather than the primary one.
        Falls back to the primary screen when the window can't be found."""
        rect = _window_rect_of_pid(self.target_pid)
        if rect:
            return rect
        return (0, 0, self.root.winfo_screenwidth(),
                self.root.winfo_screenheight())

    def _default_meter_pos(self):
        sw = self.root.winfo_screenwidth()
        w = max(self.root.winfo_reqwidth(), self.root.winfo_width(), 380)
        self.root.geometry(f"+{sw - w - 12}+12")

    def _default_rift_pos(self):
        """Bottom of the top-centre stack, under the parse banner. Draggable
        from there like everything else, and remembered."""
        self.riftwin.update_idletasks()
        l, t, r, _b = self._game_rect()
        w = max(self.riftwin.winfo_reqwidth(), self.riftwin.winfo_width(), 120)
        self.riftwin.geometry(f"+{l + ((r - l) - w) // 2}+{t + TOP_STRIP_RIFT}")

    def _default_minimap_pos(self):
        """Bottom-left of the game window. The meter stack owns the right side,
        and a minimap wants a corner it can keep."""
        self.mapwin.update_idletasks()
        l, _t, _r, b = self._game_rect()
        h = max(self.mapwin.winfo_reqheight(), self.mapwin.winfo_height(), 200)
        self.mapwin.geometry(f"+{l + 24}+{b - h - 24}")

    def _default_detail_pos(self):
        # Just below the meter, left-aligned with it. The meter is usually
        # EMPTY when this runs (startup / reset positions), so reserve room for
        # it to grow a full party of rows without covering the breakdown.
        self.root.update_idletasks()
        x = self.root.winfo_x()
        h = max(self.root.winfo_reqheight(), self.root.winfo_height(), 240)
        self.detail.geometry(f"+{x}+{self.root.winfo_y() + h + 10}")

    def _default_menu_pos(self):
        self.menu.update_idletasks()
        l, t, r, b = self._game_rect()
        w = max(self.menu.winfo_reqwidth(), self.menu.winfo_width(), 200)
        h = max(self.menu.winfo_reqheight(), self.menu.winfo_height(), 120)
        self.menu.geometry(f"+{l + ((r - l) - w) // 2}+{t + ((b - t) - h) // 2}")

    def _place_hint(self):
        """Top-middle of the game window. Re-run each time it's shown: it has no
        saved position, and the game window may have moved or resized."""
        self.hintwin.update_idletasks()
        l, t, r, _b = self._game_rect()
        w = max(self.hintwin.winfo_reqwidth(), self.hintwin.winfo_width(), 10)
        self.hintwin.geometry(f"+{l + ((r - l) - w) // 2}+{t + TOP_STRIP_HINT}")

    def _save_pos(self):
        try:
            POSITION_CACHE.write_text(json.dumps({
                "meter": {"x": self.root.winfo_x(), "y": self.root.winfo_y()},
                "detail": {"x": self.detail.winfo_x(),
                           "y": self.detail.winfo_y()},
                "menu": {"x": self.menu.winfo_x(), "y": self.menu.winfo_y()},
                "rift": {"x": self.riftwin.winfo_x(),
                         "y": self.riftwin.winfo_y()},
                "minimap": {"x": self.mapwin.winfo_x(),
                            "y": self.mapwin.winfo_y()},
            }))
        except OSError:
            pass

    # ---- UI ----
    def _build_meter(self):
        self.m_border = border = tk.Frame(self.root, bg=BG_BORDER, padx=2, pady=2)
        border.pack(fill="both", expand=True)

        self.header = tk.Frame(border, bg=BG_HEADER)
        self.header.pack(fill="x")
        self.title_lbl = tk.Label(self.header, text="Farever+ Party Meter",
                                  bg=BG_HEADER, fg=FG_HEADER,
                                  font=self.fonts["ui_b"], anchor="w",
                                  padx=8, pady=4)
        self.title_lbl.pack(side="left")
        self.timer_lbl = tk.Label(self.header, text="", bg=BG_HEADER, fg=FG_HEADER,
                                  font=self.fonts["mono"], padx=8)
        self.timer_lbl.pack(side="right")
        self._bind_drag(self.root, (self.header, self.title_lbl))

        self.m_body = body = tk.Frame(border, bg=BG_BODY, padx=8, pady=6)
        body.pack(fill="both", expand=True)

        self.overview_title = tk.Label(body, text="PARTY", bg=BG_BODY,
                                       fg=ACCENT, font=self.fonts["ui_sm_b"],
                                       anchor="w")
        self.overview_title.pack(fill="x")
        # Same font/size as the rows so the monospace columns line up exactly.
        self.cols_lbl = tk.Label(
            body, text=self._meter_cols_text(),
            bg=BG_BODY, fg=FG_DIM, font=self.fonts["mono_10"], anchor="w")
        self.cols_lbl.pack(fill="x", pady=(2, 0))
        self.rows_box = rows_box = tk.Frame(body, bg=BG_BODY)
        rows_box.pack(fill="x", pady=(1, 2))
        # pack stops managing a container the moment its last slave is forgotten
        # — it keeps whatever size it last asked for. Without this 1 px keeper
        # the meter would stay as tall as the biggest party it ever showed once
        # a reset empties the rows. Packed to the bottom so row order is
        # untouched.
        self.rows_keeper = tk.Frame(rows_box, bg=BG_BODY, height=1, width=1)
        self.rows_keeper.pack(side="bottom")
        self.player_rows = [PlayerRow(rows_box, self._on_row_click, self.fonts)
                            for _ in range(MAX_PLAYER_ROWS)]

        self.root.minsize(MIN_W["meter"], 0)

    def _meter_cols_text(self):
        head = f"  #  {'NAME':<12}{'DMG':>9} {'DPS':>6} {'%':>4}"
        return head + (f"{'HEAL':>9}" if self._show["healing"] else "")

    def _build_detail(self):
        self.d_border = border = tk.Frame(self.detail, bg=BG_BORDER, padx=2, pady=2)
        border.pack(fill="both", expand=True)

        self.d_header = tk.Frame(border, bg=BG_HEADER)
        self.d_header.pack(fill="x")
        self.d_title = tk.Label(self.d_header, text="Breakdown",
                                bg=BG_HEADER, fg=FG_HEADER,
                                font=self.fonts["ui_b"], anchor="w",
                                padx=8, pady=4)
        self.d_title.pack(side="left")
        # Sits in the header rather than the body so it reads as a caption on
        # the window instead of another data row. It tints with the header.
        self.d_tip = tk.Label(self.d_header,
                              text="Click a player in the meter to view details",
                              bg=BG_HEADER, fg=FG_HEADER_DIM,
                              font=self.fonts["ui_tiny_i"], anchor="e", padx=8)
        self.d_tip.pack(side="right")
        self._bind_drag(self.detail, (self.d_header, self.d_title, self.d_tip))

        self.d_body = body = tk.Frame(border, bg=BG_BODY, padx=8, pady=6)
        body.pack(fill="both", expand=True)
        self.stats_lbl = tk.Label(body, text="", bg=BG_BODY, fg=FG_TEXT,
                                  font=self.fonts["mono"], anchor="w")
        self.stats_lbl.pack(fill="x")

        self.d_cols = cols = tk.Frame(body, bg=BG_BODY)
        cols.pack(fill="x", pady=(3, 2))
        self.dmg_col = SkillColumn(cols, "DAMAGE", DMG_BAR, self.fonts)
        self.dmg_col.f.pack(side="left", anchor="n")
        # Kept as attributes so the healing toggle can unpack them; re-packing
        # in this order puts them back to the right of the damage column.
        self.col_sep = tk.Frame(cols, bg=BG_BODY_SOFT, width=1)
        self.col_sep.pack(side="left", fill="y", padx=6)
        self.heal_col = SkillColumn(cols, "HEALING", HEAL_BAR, self.fonts)
        self.heal_col.f.pack(side="left", anchor="n")

        self.elem_lbl = tk.Label(body, text="", bg=BG_BODY, fg=FG_DIM,
                                 font=self.fonts["mono_sm"], anchor="w", justify="left")
        self.elem_lbl.pack(fill="x", pady=(3, 0))
        self.detail.minsize(MIN_W["detail"], 0)

    def _build_menu(self):
        """The control menu: what used to be hotkeys, as buttons. Only on screen
        while the game's escape menu is — which is also the only time the game
        has a usable cursor — so it never needs to be click-through."""
        border = tk.Frame(self.menu, bg=BG_BORDER, padx=2, pady=2)
        border.pack(fill="both", expand=True)

        # Green like the other two headers get while it's on screen — the menu
        # only ever exists in the unlocked state, so this never changes.
        self.m_header = tk.Frame(border, bg=BG_HEADER_UNLOCKED)
        self.m_header.pack(fill="x")
        self.m_title = tk.Label(self.m_header, text="Farever+ Controls",
                                bg=BG_HEADER_UNLOCKED, fg=FG_HEADER,
                                font=self.fonts["ui_b"], anchor="w",
                                padx=8, pady=4)
        self.m_title.pack(side="left")
        self._bind_drag(self.menu, (self.m_header, self.m_title))

        body = tk.Frame(border, bg=BG_BODY, padx=8, pady=8)
        body.pack(fill="both", expand=True)

        # Top of the menu, above everything. From a console this is a warning:
        # closing that window kills the process before it can unload the hook and
        # detach, which is what destabilises the game across relaunches, and it's
        # the one way to break things that looks like a normal way to quit. From
        # the installed build there's no such window, so the same line is used to
        # point at the two clean exits instead.
        self.warn_lbl = tk.Label(body,
                                 text=SHUTDOWN_WARNING if HAS_CONSOLE
                                 else SHUTDOWN_HINT,
                                 bg=BG_BODY,
                                 fg=FG_WARN if HAS_CONSOLE else FG_DIM,
                                 font=self.fonts["ui_sm_b"],
                                 anchor="w", justify="left",
                                 wraplength=WARN_WRAP)
        self.warn_lbl.pack(fill="x", pady=(0, 8))
        tk.Frame(body, bg=BG_BAR_TRACK, height=1).pack(fill="x", pady=(0, 2))

        # Two columns rather than one long strip: the menu had grown tall enough
        # to be a scroll, and the scale slider can only make that worse.
        cols = tk.Frame(body, bg=BG_BODY)
        cols.pack(fill="both", expand=True)
        left = tk.Frame(cols, bg=BG_BODY)
        left.pack(side="left", fill="both", expand=True, anchor="n")
        tk.Frame(cols, bg=BG_BAR_TRACK, width=1).pack(side="left", fill="y",
                                                      padx=10)
        right = tk.Frame(cols, bg=BG_BODY)
        right.pack(side="left", fill="both", expand=True, anchor="n")

        def section(parent, text, first=False, note=None):
            row = tk.Frame(parent, bg=BG_BODY)
            row.pack(fill="x", pady=(0 if first else 9, 3))
            tk.Label(row, text=text, bg=BG_BODY, fg=ACCENT,
                     font=self.fonts["ui_sm_b"], anchor="w").pack(side="left")
            if note:
                # Quieter than the heading it hangs off: it's a note about how
                # the section behaves, not another thing to read every time.
                tk.Label(row, text=note, bg=BG_BODY, fg=FG_DIM,
                         font=self.fonts["ui_tiny_i"],
                         anchor="e").pack(side="right")

        def button(parent, cmd):
            b = tk.Button(parent, text="", command=cmd, anchor="w",
                          font=self.fonts["ui"], bg=BG_BODY_SOFT, fg=FG_TEXT,
                          activebackground=BG_BAR_TRACK, activeforeground=FG_VALUE,
                          relief="flat", bd=0, padx=10, pady=5,
                          highlightthickness=1, highlightbackground=BG_BAR_TRACK,
                          cursor="hand2")
            b.pack(fill="x", pady=2)
            return b

        def field(parent, label):
            """A labelled control row, for the things that aren't buttons."""
            row = tk.Frame(parent, bg=BG_BODY)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=label, bg=BG_BODY, fg=FG_TEXT,
                     font=self.fonts["ui"], anchor="w", padx=2).pack(side="left")
            return row

        # Commands are queued rather than run inline: they mutate overlay state
        # the refresh loop also touches, and _drain runs them on the Tk thread.
        section(left, "OPTIONS", first=True)
        self.btn_mode = button(left, self._enqueue(self._toggle_mode))

        row = field(left, "Theme")
        self._theme_var = tk.StringVar(value=self._theme_mode)
        self.opt_theme = tk.OptionMenu(row, self._theme_var, *THEME_MODES,
                                       command=self._on_theme_pick)
        self.opt_theme.config(
            bg=BG_BODY_SOFT, fg=FG_TEXT, activebackground=BG_BAR_TRACK,
            activeforeground=FG_VALUE, relief="flat", bd=0, anchor="w",
            padx=10, pady=3, font=self.fonts["ui"], cursor="hand2",
            highlightthickness=1, highlightbackground=BG_BAR_TRACK,
            direction="right")
        self.opt_theme["menu"].config(
            bg=BG_BODY, fg=FG_TEXT, activebackground=BTN_ON_BG,
            activeforeground=FG_HEADER, bd=0, relief="flat",
            font=self.fonts["ui"])
        self.opt_theme.pack(side="right", expand=True, fill="x", padx=(8, 0))

        row = field(left, "Minimap")
        self._map_mode_var = tk.StringVar(value=self._map_mode)
        self.opt_map = tk.OptionMenu(row, self._map_mode_var, *MINIMAP_MODES,
                                     command=self._on_map_mode_pick)
        self.opt_map.config(
            bg=BG_BODY_SOFT, fg=FG_TEXT, activebackground=BG_BAR_TRACK,
            activeforeground=FG_VALUE, relief="flat", bd=0, anchor="w",
            padx=10, pady=3, font=self.fonts["ui"], cursor="hand2",
            highlightthickness=1, highlightbackground=BG_BAR_TRACK,
            direction="right")
        self.opt_map["menu"].config(
            bg=BG_BODY, fg=FG_TEXT, activebackground=BTN_ON_BG,
            activeforeground=FG_HEADER, bd=0, relief="flat",
            font=self.fonts["ui"])
        self.opt_map.pack(side="right", expand=True, fill="x", padx=(8, 0))

        # Every font in the overlay is a named Tk font, so dragging this resizes
        # the lot. Released rather than live: repainting the whole tree on each
        # pixel of drag is visibly slow.
        row = field(left, "Scale")
        self._scale_var = tk.IntVar(value=100)
        self.scl_ui = tk.Scale(
            row, from_=UI_SCALE_MIN, to=UI_SCALE_MAX, resolution=5,
            orient="horizontal", variable=self._scale_var, showvalue=True,
            bg=BG_BODY, fg=FG_DIM, troughcolor=BG_BAR_TRACK,
            activebackground=BTN_ON_BG, highlightthickness=0, bd=0,
            sliderrelief="flat", font=self.fonts["ui_tiny_i"], length=130,
            cursor="hand2")
        self.scl_ui.bind("<ButtonRelease-1>", self._on_scale_pick)
        self.scl_ui.pack(side="right", expand=True, fill="x", padx=(8, 0))

        section(left, "SHOW / HIDE", note="Always visible when Esc is open")
        self.element_btns = {
            key: button(left, self._enqueue(lambda k=key: self._toggle_element(k)))
            for key, _label in TOGGLEABLE_ELEMENTS
        }
        # Last in this section rather than under OPTIONS: it hides the same
        # windows the checkboxes above do, just on a condition instead of a
        # click.
        self.btn_hide_ooc = button(left, self._enqueue(self._toggle_hide_ooc))

        section(right, "ACTIONS", first=True)
        self.btn_parse = button(right, self._enqueue(self._toggle_parse))
        self.btn_parses = button(right, self._enqueue(self._open_parses))
        self.btn_parses.config(text="Parse Screenshots")

        # Last, and on their own: both throw work away, so they want distance
        # from the settings you click casually.
        section(right, "RESET")
        # Exactly what the hotkey fires, so the two can't diverge. Labelled with
        # the keybind because the hotkey is the one that's useful mid-fight,
        # when the escape menu (and so this button) isn't an option.
        self.btn_reset_data = button(right, self._enqueue(self.session.reset))
        self.btn_reset_data.config(
            text=f"Reset encounter data   ({RESET_HOTKEY_KEYS})")
        self.btn_reset_pos = button(right, self._enqueue(self._reset_pos))
        self.btn_reset_pos.config(text="Reset window positions")

        # Bottom of the menu, on its own: the one button that ends the session.
        # It's here as well as on the tray icon because this is where the user
        # already is — mid-game, escape menu open — and because a tray icon
        # Windows 11 has filed into the overflow flyout is not somewhere you can
        # count on them finding.
        section(right, "QUIT")
        self.btn_quit = button(right, self._enqueue(self._quit_clicked))
        self.btn_quit.config(text=QUIT_LABEL, fg=FG_WARN)
        self.menu.minsize(MIN_W["menu"], 0)

    def _build_hint(self):
        """The one remaining keybind, as free-floating text over the game — no
        panel, no border, just a drop-shadowed line so it stays readable on
        whatever is behind it."""
        self.hint_canvas = tk.Canvas(self.hintwin, bg=TRANSPARENT_KEY,
                                     highlightthickness=0, bd=0)
        self.hint_canvas.pack()
        self._draw_hint()

    def _draw_hint(self):
        """Re-measured rather than sized once, so the scale slider moves it."""
        f, c = self.fonts["ui_hint_b"], self.hint_canvas
        pad, off = 6, 2
        w = f.measure(RESET_HOTKEY_TEXT) + pad * 2 + off
        h = f.metrics("linespace") + pad * 2 + off
        c.config(width=w, height=h)
        c.delete("all")
        c.create_text(pad + off, pad + off, text=RESET_HOTKEY_TEXT, font=f,
                      fill=BG_BORDER, anchor="nw")
        c.create_text(pad, pad, text=RESET_HOTKEY_TEXT, font=f,
                      fill=BG_BODY, anchor="nw")

    def _build_minimap(self):
        """A square, north-up map of what's around you.

        One canvas, redrawn wholesale each tick. That sounds wasteful and isn't:
        at ~40 entities it's a few dozen canvas items, and tracking item
        identity across ticks — entities appear, move and despawn constantly —
        costs more in bookkeeping than it saves in redraws."""
        self.map_border = tk.Frame(self.mapwin, bg=BG_BORDER, padx=2, pady=2)
        self.map_border.pack(fill="both", expand=True)
        self.map_header = tk.Frame(self.map_border, bg=BG_HEADER)
        self.map_header.pack(fill="x")
        self.map_title = tk.Label(self.map_header, text="Nearby", bg=BG_HEADER,
                                  fg=FG_HEADER, font=self.fonts["ui_sm_b"],
                                  anchor="w", padx=6, pady=2)
        self.map_title.pack(side="left")
        self.map_count = tk.Label(self.map_header, text="", bg=BG_HEADER,
                                  fg=FG_HEADER_DIM, font=self.fonts["ui_tiny_i"],
                                  anchor="e", padx=6, pady=2)
        self.map_count.pack(side="right")
        self.map_canvas = tk.Canvas(self.map_border, bg=BG_BODY,
                                    highlightthickness=0, bd=0,
                                    width=MINIMAP_SIZE, height=MINIMAP_SIZE)
        self.map_canvas.pack()
        self._bind_drag(self.mapwin, (self.map_header, self.map_title,
                                      self.map_count))
        # Dragging the map body would fight with the click-to-inspect idea if
        # that ever lands, so only the header moves it — same as the meter.
        self._map_range = MINIMAP_RANGE

    def _minimap_px(self, ex, ey, me, half, scale, rot):
        """World -> canvas, relative to the player.

        The game's +y is drawn as up, which means the canvas y is negated: Tk's
        y grows downward and the world's does not.

        `rot` is (cos r, sin r) in rotating mode and None in fixed mode. The
        offset is turned so the direction the player faces lands at the top of
        the map — which is what lets the arrow stay still.

        Facing is (cos r, sin r), i.e. r is measured from +x. Turning that to
        screen-up is a rotation by (pi/2 - r), which reduces to the form below;
        substituting the facing vector gives (0, 1) as it should."""
        dx, dy = ex - me["x"], ey - me["y"]
        if rot is not None:
            ca, sa = rot
            dx, dy = dx * sa - dy * ca, dx * ca + dy * sa
        return half + dx * scale, half - dy * scale

    def _draw_minimap(self):
        if not self._shown.get("minimap"):
            return
        c = self.map_canvas
        me, ents, _stamp = self.world.read()
        c.delete("all")
        size = int(MINIMAP_SIZE * self._ui_scale)
        if int(c["width"]) != size:
            c.config(width=size, height=size)
        half = size / 2.0
        scale = half / float(self._map_range)

        if not self.world.fresh():
            # Say so rather than showing an empty box: a blank map and a map of
            # an empty area look identical, and only one of them is a problem.
            c.create_text(half, half, text="waiting for the game",
                          fill=FG_DIM, font=self.fonts["ui_tiny_i"])
            self.map_count.config(text="")
            return

        theme = self._theme
        body = theme.get("body", BG_BODY)
        c.configure(bg=body)
        track = theme.get("track", BG_BAR_TRACK)
        mez = me.get("z", 0)
        # Range rings at a third and two thirds, so distances are readable
        # without a scale bar taking up room.
        for frac in (0.34, 0.67):
            r = half * frac
            c.create_oval(half - r, half - r, half + r, half + r,
                          outline=track, width=1)
        # Rings only, no crosshair: the rings carry the distance information on
        # their own, and in rotating mode a crosshair would have to turn with
        # the map, which reads as the whole panel wobbling.

        # Rotating mode turns the world under a fixed arrow; fixed mode leaves
        # the world alone and turns the arrow instead.
        heading = float(me.get("r", 0.0) or 0.0)
        rot = ((math.cos(heading), math.sin(heading))
               if self._map_mode == "Rotating" else None)

        # The minimap always shows everyone nearby, regardless of the meter's
        # party/all mode — a map that hid the player standing next to you would
        # be misleading. Group members are marked with a ring instead.
        local, roster = self.world.who()

        drawn = 0
        by_cat = {}
        for e in ents:
            by_cat.setdefault(e.get("c"), []).append(e)
        for cat in MINIMAP_ORDER:
            style = MINIMAP_STYLE_MAP[cat]
            for e in by_cat.get(cat, ()):
                x, y = self._minimap_px(e.get("x", 0), e.get("y", 0), me,
                                        half, scale, rot)
                if not (0 <= x <= size and 0 <= y <= size):
                    continue        # outside the square; the hook's cull is round
                r = style["r"] * self._ui_scale
                # The local player is drawn last, as an arrow, not a dot.
                if cat == "hero" and e.get("n") and e["n"] == local:
                    continue
                # Faded toward the background rather than hidden when it's on
                # another floor: still there, but visibly not reachable.
                far = abs(e.get("z", mez) - mez) > MINIMAP_Z_FADE
                fill = style["fill"]
                if far:
                    fill = _lerp_hex(body, fill, MINIMAP_Z_DIM)
                self._map_glyph(c, x, y, r, style, fill)
                if cat == "hero" and e.get("n") in roster:
                    # Party members get a ring rather than a different colour:
                    # colour already means category, and overloading it would
                    # make a grouped player read as a different kind of thing.
                    rr = r + 2.5 * self._ui_scale
                    ring = (_lerp_hex(body, MINIMAP_PARTY_RING, MINIMAP_Z_DIM)
                            if far else MINIMAP_PARTY_RING)
                    c.create_oval(x - rr, y - rr, x + rr, y + rr,
                                  outline=ring, width=2)
                drawn += 1

        # Rotating mode already turned the world, so the arrow points at the
        # top of the map. Fixed mode turns the arrow instead: facing is
        # (cos r, sin r) in world, and screen y is inverted.
        if rot is not None:
            self._draw_me_arrow(c, half, 0.0, -1.0)
        else:
            self._draw_me_arrow(c, half, math.cos(heading), -math.sin(heading))
        self.map_count.config(text=f"{drawn}  ·  {int(self._map_range)}u")

    def _map_glyph(self, c, x, y, r, style, fill):
        shape = style["shape"]
        if shape == "dot":
            c.create_oval(x - r, y - r, x + r, y + r, fill=fill, outline="")
        elif shape == "square":
            c.create_rectangle(x - r, y - r, x + r, y + r, fill=fill, outline="")
        else:   # diamond
            c.create_polygon(x, y - r, x + r, y, x, y + r, x - r, y,
                             fill=fill, outline="")

    def _draw_me_arrow(self, c, half, dx, dy):
        """You, at the centre. `dx, dy` is the way you're pointing in SCREEN
        space — already resolved by the caller, because the two modes disagree
        about it: fixed mode turns the arrow, rotating mode turned the world
        instead and leaves the arrow pointing at the top of the map."""
        s = 6.0 * self._ui_scale
        px, py = -dy, dx        # perpendicular, for the two back corners
        tip = (half + dx * 1.4 * s, half + dy * 1.4 * s)
        tail = (half - dx * 0.4 * s, half - dy * 0.4 * s)
        left = (half - dx * s + px * 0.9 * s, half - dy * s + py * 0.9 * s)
        right = (half - dx * s - px * 0.9 * s, half - dy * s - py * 0.9 * s)
        c.create_polygon(*tip, *left, *tail, *right, fill=MINIMAP_ME,
                         outline=BG_BORDER, width=1)

    def _build_rift(self):
        """The next-rift countdown. Styled after the rifts themselves rather
        than the meter — hot magenta rim over a near-black maroon interior — so
        it reads as belonging to the game's event, not to the damage meter.

        The panel sits on a canvas with RIFT_RIPPLE_MARGIN of transparent space
        around it, which is where the pulse's expanding square is drawn. Canvas
        items render behind embedded windows, so the panel covers the middle of
        the ripple and only the part outside it shows."""
        self.rift_canvas = tk.Canvas(self.riftwin, bg=TRANSPARENT_KEY,
                                     highlightthickness=0, bd=0)
        self.rift_canvas.pack()
        m = RIFT_RIPPLE_MARGIN
        self.rift_glow = glow = tk.Frame(self.rift_canvas, bg=RIFT_GLOW,
                                         padx=1, pady=1)
        self.rift_edge = edge = tk.Frame(glow, bg=RIFT_EDGE, padx=2, pady=2)
        edge.pack(fill="both", expand=True)
        self.rift_body = body = tk.Frame(edge, bg=RIFT_BODY,
                                         padx=14, pady=8)
        body.pack(fill="both", expand=True)

        self.rift_title = tk.Label(body, text="NEXT RIFT", bg=RIFT_BODY,
                                   fg=RIFT_TITLE, font=self.fonts["ui_sm_b"],
                                   anchor="w")
        self.rift_title.pack(fill="x")
        self.rift_lbl = tk.Label(body, text="No rift upcoming", bg=RIFT_BODY,
                                 fg=RIFT_TIME, font=self.fonts["ui_idle_i"],
                                 anchor="w")
        self.rift_lbl.pack(fill="x")
        self.rift_canvas.create_window(m, m, anchor="nw", window=glow)
        self._rift_panel = (0, 0)          # last panel size the canvas was cut to
        self._sync_rift_canvas()
        self._bind_drag(self.riftwin, (self.rift_canvas, glow, edge, body,
                                       self.rift_title, self.rift_lbl))

    def _sync_rift_canvas(self):
        """Keep the canvas exactly panel + margin. The panel changes width with
        the text ("No rift upcoming" is far wider than a countdown) and with the
        scale slider, so this is checked rather than set once."""
        self.rift_glow.update_idletasks()
        w = self.rift_glow.winfo_reqwidth()
        h = self.rift_glow.winfo_reqheight()
        if (w, h) == self._rift_panel:
            return
        self._rift_panel = (w, h)
        m = RIFT_RIPPLE_MARGIN
        self.rift_canvas.config(width=w + m * 2, height=h + m * 2)

    def _tick_rift_timer(self):
        """Count down to the top of the hour, which is when rifts open. For the
        first RIFT_QUIET_MINS past it, the rift that just opened is the current
        one — counting 59 minutes to the *next* one then would be misleading."""
        now = time.localtime()
        into_hour = now.tm_min * 60 + now.tm_sec
        if into_hour < RIFT_QUIET_MINS * 60:
            self.rift_title.config(text="RIFT TIMER")
            self.rift_lbl.config(text="No rift upcoming",
                                 font=self.fonts["ui_idle_i"])
            self._set_rift_box(RIFT_BOX_FAR)
            self._set_pulsing(False)
            self._rift_idle = True
            return
        self._rift_idle = False
        left = 3600 - into_hour
        mins, secs = divmod(left, 60)
        self.rift_title.config(text="NEXT RIFT")
        self.rift_lbl.config(text=f"{mins:02d}:{secs:02d}",
                             font=self.fonts["mono_xl_b"])
        # Three stages: ordinary while it's far off, rift-coloured inside 15
        # minutes, pulsing inside 5. Each one is a bigger nudge than the last.
        self._set_rift_box(RIFT_BOX_NEAR if left <= RIFT_STYLE_SECS
                           else RIFT_BOX_FAR)
        self._set_pulsing(left <= RIFT_PULSE_SECS)

    def _set_rift_box(self, style):
        """Repaint the countdown box. Guarded on the current style: this runs
        every tick and reconfiguring five widgets each time is pure waste."""
        if style is self._rift_box:
            return
        self._rift_box = style
        self.rift_glow.config(bg=style["glow"])
        self.rift_edge.config(bg=style["edge"])
        self.rift_body.config(bg=style["body"])
        self.rift_title.config(bg=style["body"], fg=style["title"])
        self.rift_lbl.config(bg=style["body"], fg=style["time"])

    def _set_pulsing(self, on):
        self._rift_pulse = on
        if on and self._pulse_job is None:
            self._pulse_job = self.root.after(RIFT_PULSE_MS, self._step_pulse)

    def _step_pulse(self):
        """Ramp the rim colour up and back on a cosine, so it breathes rather
        than blinks. Stops itself once the countdown is far enough out again, or
        the window isn't on screen to pulse."""
        self._pulse_job = None
        if not self._rift_pulse or not self._shown["rift"]:
            style = self._rift_box or RIFT_BOX_NEAR
            self.rift_glow.config(bg=style["glow"])
            self.rift_edge.config(bg=style["edge"])
            self.rift_title.config(fg=style["title"])
            self.rift_canvas.delete("ripple")
            return
        phase = (time.monotonic() % RIFT_PULSE_PERIOD) / RIFT_PULSE_PERIOD
        k = (1 - math.cos(phase * 2 * math.pi)) / 2
        self.rift_edge.config(bg=_lerp_hex(RIFT_EDGE, RIFT_PEAK, k))
        self.rift_glow.config(bg=_lerp_hex(RIFT_GLOW, RIFT_EDGE, k))
        self.rift_title.config(fg=_lerp_hex(RIFT_TITLE, "#FFFFFF", k))
        self._draw_ripple(phase)
        self._pulse_job = self.root.after(RIFT_PULSE_MS, self._step_pulse)

    def _draw_ripple(self, phase):
        """One square, expanding out of the panel's edge and thinning as it
        goes. Tk canvas has no alpha, so the fade is done with `outlinestipple`
        — progressively sparser dither patterns let more of the game through,
        which over a transparent-key canvas reads as fading out."""
        self._sync_rift_canvas()
        c, m = self.rift_canvas, RIFT_RIPPLE_MARGIN
        c.delete("ripple")
        pw, ph = self._rift_panel
        if not pw:
            return
        # It has to be gone *before* it reaches the canvas edge. Run it to the
        # boundary and Tk clips the outline into a hard rectangle sitting on the
        # window's rim, which reads as a permanent border the ripple flies out
        # to rather than something dissipating.
        if phase > RIFT_RIPPLE_FADEOUT:
            return
        travel = phase / RIFT_RIPPLE_FADEOUT      # 0..1 over the visible part
        out = (m - 3) * travel
        x0, y0 = m - out, m - out
        x1, y1 = m + pw + out, m + ph + out
        stipple = ("", "gray75", "gray50", "gray25", "gray12")[
            min(4, int(travel * 5))]
        c.create_rectangle(x0, y0, x1, y1, outline=RIFT_PEAK, width=2,
                           outlinestipple=stipple, tags="ripple")

    def _build_prompt(self):
        """The rift prompts. Styled like the rifts rather than the meter — they
        only ever appear because of one, and the colour is what tells you at a
        glance which of the two questions you're being asked isn't a meter
        setting."""
        glow = tk.Frame(self.promptwin, bg=RIFT_GLOW, padx=1, pady=1)
        glow.pack(fill="both", expand=True)
        border = tk.Frame(glow, bg=RIFT_EDGE, padx=2, pady=2)
        border.pack(fill="both", expand=True)
        header = tk.Frame(border, bg=RIFT_GLOW)
        header.pack(fill="x")
        tk.Label(header, text="RIFT", bg=RIFT_GLOW, fg=RIFT_TIME,
                 font=self.fonts["ui_b"], anchor="w",
                 padx=12, pady=6).pack(side="left")

        body = tk.Frame(border, bg=RIFT_BODY, padx=18, pady=16)
        body.pack(fill="both", expand=True)
        self.prompt_title = tk.Label(body, text="", bg=RIFT_BODY, fg=RIFT_TIME,
                                     font=self.fonts["ui_lg_b"], anchor="w")
        self.prompt_title.pack(fill="x")
        self.prompt_question = tk.Label(body, text="", bg=RIFT_BODY,
                                        fg=RIFT_TITLE, font=self.fonts["ui_10"],
                                        anchor="w", pady=8)
        self.prompt_question.pack(fill="x")

        btns = tk.Frame(body, bg=RIFT_BODY)
        btns.pack(fill="x", pady=(10, 0))

        def answer_button(text, on, yes):
            b = tk.Button(btns, text=text, command=on,
                          font=self.fonts["ui_b"],
                          bg=RIFT_EDGE if yes else RIFT_BODY,
                          fg="#2C0A1E" if yes else RIFT_TITLE,
                          activebackground=RIFT_TITLE if yes else RIFT_GLOW,
                          activeforeground="#2C0A1E" if yes else RIFT_TIME,
                          relief="flat", bd=0, padx=30, pady=8,
                          highlightthickness=1, cursor="hand2",
                          highlightbackground=RIFT_EDGE)
            b.pack(side="left", expand=True, fill="x", padx=4)
            return b

        answer_button("Yes", self._enqueue(lambda: self._answer_rift(True)), True)
        answer_button("No", self._enqueue(lambda: self._answer_rift(False)), False)
        self.promptwin.minsize(MIN_W["prompt"], 0)

    def _open_rift_prompt(self, kind):
        self._prompt_kind = kind
        if kind == "enter":
            self.prompt_title.config(text="You have entered a rift")
            self.prompt_question.config(text="Enable 'View All Players'?")
        else:
            self.prompt_title.config(text="You have left the rift")
            self.prompt_question.config(text="Return to viewing party members only?")
        self._prompt_open = True
        self.promptwin.update_idletasks()
        l, t, r, b = self._game_rect()
        w = max(self.promptwin.winfo_reqwidth(), 300)
        h = max(self.promptwin.winfo_reqheight(), 120)
        self.promptwin.geometry(f"+{l + ((r - l) - w) // 2}+{t + ((b - t) - h) // 2}")
        self._apply_clickthrough()      # the prompt has to be clickable
        self._refresh_visibility()
        print(f"[meter] {'entered' if kind == 'enter' else 'left'} a rift — "
              "asking about the player view.", file=sys.stderr)

    def _close_rift_prompt(self):
        self._prompt_open = False
        self._refresh_visibility()

    def _answer_rift(self, yes):
        """Yes switches the view — to all-players on entering a rift, back to
        party-only on leaving. Either way that resets the encounter, the same
        as the mode button: the two views can't share one encounter without the
        percentages lying."""
        want = "all" if self._prompt_kind == "enter" else "party"
        if yes and self.mode != want:
            self.mode = want
            self.focus_player = None
            self.session.reset()
        self._close_rift_prompt()
        print(f"[meter] rift prompt answered: {'yes' if yes else 'no'}",
              file=sys.stderr)

    def _tick_rift(self):
        """One prompt per rift entry, and it goes away by itself if the rift
        does — an unanswered box shouldn't outlive what it was asking about."""
        in_rift = self.ui_state.in_rift()
        if in_rift and not self._rift_seen:
            self._rift_seen = True
            self._open_rift_prompt("enter")
        elif not in_rift and self._rift_seen:
            # Leaving replaces the question rather than just dismissing it: the
            # all-players view you switched on for the rift is the wrong one to
            # be left holding once you're back outside.
            self._rift_seen = False
            self._open_rift_prompt("leave")

    def _build_parse(self):
        """The parse banner — the same drop-shadowed floating text as the
        keybind hint, but its content changes every second, so the canvas and
        the window are re-measured on each update instead of sized once."""
        self._parse_font = self.fonts["ui_parse_b"]
        self._parse_canvas = tk.Canvas(self.parsewin, bg=TRANSPARENT_KEY,
                                       highlightthickness=0, bd=0)
        self._parse_canvas.pack()
        self._parse_text = None        # last text drawn, to skip redundant work

    def _set_parse_banner(self, text, fill=BG_BODY):
        """Draw `text` centred over the top of the game window. Cheap to call
        every tick: unchanged text redraws nothing (and re-measuring costs a
        game-window lookup, which is why that matters)."""
        if text == self._parse_text:
            return
        self._parse_text = text
        f, c = self._parse_font, self._parse_canvas
        pad, off = 8, 2
        w = f.measure(text) + pad * 2 + off
        h = f.metrics("linespace") + pad * 2 + off
        c.config(width=w, height=h)
        c.delete("all")
        c.create_text(pad + off, pad + off, text=text, font=f, fill=BG_BORDER,
                      anchor="nw")
        c.create_text(pad, pad, text=text, font=f, fill=fill, anchor="nw")
        self.parsewin.update_idletasks()
        l, t, r, _b = self._game_rect()
        # Below the keybind hint, which shares this strip whenever the escape
        # menu is open — and it is, for the first few seconds of a parse.
        self.parsewin.geometry(f"+{l + ((r - l) - w) // 2}+{t + TOP_STRIP_PARSE}")

    def _hide_parse_banner(self):
        self._parse_text = None
        self.parsewin.withdraw()

    def _toggle_parse(self):
        if self._parse_state is None:
            self._parse_state = "countdown"
            self._parse_until = time.time() + PARSE_PREROLL_SECS
            self.parsewin.deiconify()
            self.parsewin.attributes("-topmost", True)
            self._set_parse_banner(f"PARSE STARTS IN {PARSE_PREROLL_SECS}")
        else:
            self._stop_parse()

    def _begin_parse(self, now):
        """Pre-roll over: clear the meter and start the fixed-length sample."""
        self.session.reset()
        self.session.set_capture_window(PARSE_LENGTH_SECS)
        # Our own reset, so don't let the epoch watcher read it as the player
        # resetting out of parse mode.
        self._last_epoch = self.session.epoch
        self.focus_player = None
        self._parse_state = "parsing"
        self._parse_until = now + PARSE_LENGTH_SECS
        self._set_parse_banner(f"PARSE  {PARSE_LENGTH_SECS}s")

    def _finish_parse(self):
        """Nothing to switch off: the session's capture window has already
        elapsed, which stops both new data and the duration clock. This just
        moves the UI into its 'sample is sitting there to be read' state, and
        writes the result out before anything can clear it."""
        self._parse_state = "done"
        self._set_parse_banner(f"PARSE COMPLETE  {PARSE_LENGTH_SECS}s",
                               fill=BG_HEADER_UNLOCKED)
        self._save_parse_image()

    def _parse_snapshot(self):
        """Everything the image needs, as plain data — same rows, same focus and
        the same merged skill tables the overlay is displaying."""
        duration, _ = self.session.current()
        rows = self._apply_mode(self.session.snapshot()[2])
        party_total = sum(p.total for p in rows)
        focus_name = self._resolve_focus(rows)
        fp = next((p for p in rows if p.name == focus_name), None)

        focus = None
        if fp is not None:
            fdps = fp.total / duration if duration > 0 else 0.0
            crit_pct = (fp.crits / fp.hits * 100) if fp.hits else 0.0
            stats = [f"{int(fp.total)} dmg", f"{fdps:.0f} dps",
                     f"{fp.hits} hits", f"{crit_pct:.0f}% crit",
                     f"{int(fp.heal_total)} heal"]
            if fp.kills:
                stats.append(f"{fp.kills} kills")
            el = sorted(fp.elements.items(), key=lambda kv: -kv[1][1])
            focus = {
                "name": fp.name,
                "total": fp.total,
                "heal": fp.heal_total,
                "stats": " · ".join(stats),
                "skills": self._merge_named(fp.skills),
                "heals": self._merge_named(fp.heals),
                "elements": "  ".join(f"{k}:{int(v[1])}" for k, v in el[:6]),
            }
        return {
            "title": f"Farever+ {PARSE_LENGTH_SECS}s Parse",
            "when": time.strftime("%Y-%m-%d %H:%M"),
            "duration": duration,
            "mode": "PARTY" if self.mode == "party" else "ALL PLAYERS",
            "rows": [{
                "name": p.name, "total": p.total, "heal": p.heal_total,
                "dps": p.total / duration if duration > 0 else 0.0,
                "pct": (p.total / party_total * 100) if party_total else 0.0,
                "is_me": p.is_me,
            } for p in rows],
            "focus": focus,
        }

    def _open_parses(self):
        """Open the parse folder in Explorer. Created on demand, so the button
        does something sensible before the first parse has ever been saved
        rather than failing on a folder that doesn't exist yet."""
        try:
            PARSES_DIR.mkdir(parents=True, exist_ok=True)
            os.startfile(PARSES_DIR)
        except Exception as e:
            print(f"[meter] couldn't open {PARSES_DIR}: {e}", file=sys.stderr)

    def _save_parse_image(self):
        """Write the finished parse to parses/. Never fatal: a missing Pillow or
        an unwritable folder costs you the picture, not the parse that's sitting
        on screen."""
        try:
            name = f"parse-{time.strftime('%Y%m%d-%H%M%S')}.png"
            out = render_parse_image(self._parse_snapshot(), PARSES_DIR / name)
            print(f"[meter] parse saved to {out}", file=sys.stderr)
        except ImportError:
            print("[meter] parse image skipped — Pillow isn't installed "
                  "(pip install pillow).", file=sys.stderr)
        except Exception as e:
            print(f"[meter] couldn't save the parse image: {e}", file=sys.stderr)

    def _stop_parse(self):
        """Back to live metering — which clears the sample. Resuming capture
        into a finished parse would quietly append live hits to the numbers you
        were reading, so leaving them would be worse than dropping them."""
        self._parse_state = None
        self._hide_parse_banner()
        self.session.reset()
        self._last_epoch = self.session.epoch
        self.focus_player = None

    def _tick_parse(self):
        """Drive the countdown from the refresh loop. Only the phase changes
        matter for correctness — the exact 60 s cutoff is enforced inside the
        session, not here, so a late tick can't lengthen the sample."""
        if self._parse_state is None or self._parse_state == "done":
            return
        now = time.time()
        left = self._parse_until - now
        if self._parse_state == "countdown":
            if left <= 0:
                self._begin_parse(now)
            else:
                self._set_parse_banner(f"PARSE STARTS IN {math.ceil(left)}")
        elif self._parse_state == "parsing":
            if left <= 0:
                self._finish_parse()
            else:
                self._set_parse_banner(f"PARSE  {math.ceil(left)}s")

    # ---- drag / lock ----
    def _is_locked(self):
        """Click-through unless the game is showing a cursor-freeing window
        (its escape menu). The game's UI state is the only input."""
        return not self._menu_unlock

    def _bind_drag(self, win, widgets):
        """Drag any of `widgets` to move `win` (while unlocked)."""
        state = {}

        def start(e):
            if self._is_locked():
                return
            state["dx"] = e.x_root - win.winfo_x()
            state["dy"] = e.y_root - win.winfo_y()
            state["on"] = True

        def move(e):
            if self._is_locked() or not state.get("on"):
                return
            win.geometry(f"+{e.x_root - state['dx']}+{e.y_root - state['dy']}")

        def end(e):
            if state.pop("on", None):
                self._save_pos()

        for w in widgets:
            w.bind("<Button-1>", start)
            w.bind("<B1-Motion>", move)
            w.bind("<ButtonRelease-1>", end)

    def _on_row_click(self, name):
        if not self._is_locked() and name:
            self.focus_player = name

    def _set_win_clickthrough(self, win, enabled):
        if sys.platform != "win32":
            return
        hwnd = win.winfo_id()
        parent = ctypes.windll.user32.GetParent(hwnd)
        _set_clickthrough(parent or hwnd, enabled)

    def _round_win_corners(self, win):
        """Round one window, on the wrapper hwnd click-through also targets.
        DWM keeps the shape as the window resizes, so this only needs redoing
        when the hwnd itself is replaced — see _on_map_round."""
        if sys.platform != "win32":
            return
        hwnd = win.winfo_id()
        parent = ctypes.windll.user32.GetParent(hwnd)
        _set_rounded_corners(parent or hwnd)

    def _on_map_round(self, event):
        # <Map> fires for the initial show, for every deiconify, and after Tk
        # swaps a toplevel's wrapper — i.e. exactly when the rounding needs
        # reasserting. Child widgets bubble their own <Map> here, so only act
        # on the toplevel's.
        win = event.widget
        if not isinstance(win, (tk.Tk, tk.Toplevel)):
            return
        # Not the rift timer: its window is mostly transparent canvas around a
        # much smaller panel, so DWM's rounding has nothing to round — it just
        # leaves a faint border floating out where the ripple ends. The panel
        # draws its own edges.
        if win is self.riftwin:
            return
        self._round_win_corners(win)

    def _apply_clickthrough(self):
        locked = self._is_locked()
        for win in (self.root, self.detail, self.riftwin):
            self._set_win_clickthrough(win, locked)
        # The control menu is always interactive (it is only ever shown while
        # the cursor is free); the floating hint and parse banner are text over
        # the game and must never take a click.
        self._set_win_clickthrough(self.menu, False)
        self._set_win_clickthrough(self.hintwin, True)
        self._set_win_clickthrough(self.parsewin, True)
        # The prompt must take clicks whenever it's up, regardless of lock
        # state — it's the one overlay window that has to be answered.
        self._set_win_clickthrough(self.promptwin, False)

    def _sync_game_ui(self):
        """Follow the game's UI: while a cursor-freeing window (escape menu) is
        open the overlay becomes interactive and the control menu appears."""
        want = self.ui_state.any_open(UNLOCK_ON_WINDOWS)
        if want == self._menu_unlock:
            return
        self._menu_unlock = want
        self._apply_clickthrough()
        self._refresh_visibility()   # owns every window's target, menu included
        if want and not self._prompt_open:
            self._place_hint()       # after the map: it measures the window
        # Logged because it's the one state change with no keypress behind it —
        # if someone reports "the meter won't take my clicks", this line says
        # whether the game-menu signal is arriving at all.
        print(f"[meter] game menu {'open' if want else 'closed'} — overlay "
              f"{'unlocked' if not self._is_locked() else 'locked'}",
              file=sys.stderr)

    # ---- actions ----
    def _enqueue(self, fn):
        """Wrap `fn` so it runs on the Tk thread at the next refresh. Hotkey and
        mouse-hook callbacks arrive on their own threads, and Tk is not
        thread-safe; menu buttons use it too so every action takes one path."""
        def handler():
            with self._q_lock:
                self._action_q.append(fn)
        return handler

    def _apply_update_notice(self):
        """Turn the control menu's top line into an update notice, once.

        Polled from the refresh tick rather than pushed by the checker, because
        the check runs on its own thread and Tk is not thread-safe. It replaces
        the shutdown hint — that line has done its job by the time someone has
        the menu open, and a new version is the more useful thing to say."""
        if self._update_shown or not UPDATE["latest"]:
            return
        self._update_shown = True
        self.warn_lbl.config(
            text=f"Farever+ {UPDATE['latest']} is available — you're running "
                 f"{VERSION}.  Click here to download it.",
            fg=FG_WARN, cursor="hand2")
        self.warn_lbl.bind("<Button-1>", lambda _e: self._open_update())

    def _open_update(self):
        import webbrowser
        try:
            webbrowser.open(UPDATE["url"])
        except Exception as e:
            print(f"[update] couldn't open {UPDATE['url']}: {e}",
                  file=sys.stderr)

    def request_quit(self):
        """Stop the meter, safely callable from any thread — the tray icon runs
        on its own. Routed through the action queue so the quit itself happens
        on the Tk thread like every other action."""
        self._enqueue(self._quit)()

    def _quit(self):
        """quit(), not destroy(): returning from the mainloop hands control back
        to main()'s finally, which is what unloads the hook and detaches. That
        ordering is the entire point of having a Quit button at all."""
        print("[meter] stop requested — shutting down.", file=sys.stderr)
        self.root.quit()

    def _quit_clicked(self):
        """Two clicks to quit. The button sits in the same menu as the display
        toggles, and a misclick that ends the meter mid-fight — taking the
        encounter with it — is worth one extra click to rule out."""
        if self._quit_armed:
            self._quit()
            return
        self._quit_armed = True
        self.btn_quit.config(text="Click again to stop", bg=FG_WARN,
                             fg=FG_HEADER, activebackground=FG_WARN,
                             activeforeground=FG_HEADER)
        self.root.after(4000, self._disarm_quit)

    def _disarm_quit(self):
        self._quit_armed = False
        try:
            self.btn_quit.config(text=QUIT_LABEL, bg=BG_BODY_SOFT, fg=FG_WARN,
                                 activebackground=BG_BAR_TRACK,
                                 activeforeground=FG_VALUE)
        except tk.TclError:
            pass        # the window went away while the timer was pending

    def _install_hotkeys(self):
        start_hotkeys({HK_RESET: self._enqueue(self.session.reset)},
                      self.target_pid)

    def _drain(self):
        with self._q_lock:
            q, self._action_q = self._action_q, []
        for fn in q:
            try:
                fn()
            except Exception as e:
                print("[action]", e, file=sys.stderr)

    def _toggle_element(self, key):
        """Show/hide one overlay element. The control menu itself is never in
        TOGGLEABLE_ELEMENTS — it's how you get the others back."""
        self._show[key] = not self._show[key]
        self._refresh_visibility()

    def _on_scale_pick(self, _event=None):
        self._enqueue(lambda: self._set_ui_scale(self._scale_var.get() / 100))()

    def _set_ui_scale(self, factor):
        """Resize every named font, which resizes every window that packs to its
        content. The two canvas-drawn banners measure their text at draw time,
        so they're re-drawn rather than left at the old size."""
        if abs(factor - self._ui_scale) < 0.001:
            return
        self._ui_scale = factor
        for key, (_family, size, *_style) in FONT_SPECS.items():
            self.fonts[key].configure(size=max(6, round(size * factor)))
        self._parse_text = None          # force the parse banner to re-measure
        self._draw_hint()
        # Pixel floors and wrap widths don't come along for free.
        for win, key in ((self.root, "meter"), (self.detail, "detail"),
                         (self.menu, "menu"), (self.promptwin, "prompt")):
            win.minsize(int(MIN_W[key] * factor), 0)
        self.warn_lbl.config(wraplength=int(WARN_WRAP * factor))
        self.root.update_idletasks()
        print(f"[meter] UI scale {factor:.2f}x", file=sys.stderr)

    def _on_theme_pick(self, value):
        # Queued like every other menu action: it mutates state the refresh
        # loop reads, and Tk isn't thread-safe.
        self._enqueue(lambda: self._set_theme_mode(value))()

    def _on_map_mode_pick(self, value):
        # Queued for the same reason as the theme pick: the draw pass reads it.
        self._enqueue(lambda: setattr(self, "_map_mode", value))()

    def _toggle_hide_ooc(self):
        self._hide_ooc = not self._hide_ooc
        self._refresh_visibility()

    def _refresh_visibility(self):
        """Fade each element in/out from its own show/hide setting plus the two
        global rules: "hide out of combat", and hiding behind the game's own
        screens.

        Out-of-combat hiding keeps things up for a few seconds after the
        fighting stops (HIDE_OOC_LINGER_SECS) — the game's isInCombat flag drops
        between pulls, and without the grace period the overlay would flicker
        away and back through a trash pack.

        The escape menu overrides all of it — including a window you ticked off
        yourself. Being able to see what a checkbox does while you're clicking
        it matters more than honouring the setting for those few seconds, and it
        means the control menu is never the only thing on screen."""
        ooc_hidden = (self._hide_ooc and not self._menu_unlock and
                      (time.time() - self._combat_seen_at) >= HIDE_OOC_LINGER_SECS)
        # Any game window that isn't the escape menu (inventory, map, ...) owns
        # the screen while it's up — see MENU_IGNORE_WINDOWS. Unlike the OOC
        # rule this is unconditional: it isn't a setting the player can untick.
        menu_hidden = (not self._menu_unlock and
                       self.ui_state.any_open_except(MENU_IGNORE_WINDOWS))
        # The rift prompt is modal: while it's up nothing else is on screen, not
        # even the control menu. That's deliberate — it leaves Esc free to hand
        # the cursor back so the question can actually be clicked.
        blanket = menu_hidden or self._prompt_open
        changed = False
        for key in self._element_win:
            hidden = blanket or (ooc_hidden and key not in OOC_EXEMPT)
            want = (self._show[key] or self._menu_unlock) and not hidden
            # No countdown while you're inside a rift: you're in the thing it
            # was counting down to.
            if key == "rift" and self.ui_state.in_rift():
                want = False
            # ...nor while there's nothing to count. "No rift upcoming" is true
            # for six minutes of every hour and is not worth a panel; the
            # escape menu still brings it back, like every other hidden thing,
            # so the Show/hide tick can be seen to do something.
            if key == "rift" and self._rift_idle and not self._menu_unlock:
                want = False
            changed |= self._want_visible(key, want)
        menu_visible = self._menu_unlock and not self._prompt_open
        changed |= self._want_visible("menu", menu_visible)
        changed |= self._want_visible("hint", menu_visible)
        changed |= self._want_visible("prompt", self._prompt_open)
        if changed:
            self._start_fade()

    def _pick_theme(self):
        """Dynamic follows the game; the other two are pinned. Either way the
        escape menu wins: the control menu is Farever-styled and the meter sits
        right next to it, so matching that beats matching a rift you can't
        currently see."""
        if self._menu_unlock:
            return THEME_DEFAULT
        if self._theme_mode == "Farever":
            return THEME_DEFAULT
        if self._theme_mode == "Rift":
            return THEME_RIFT
        return THEME_RIFT if self.ui_state.in_rift() else THEME_DEFAULT

    def _set_theme_mode(self, mode):
        self._theme_mode = mode

    def _apply_theme(self, t):
        """Repaint the meter and breakdown into `t`. Same widget tree either
        way — nothing is rebuilt, so this is safe to call mid-combat."""
        self._theme = t
        for w in (self.m_border, self.d_border):
            w.config(bg=t["border"])
        for w in (self.m_body, self.rows_box, self.rows_keeper, self.d_body,
                  self.d_cols):
            w.config(bg=t["body"])
        self.overview_title.config(bg=t["body"], fg=t["accent"])
        self.cols_lbl.config(bg=t["body"], fg=t["fg_dim"])
        self.stats_lbl.config(bg=t["body"], fg=t["fg_text"])
        self.elem_lbl.config(bg=t["body"], fg=t["fg_dim"])
        self.col_sep.config(bg=t["soft"])
        for row in self.player_rows:
            row.theme = t
            row.set_theme(t)
        for col in (self.dmg_col, self.heal_col):
            col.set_theme(t)
        # The minimap follows too. Its canvas contents are redrawn from scratch
        # on the next tick and read self._theme directly, so only the chrome
        # needs repainting here.
        self.map_border.config(bg=t["border"])
        self.map_header.config(bg=t["header"])
        self.map_title.config(bg=t["header"], fg=t["fg_header"])
        self.map_count.config(bg=t["header"], fg=t["fg_header_dim"])
        # Force the header tint to be re-pushed: its guard compares against the
        # last colour applied, which belongs to the theme we just left.
        self._header_bg = None

    def _want_visible(self, key, visible):
        """Point one faded window at a target state, returning whether that
        changed anything. Mapping happens here, at whatever opacity the fade
        last left it (0 for a fully hidden window, mid-fade for one caught on
        the way out); unmapping happens in _step_fade once it reaches zero."""
        if visible == self._shown[key]:
            return False
        self._shown[key] = visible
        if visible:
            win = self._fade_win[key]
            win.attributes("-alpha", self._alpha[key])
            win.deiconify()
            win.attributes("-topmost", True)   # re-assert over the game's UI
        return True

    def _start_fade(self):
        if self._fade_job is None:
            self._fade_job = self.root.after(FADE_STEP_MS, self._step_fade)

    def _step_fade(self):
        """Walk every faded window one step towards its target opacity, and
        unmap it once it reaches zero. Reversing mid-fade needs no special
        handling: the target flips and the next step walks back from here."""
        self._fade_job = None
        fading = False
        for key, win in self._fade_win.items():
            target = OVERLAY_ALPHA if self._shown[key] else 0.0
            a = self._alpha[key]
            if a == target:
                continue
            step = OVERLAY_ALPHA * FADE_STEP_MS / (self._fade_secs[key] * 1000)
            a = min(target, a + step) if target > a else max(target, a - step)
            self._alpha[key] = a
            win.attributes("-alpha", a)
            if a <= 0.0:
                win.withdraw()
            else:
                fading = True
        if fading:
            self._fade_job = self.root.after(FADE_STEP_MS, self._step_fade)

    def _reset_pos(self):
        try:
            POSITION_CACHE.unlink()
        except OSError:
            pass
        self._default_meter_pos()
        self._default_detail_pos()
        self._default_menu_pos()
        self._default_rift_pos()

    def _toggle_mode(self):
        self.mode = "all" if self.mode == "party" else "party"
        self.focus_player = None
        self.session.reset()

    # ---- render ----
    def _apply_mode(self, rows):
        if self.mode == "party":
            party = [p for p in rows if p.in_party]
            # If we haven't identified any party members yet, fall back to me so
            # the meter isn't blank (e.g. solo, or group not read yet).
            return party if party else [p for p in rows if p.is_me]
        return rows

    def _merge_named(self, table):
        """Merge a skill-id table (id -> [hits, total, crits]) by display name
        (e.g. all weapons' base "Attack" share one row) and return
        [(label, total, hits, crits)] sorted by total desc."""
        merged: dict[str, list] = defaultdict(lambda: [0, 0.0, 0])
        for sid, (h, tot, cr) in table.items():
            label = self.session.skill_names.get(sid) or _pretty_id(sid)
            m = merged[label]
            m[0] += h; m[1] += tot; m[2] += cr
        out = [(label, v[1], v[0], v[2]) for label, v in merged.items()]
        out.sort(key=lambda t: -t[1])
        return out[:MAX_SKILL_ROWS]

    def _apply_heal_columns(self):
        """Show/hide every healing-specific piece of chrome in one go: the
        meter's HEAL column header, and the breakdown's HEALING list with its
        divider. Guarded on the last applied state — re-packing widgets on every
        250 ms tick would flicker."""
        show = self._show["healing"]
        if show == self._heal_cols_shown:
            return
        self._heal_cols_shown = show
        self.cols_lbl.config(text=self._meter_cols_text())
        if show:
            self.col_sep.pack(side="left", fill="y", padx=6)
            self.heal_col.f.pack(side="left", anchor="n")
        else:
            self.col_sep.pack_forget()
            self.heal_col.f.pack_forget()

    def _refresh_menu(self):
        """Menu buttons are labelled with what they'll *do*, so they double as
        the state readout the old hint line used to provide."""
        for key, label in TOGGLEABLE_ELEMENTS:
            self.element_btns[key].config(
                text=("☑  " if self._show[key] else "☐  ") + label)
        # A standing setting, so this one shows its state rather than its action.
        self.btn_hide_ooc.config(
            text=("☑  Hide out of combat" if self._hide_ooc
                  else "☐  Hide out of combat"))
        # Labelled with the action, but tinted by the *state*: green while
        # all-players is the live mode, so it's obvious at a glance that the
        # meter is showing more than the group. Switching either way calls
        # session.reset(), so the label warns about that up front rather than
        # silently binning the encounter mid-fight.
        # Tinted while a parse is live for the same reason the mode button is:
        # it's a state you can forget you're in, and the meter looks normal.
        parsing = self._parse_state is not None
        self.btn_parse.config(
            text=(f"Stop {PARSE_LENGTH_SECS}s Parse" if parsing
                  else f"{PARSE_LENGTH_SECS}s Parse Mode"),
            bg=BTN_ON_BG if parsing else BG_BODY_SOFT,
            fg=FG_HEADER if parsing else FG_TEXT,
            activebackground=BTN_ON_BG_ACTIVE if parsing else BG_BAR_TRACK,
            activeforeground=FG_HEADER if parsing else FG_VALUE,
            highlightbackground=BTN_ON_BG_ACTIVE if parsing else BG_BAR_TRACK)

        all_players = self.mode == "all"
        self.btn_mode.config(
            text=("Show party only" if all_players else "Show all players")
                 + "   (resets data)",
            bg=BTN_ON_BG if all_players else BG_BODY_SOFT,
            fg=FG_HEADER if all_players else FG_TEXT,
            activebackground=BTN_ON_BG_ACTIVE if all_players else BG_BAR_TRACK,
            activeforeground=FG_HEADER if all_players else FG_VALUE,
            highlightbackground=BTN_ON_BG_ACTIVE if all_players else BG_BAR_TRACK)

    def _refresh(self):
        self._drain()
        self._apply_update_notice()
        # Before the epoch check below: starting a parse resets the session
        # itself, and syncs _last_epoch so that isn't mistaken for the player
        # resetting back out of parse mode.
        self._tick_rift()
        self._tick_rift_timer()
        self._tick_parse()
        # Ahead of _refresh_menu, so a reset that drops parse mode is reflected
        # in the button on the same tick rather than the next one.
        if self.session.epoch != self._last_epoch:
            self._last_epoch = self.session.epoch
            self.focus_player = None    # snap the breakdown back to my hero
            # A reset from anywhere else — the hotkey, the menu button, a zone
            # change — drops parse mode too: the sample it was building is gone.
            if self._parse_state is not None:
                self._parse_state = None
                self._hide_parse_banner()
        self._sync_game_ui()
        self._refresh_menu()
        self._apply_heal_columns()
        _, _, rows = self.session.snapshot()
        rows = self._apply_mode(rows)
        # The capture clock only advances while at least one *displayed*
        # (mode-filtered) player is in combat, per the game's isInCombat state.
        active = any(self.session.combat_of(p.name) for p in rows)
        self.session.set_active(active, time.time())
        duration, in_combat = self.session.current()
        if in_combat:
            self._combat_seen_at = time.time()
        self._refresh_visibility()

        # The header BAR carries the state; the header TEXT never changes, so a
        # screenshot always says what the overlay is. Green = unlocked (the
        # game's escape menu is open), orange = in combat, teal = idle. Unlocked
        # wins over combat: it's the transient one, and combat is already
        # obvious from the running timer.
        theme = self._pick_theme()
        if theme is not self._theme:
            self._apply_theme(theme)
        unlocked = not self._is_locked()
        live_bg = (theme["header_unlocked"] if unlocked
                   else theme["header_combat"] if in_combat else theme["header"])
        # A window you've ticked off still shows while the escape menu is open,
        # which makes "off" hard to see. Greying its header is the tell — it's
        # the only part of the window that carries state anyway.
        want = tuple(theme["header_off"] if not self._show[k] else live_bg
                     for k in ("meter", "detail"))
        if want != self._header_bg:
            meter_bg, detail_bg = want
            for w in (self.header, self.title_lbl, self.timer_lbl):
                w.config(bg=meter_bg)
            for w in (self.d_header, self.d_title, self.d_tip):
                w.config(bg=detail_bg)
            for w in (self.title_lbl, self.timer_lbl, self.d_title):
                w.config(fg=theme["fg_header"])
            self.d_tip.config(fg=theme["fg_header_dim"])
            self._header_bg = want

        mins, secs = divmod(int(duration), 60)
        party_total = sum(p.total for p in rows)
        self.timer_lbl.config(
            text=(f"{mins}:{secs:02d}   {int(party_total)}" if duration > 0 else ""))

        self.overview_title.config(
            text=("PARTY" if self.mode == "party" else "ALL PLAYERS")
            + f"   ({len(rows)})")

        focus = self._resolve_focus(rows)
        # Bars scale against the biggest number of their own kind on screen:
        # rows are damage-sorted so rows[0] holds the top damage, but the top
        # healer can be anyone.
        top_dmg = (rows[0].total if rows else 0.0) or 1.0
        top_heal = max((p.heal_total for p in rows), default=0.0) or 1.0
        for i, row in enumerate(self.player_rows):
            if i < len(rows):
                p = rows[i]
                dps = p.total / duration if duration > 0 else 0.0
                pct = (p.total / party_total * 100) if party_total else 0.0
                row.show(i + 1, p, dps, pct, focused=(focus == p.name),
                         dmg_frac=p.total / top_dmg,
                         heal_frac=p.heal_total / top_heal,
                         show_heal=self._show["healing"])
            else:
                row.hide()

        # ---- breakdown window ----
        fp = next((p for p in rows if p.name == focus), None)
        if fp is None:
            self.d_title.config(text="Breakdown")
            self.stats_lbl.config(text="waiting for combat ...")
            self.dmg_col.show([], 0)
            self.heal_col.show([], 0)
            self.elem_lbl.config(text="")
        else:
            self.d_title.config(text=f"Breakdown — {fp.name}")
            fdps = fp.total / duration if duration > 0 else 0.0
            crit_pct = (fp.crits / fp.hits * 100) if fp.hits else 0.0
            stats = [f"{int(fp.total)} dmg", f"{fdps:.0f} dps",
                     f"{fp.hits} hits", f"{crit_pct:.0f}% crit"]
            if self._show["healing"]:
                stats.append(f"{int(fp.heal_total)} heal")
            if fp.kills:
                stats.append(f"{fp.kills} kills")
            self.stats_lbl.config(text=" · ".join(stats))
            self.dmg_col.show(self._merge_named(fp.skills), fp.total)
            if self._show["healing"]:
                self.heal_col.show(self._merge_named(fp.heals), fp.heal_total)
            el = sorted(fp.elements.items(), key=lambda kv: -kv[1][1])
            self.elem_lbl.config(
                text="  ".join(f"{k}:{int(v[1])}" for k, v in el[:6]))

    def _resolve_focus(self, rows):
        if self.focus_player and any(p.name == self.focus_player for p in rows):
            return self.focus_player
        me = next((p.name for p in rows if p.is_me), None)
        return me or (rows[0].name if rows else None)

    def run(self):
        # The minimap gets its own timer rather than riding the 250ms refresh:
        # the hook feeds positions at ~6.7/sec, and redrawing at 4/sec throws
        # away a third of them and makes dots step instead of glide. It's a
        # canvas redraw of a few dozen items, so the extra ticks are cheap —
        # and it deliberately does NOT touch the aggregation the main loop owns.
        def map_loop():
            try:
                self._draw_minimap()
            except tk.TclError:
                return              # window went away; stop rescheduling
            self.root.after(MINIMAP_TICK_MS, map_loop)
        map_loop()

        def loop():
            # Checked before the refresh and without rescheduling, so a stand-
            # down request costs at most one tick. quit() (not destroy()) leaves
            # main()'s finally to unload the hook and detach.
            if quit_requested():
                print("[meter] a newer instance asked us to exit — shutting "
                      "down.", file=sys.stderr)
                self.root.quit()
                return
            self._refresh()
            self.root.after(REFRESH_MS, loop)
        loop()
        self.root.mainloop()


def _lerp_hex(a, b, t):
    """Blend two #rrggbb colours, t in 0..1."""
    av = tuple(int(a[i:i + 2], 16) for i in (1, 3, 5))
    bv = tuple(int(b[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02X%02X%02X" % tuple(
        int(round(x + (y - x) * t)) for x, y in zip(av, bv))


def _clamp01(v):
    return min(1.0, max(0.0, v))


class PlayerRow:
    """One meter line (rank, name, damage, dps, %, healing) over a stacked
    bar pair: damage (blue, top) and healing (green, bottom)."""

    def __init__(self, parent, on_click, fonts, theme=None):
        self.fonts = fonts
        self.theme = theme or THEME_DEFAULT
        self.f = tk.Frame(parent, bg=BG_BODY)
        self.line = tk.Label(self.f, text="", bg=BG_BODY, fg=FG_TEXT,
                             font=self.fonts["mono_10"], anchor="w")
        self.line.pack(fill="x")
        self.dmg_track = tk.Frame(self.f, bg=BG_BAR_TRACK, height=5)
        self.dmg_track.pack(fill="x")
        self.dmg_bar = tk.Frame(self.dmg_track, bg=DMG_BAR, height=5)
        self.dmg_bar.place(relwidth=0.0, relheight=1.0)
        self.heal_track = tk.Frame(self.f, bg=BG_BAR_TRACK, height=5)
        self.heal_track.pack(fill="x", pady=(1, 2))
        self.heal_bar = tk.Frame(self.heal_track, bg=HEAL_BAR, height=5)
        self.heal_bar.place(relwidth=0.0, relheight=1.0)
        for w in (self.f, self.line):
            w.bind("<Button-1>", lambda e: on_click(self._name))
        self._packed = False
        self._heal_packed = True
        self._name = None
        self._is_me = False

    def set_theme(self, t):
        self.f.config(bg=t["body"])
        self.line.config(bg=t["body"],
                         fg=t["fg_value"] if self._is_me else t["fg_text"])
        self.dmg_track.config(bg=t["track"])
        self.dmg_bar.config(bg=t["dmg"])
        self.heal_track.config(bg=t["track"])
        self.heal_bar.config(bg=t["heal"])

    def show(self, rank, p, dps, pct, focused, dmg_frac, heal_frac,
             show_heal=True):
        if not self._packed:
            self.f.pack(fill="x", pady=1)
            self._packed = True
        self._name = p.name
        tag = "▸ " if focused else "  "
        me = "*" if p.is_me else " "
        line = (f"{tag}{rank}.{me}{p.name[:12]:<12}{int(p.total):>9} "
                f"{dps:>6.0f} {pct:>3.0f}%")
        if show_heal:
            line += f"{int(p.heal_total):>9}"
        self._is_me = p.is_me
        self.line.config(text=line,
                         fg=self.theme["fg_value"] if p.is_me
                         else self.theme["fg_text"])
        self.dmg_bar.place_configure(relwidth=_clamp01(dmg_frac))
        if show_heal != self._heal_packed:
            self._heal_packed = show_heal
            if show_heal:
                self.heal_track.pack(fill="x", pady=(1, 2))
            else:
                self.heal_track.pack_forget()
            # The green bar carried the row's bottom margin; hand it to the
            # damage bar so rows don't run together without it.
            self.dmg_track.pack_configure(pady=(0, 0 if show_heal else 2))
        if show_heal:
            self.heal_bar.place_configure(relwidth=_clamp01(heal_frac))

    def hide(self):
        if self._packed:
            self.f.pack_forget()
            self._packed = False


class SkillColumn:
    """A titled skill list with a bar under each row (used for both damage and
    healing; bars scale to the column's biggest entry). Rows are shown as a
    prefix of a fixed pool, so pack order is stable."""

    def __init__(self, parent, title, bar_color, fonts):
        self.fonts = fonts
        self.f = tk.Frame(parent, bg=BG_BODY)
        self.bar_key = "heal" if bar_color == HEAL_BAR else "dmg"
        self.title_lbl = tk.Label(self.f, text=title, bg=BG_BODY, fg=ACCENT,
                                  font=self.fonts["ui_sm_b"], anchor="w")
        self.title_lbl.pack(fill="x")
        self.rows = []
        for _ in range(MAX_SKILL_ROWS):
            rf = tk.Frame(self.f, bg=BG_BODY)
            lbl = tk.Label(rf, text="", bg=BG_BODY, fg=FG_TEXT,
                           font=self.fonts["mono"], anchor="w")
            lbl.pack(fill="x")
            track = tk.Frame(rf, bg=BG_BAR_TRACK, height=4)
            track.pack(fill="x", pady=(0, 1))
            bar = tk.Frame(track, bg=bar_color, height=4)
            bar.place(relwidth=0.0, relheight=1.0)
            self.rows.append((rf, lbl, bar, track))
        self._shown = 0

    def set_theme(self, t):
        self.f.config(bg=t["body"])
        self.title_lbl.config(bg=t["body"], fg=t["accent"])
        for rf, lbl, bar, track in self.rows:
            rf.config(bg=t["body"])
            lbl.config(bg=t["body"], fg=t["fg_text"])
            track.config(bg=t["track"])
            bar.config(bg=t[self.bar_key])

    def show(self, entries, denom):
        """entries: [(label, total, hits, crits)] sorted desc; denom is the
        player's overall total for the % column."""
        n = min(len(entries), len(self.rows))
        if n > self._shown:
            for i in range(self._shown, n):
                self.rows[i][0].pack(fill="x")
        elif n < self._shown:
            for i in range(n, self._shown):
                self.rows[i][0].pack_forget()
        self._shown = n
        top = (entries[0][1] if entries else 0.0) or 1.0
        for i in range(n):
            label, tot, hits, _crits = entries[i]
            pct = (tot / denom * 100) if denom else 0.0
            _rf, lbl, bar, _track = self.rows[i]
            lbl.config(text=f"{label[:16]:<16}{int(tot):>8} {pct:>3.0f}% {hits:>3}h")
            bar.place_configure(relwidth=_clamp01(tot / top))


# ---------------------------------------------------------------------------
# Frida host
# ---------------------------------------------------------------------------
def build_script_source():
    data = (ANALYSIS / "resolver_data.json").read_text(encoding="utf-8")
    off = (ANALYSIS / "meter_offsets.json").read_text(encoding="utf-8")
    js = (FRIDA_DIR / "meter_hook.js").read_text(encoding="utf-8")
    return f"const DATA = {data};\nconst OFF = {off};\n" + js


DATA_STAMP = ANALYSIS / ".data_stamp.json"

# Top-level keys the current hook needs out of resolver_data.json. Data
# generated by an older build_targets.py predates some of these, and the
# hlboot.dat stamp alone can't tell (the *game* hasn't changed, our tools
# have) — so a file missing any of them forces a regenerate.
REQUIRED_RESOLVER_KEYS = ("anchors", "count_targets", "funcs", "ui_targets")


def _data_is_current():
    """True if resolver_data.json was produced by a build_targets.py new enough
    to carry everything the hook reads."""
    try:
        d = json.loads((ANALYSIS / "resolver_data.json").read_text())
    except Exception:
        return False
    return all(d.get(k) for k in REQUIRED_RESOLVER_KEYS)


def regenerate_data(hlboot=None, force=False):
    """Re-run the target/offset generators against the given hlboot.dat (or the
    tools' own auto-detect when None). Self-heals the shipped JSONs after a
    Farever patch. Skips the multi-second reparse when the same hlboot.dat is
    unchanged since the last successful run. Returns True on success."""
    import subprocess
    tools = [ROOT / "hltools" / "build_targets.py",
             ROOT / "hltools" / "emit_offsets.py"]
    missing = [t.name for t in tools if not t.exists()]
    if missing:
        print(f"[meter] can't self-heal — missing {', '.join(missing)} "
              "(copy the whole farevermeter-plus folder).", file=sys.stderr)
        return False
    stamp = None
    if hlboot is not None:
        st = Path(hlboot).stat()
        stamp = {"src": str(hlboot), "mtime": st.st_mtime, "size": st.st_size}
        if not force:
            try:
                if (json.loads(DATA_STAMP.read_text()) == stamp
                        and (ANALYSIS / "resolver_data.json").is_file()
                        and (ANALYSIS / "meter_offsets.json").is_file()
                        and _data_is_current()):
                    print("[meter] data already matches this build "
                          "(hlboot.dat unchanged).", file=sys.stderr)
                    return True
            except Exception:
                pass
    # The tools write beside their own location, which frozen is the bundle's
    # temp directory — the output would be thrown away with it on exit. Point
    # them at the writable copy instead. Harmless from source, where the two
    # paths are already the same.
    env = dict(os.environ, FAREVER_ANALYSIS_OUT=str(ANALYSIS))
    for t in tools:
        print(f"[meter] regenerating {t.name} for this build ...", file=sys.stderr)
        # Frozen there is no python.exe to hand a script to, and sys.executable
        # is this program — so it re-invokes itself in tool mode instead.
        cmd = ([sys.executable, TOOL_FLAG, t.name] if FROZEN
               else [sys.executable, str(t)])
        if hlboot is not None:
            cmd.append(str(hlboot))
        # Without CREATE_NO_WINDOW a console flashes up for each tool on every
        # launch of the windowed build — twice, right as the game is loading.
        r = subprocess.run(cmd, capture_output=True, text=True, env=env,
                           creationflags=CREATE_NO_WINDOW)
        if r.returncode != 0:
            print(f"[meter] {t.name} failed:\n{r.stdout}\n{r.stderr}",
                  file=sys.stderr)
            return False
    if stamp is not None:
        try:
            DATA_STAMP.write_text(json.dumps(stamp))
        except OSError:
            pass
    print("[meter] data regenerated for current build.", file=sys.stderr)
    return True


def _exe_path_of_pid(pid):
    """Full image path of a running process (None if unavailable)."""
    if sys.platform != "win32":
        return None
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(4096)
        size = wintypes.DWORD(len(buf))
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
    finally:
        k32.CloseHandle(h)
    return None


def find_game_process(device):
    """Locate the running Farever process by enumeration (not by name-attach).
    Waits for the game to launch if it isn't up yet; if several instances are
    running, asks which one to meter."""
    def matches():
        return [p for p in device.enumerate_processes()
                if p.name.lower() == TARGET_PROCESS.lower()]

    procs = matches()
    if not procs:
        print(f"[*] {TARGET_PROCESS} isn't running — waiting for it to start "
              "(launch the game) ...", file=sys.stderr)
        while not procs:
            # This can be a long wait, and with no console it's an invisible
            # one — so it has to be abandonable from the tray icon rather than
            # only by Ctrl+C.
            if STOP.wait(timeout=1.5):
                print("[meter] stopped while waiting for the game.",
                      file=sys.stderr)
                return None
            procs = matches()
        print(f"[*] {TARGET_PROCESS} is up.", file=sys.stderr)
    if len(procs) == 1:
        return procs[0]
    infos = [(p, _exe_path_of_pid(p.pid)) for p in procs]
    print(f"[*] {len(infos)} {TARGET_PROCESS} processes found:", file=sys.stderr)
    for i, (p, path) in enumerate(infos, 1):
        print(f"      {i}. pid {p.pid:>6}  {path or '(path unavailable)'}",
              file=sys.stderr)
    if not HAS_CONSOLE:
        # No stdin to answer on, so the question becomes a dialog. Rare enough
        # that it doesn't need to be pretty — but it does need to be asked,
        # since guessing wrong means metering the wrong client.
        i = ask_choice(
            "Farever+ Meter",
            f"{len(infos)} copies of Farever are running.\n"
            "Which one should the meter attach to?",
            [f"pid {p.pid} — {path or '(path unavailable)'}"
             for p, path in infos])
        return infos[i][0]
    while True:
        try:
            ans = input(f"    Which one is your game? [1-{len(infos)}] "
                        "(Enter = 1): ").strip()
        except (EOFError, RuntimeError):
            return infos[0][0]
        if not ans:
            return infos[0][0]
        if ans.isdigit() and 1 <= int(ans) <= len(infos):
            return infos[int(ans) - 1][0]


# ---------------------------------------------------------------------------
# Parse image
# ---------------------------------------------------------------------------
# Drawn from the numbers rather than screenshotted from the overlay: the live
# windows are layered and transparent, the breakdown only ever shows one player,
# and a capture would be at the mercy of whatever the game had drawn behind
# them. Same palette and the same monospace column layout, so it still reads as
# the meter.
PARSE_IMG_W = 620
PARSE_FONT_UI = "segoeuib.ttf"      # Segoe UI Bold, to match the headers
PARSE_FONT_MONO = "consola.ttf"     # Consolas, to match the columns


def _parse_font(name, size):
    """Load a Windows font by filename, falling back to PIL's built-in bitmap
    font so a missing/odd font install degrades the image instead of losing it."""
    from PIL import ImageFont
    for path in (Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / name,
                 Path(name)):
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_parse_image(data, path):
    """Draw a finished parse to `path` as a PNG. `data` is the plain dict built
    by Overlay._parse_snapshot — no Tk or session access from in here."""
    from PIL import Image, ImageDraw

    ui = _parse_font(PARSE_FONT_UI, 15)
    ui_small = _parse_font(PARSE_FONT_UI, 11)
    mono = _parse_font(PARSE_FONT_MONO, 14)
    mono_small = _parse_font(PARSE_FONT_MONO, 12)

    pad, bar_h, line_h, row_gap = 12, 6, 19, 6
    x0, x1 = 12, PARSE_IMG_W - 13
    rows, focus = data["rows"], data["focus"]

    # Drawn onto a canvas that's certainly tall enough and cropped to the ink at
    # the end — cheaper to get right than keeping a height formula in step with
    # the layout below, and it can't clip a long skill list.
    img = Image.new("RGB", (PARSE_IMG_W, 2000), BG_BODY)
    d = ImageDraw.Draw(img)

    d.rectangle((0, 0, PARSE_IMG_W - 1, 35), fill=BG_HEADER)
    d.text((x0, 9), data["title"], font=ui, fill=FG_HEADER)
    stamp = f"{data['when']}   {data['duration']:.0f}s"
    d.text((x1 - d.textlength(stamp, font=mono_small), 13), stamp,
           font=mono_small, fill=FG_HEADER)
    y = 36 + pad

    def bar(x, y, w, frac, colour, thick):
        d.rectangle((x, y, x + w, y + thick - 1), fill=BG_BAR_TRACK)
        filled = int(w * min(1.0, max(0.0, frac)))
        if filled > 0:
            d.rectangle((x, y, x + filled, y + thick - 1), fill=colour)

    d.text((x0, y), f"{data['mode']}   ({len(rows)})", font=ui_small, fill=ACCENT)
    y += 18
    d.text((x0, y), f"  #  {'NAME':<12}{'DMG':>9} {'DPS':>6} {'%':>4}{'HEAL':>9}",
           font=mono, fill=FG_DIM)
    y += line_h

    top_dmg = max((r["total"] for r in rows), default=0.0) or 1.0
    top_heal = max((r["heal"] for r in rows), default=0.0) or 1.0
    for i, r in enumerate(rows, 1):
        me = "*" if r["is_me"] else " "
        d.text((x0, y), f"  {i}.{me}{r['name'][:12]:<12}{int(r['total']):>9} "
                        f"{r['dps']:>6.0f} {r['pct']:>3.0f}%{int(r['heal']):>9}",
               font=mono, fill=FG_VALUE if r["is_me"] else FG_TEXT)
        y += line_h
        bar(x0, y, x1 - x0, r["total"] / top_dmg, DMG_BAR, bar_h)
        y += bar_h
        bar(x0, y, x1 - x0, r["heal"] / top_heal, HEAL_BAR, bar_h)
        y += bar_h + row_gap

    if focus:
        y += 6
        d.line((x0, y, x1, y), fill=BG_BAR_TRACK)
        y += 10
        d.text((x0, y), f"BREAKDOWN — {focus['name']}", font=ui, fill=FG_TEXT)
        y += 24
        d.text((x0, y), focus["stats"], font=mono_small, fill=FG_DIM)
        y += line_h + 4

        # Damage left, healing right — each column's bars scale to that column's
        # own biggest entry, exactly like the live breakdown.
        colw = (x1 - x0 - 18) // 2
        columns = ((x0, "DAMAGE", focus["skills"], DMG_BAR, focus["total"]),
                   (x0 + colw + 18, "HEALING", focus["heals"], HEAL_BAR,
                    focus["heal"]))
        for cx, title, _entries, _colour, _denom in columns:
            d.text((cx, y), title, font=ui_small, fill=ACCENT)
        y += 16
        col_bottom = y
        for cx, _title, entries, colour, denom in columns:
            # Same denominator and same line format as SkillColumn.show: the %
            # is of the player's overall total, not of the listed rows.
            scale = max((e[1] for e in entries), default=0.0) or 1.0
            cy = y
            for label, amount, hits, _crits in entries:
                pct = (amount / denom * 100) if denom else 0.0
                d.text((cx, cy),
                       f"{label[:16]:<16}{int(amount):>8} {pct:>3.0f}% {hits:>3}h",
                       font=mono_small, fill=FG_TEXT)
                cy += line_h
                bar(cx, cy, colw, amount / scale, colour, 4)
                cy += 4 + 3
            col_bottom = max(col_bottom, cy)
        y = col_bottom

        if focus["elements"]:
            y += 4
            d.text((x0, y), focus["elements"], font=mono_small, fill=FG_DIM)
            y += line_h

    img = img.crop((0, 0, PARSE_IMG_W, y + pad))
    # Border last, so it frames the cropped height rather than the scratch one.
    ImageDraw.Draw(img).rectangle((0, 0, PARSE_IMG_W - 1, img.height - 1),
                                 outline=BG_BORDER, width=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


# ---------------------------------------------------------------------------
# Single instance
# ---------------------------------------------------------------------------
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
STILL_ACTIVE = 259


def _open_process(pid, access):
    if sys.platform != "win32" or pid <= 0:
        return None
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
    return k32.OpenProcess(access, 0, pid) or None


def _close_handle(h):
    k32 = ctypes.windll.kernel32
    k32.CloseHandle.argtypes = (ctypes.c_void_p,)
    k32.CloseHandle(h)


def _process_alive(pid):
    """True while `pid` is still running. Note this can't be os.kill(pid, 0):
    on Windows os.kill TERMINATES the target instead of probing it."""
    h = _open_process(pid, PROCESS_QUERY_LIMITED_INFORMATION)
    if not h:
        return False
    try:
        k32 = ctypes.windll.kernel32
        k32.GetExitCodeProcess.argtypes = (ctypes.c_void_p,
                                           ctypes.POINTER(ctypes.c_ulong))
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        return bool(ok) and code.value == STILL_ACTIVE
    finally:
        _close_handle(h)


def _process_image(pid):
    """Full path of a pid's executable, or "". Guards the force-kill path: a
    stale lock file can name a pid Windows has since recycled onto something
    else entirely, and that must not be what gets terminated."""
    h = _open_process(pid, PROCESS_QUERY_LIMITED_INFORMATION)
    if not h:
        return ""
    try:
        k32 = ctypes.windll.kernel32
        k32.QueryFullProcessImageNameW.argtypes = (
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_ulong))
        size = ctypes.c_ulong(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        return buf.value if ok else ""
    finally:
        _close_handle(h)


def _quit_flag(pid):
    return LOCK_DIR / f"quit-{pid}"


def quit_requested():
    """Has a newly-started instance asked us to stand down? Polled by the
    overlay's own loop, so the answer is acted on within one refresh tick."""
    try:
        return _quit_flag(os.getpid()).exists()
    except OSError:
        return False


def watch_for_quit_request():
    """Poll the stand-down flag on a background thread, for the stretches where
    nothing else is polling it.

    The overlay checks the flag on its own refresh tick, but the overlay doesn't
    exist yet while we're waiting for Farever to launch or for the hook's memory
    scan to finish — and those are precisely the stretches a newly-started meter
    has to displace us through. Without this, an instance that hasn't reached
    the overlay ignores the request entirely and gets force-killed twelve
    seconds later, which is the outcome the whole polite handover exists to
    avoid. It matters more now that the meter is built to be started *before*
    the game, and so spends real time waiting."""
    def work():
        while not STOP.is_set():
            if quit_requested():
                print("[meter] a newer instance asked us to exit — standing "
                      "down.", file=sys.stderr)
                request_stop()
                return
            time.sleep(0.25)

    threading.Thread(target=work, daemon=True, name="quit-watch").start()


def _stop_instance(pid):
    """Ask pid to exit, and wait. The request is a file the running overlay
    polls — it returns from its mainloop and takes main()'s normal shutdown
    path, unloading the hook and detaching. Force-killing is the fallback only,
    because that's what leaves a half-attached agent in the game."""
    flag = _quit_flag(pid)
    try:
        flag.write_text("quit")
    except OSError:
        return
    deadline = time.monotonic() + QUIT_WAIT_SECS
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            print(f"[meter] pid {pid} shut down cleanly.", file=sys.stderr)
            break
        time.sleep(0.25)
    else:
        print(f"[meter] pid {pid} didn't respond within {QUIT_WAIT_SECS:.0f}s — "
              "forcing it. If the hook then fails to attach, fully close "
              "Farever and reopen it.", file=sys.stderr)
        h = _open_process(pid, PROCESS_TERMINATE)
        if h:
            ctypes.windll.kernel32.TerminateProcess.argtypes = (ctypes.c_void_p,
                                                                ctypes.c_uint)
            ctypes.windll.kernel32.TerminateProcess(h, 1)
            _close_handle(h)
    try:
        flag.unlink()
    except OSError:
        pass


def _acquire_claim_mutex():
    """Take the system-wide lock covering the read-decide-write in
    claim_single_instance(), returning a handle to release afterwards.

    Without it that sequence races itself. Stopping the previous instance takes
    up to QUIT_WAIT_SECS, and the winner's own pid isn't written to the lock
    file until after that — so a meter started inside the gap reads the same
    stale pid, shuts down the same already-dying instance, and declares itself
    the survivor too. Two live meters, each certain it's the only one. Starting
    the game from Steam while a shortcut launch is still settling is enough to
    hit it."""
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    k32.ReleaseMutex.argtypes = [ctypes.c_void_p]
    try:
        h = k32.CreateMutexW(None, False, CLAIM_MUTEX)
        if not h:
            return None
        # Long enough to outlast a full QUIT_WAIT_SECS handover ahead of us.
        # A timeout isn't fatal — carrying on unserialised is exactly the old
        # behaviour, so the worst case is no worse than before.
        k32.WaitForSingleObject(h, CLAIM_WAIT_MS)
        return h
    except Exception as e:
        print(f"[meter] claim lock unavailable ({e}) — continuing.",
              file=sys.stderr)
        return None


def _release_claim_mutex(h):
    if not h:
        return
    k32 = ctypes.windll.kernel32
    try:
        k32.ReleaseMutex(h)
        k32.CloseHandle(h)
    except Exception:
        pass


def claim_single_instance():
    """Become the only meter running, then record ourselves in the lock file.

    Two overlays on screen at once is confusing enough; two hooks in the game is
    worse. The usual way into it is launching a *second copy* of this script
    from a different folder while the first is still up — which is why the lock
    lives in LOCK_DIR rather than beside the script."""
    try:
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return                       # no lock dir => skip the mechanism entirely

    claim = _acquire_claim_mutex()
    try:
        _claim_locked()
    finally:
        _release_claim_mutex(claim)


def _claim_locked():
    """The claim itself. Only ever runs one-at-a-time system-wide."""
    try:
        other = json.loads(LOCK_FILE.read_text())
    except Exception:
        other = {}
    pid = int(other.get("pid") or 0)
    if pid and pid != os.getpid() and _process_alive(pid):
        image = _process_image(pid)
        # A meter is either a python interpreter running the script or the
        # installed executable, and BOTH have to be recognised from either side
        # — the case this exists for is one build being started while the other
        # is already up, and each has to see the other as a meter to displace.
        name = Path(image).name.lower()
        if "python" in name or name in METER_IMAGE_NAMES:
            print(f"[meter] another meter is already running (pid {pid}, "
                  f"{other.get('script') or 'unknown script'}) — asking it to "
                  "exit ...", file=sys.stderr)
            _stop_instance(pid)
        else:
            print(f"[meter] ignoring a stale lock: pid {pid} is "
                  f"{image or 'something unidentifiable'}, not a meter.",
                  file=sys.stderr)

    try:
        LOCK_FILE.write_text(json.dumps({
            "pid": os.getpid(),
            "script": str(Path(__file__).resolve()),
            "started": time.time(),
        }))
    except OSError:
        pass


def release_instance_lock():
    for p in (LOCK_FILE, _quit_flag(os.getpid())):
        try:
            p.unlink()
        except OSError:
            pass


def locate_hlboot(pid):
    """Find the hlboot.dat matching the *running* game. Priority: explicit
    FAREVER_HLBOOT override, then the file next to the process's own exe (which
    makes a multi-install mismatch impossible), then the drive auto-detect, and
    finally just asking. Returns a Path or None (= use shipped data as-is)."""
    env = os.environ.get("FAREVER_HLBOOT")
    if env:
        if Path(env).is_file():
            return Path(env)
        print(f"[meter] FAREVER_HLBOOT points to a missing file: {env}",
              file=sys.stderr)
    exe = _exe_path_of_pid(pid)
    if exe:
        cand = Path(exe).parent / "hlboot.dat"
        if cand.is_file():
            return cand
        print(f"[meter] no hlboot.dat next to {exe} — searching drives.",
              file=sys.stderr)
    sys.path.insert(0, str(ROOT / "hltools"))
    try:
        from gamepath import find_hlboot
        return Path(find_hlboot(argv_index=99))
    except (SystemExit, Exception):
        pass
    if not HAS_CONSOLE:
        # Asked at most once per install in practice: the file normally sits
        # next to the running exe, and that's checked first.
        p = ask_directory("Farever+ Meter — where is Farever installed? "
                          "(the folder containing hlboot.dat)")
        if p is None:
            return None
        cand = p if p.is_file() else p / "hlboot.dat"
        if cand.is_file():
            return cand
        message_box(f"No hlboot.dat in:\n{p}\n\nThe meter will start with the "
                    "data it shipped with, which is fine unless Farever has "
                    "patched since this version was built.",
                    "Farever+ Meter", 0x30)      # MB_ICONWARNING
        return None
    while True:
        try:
            d = input("    Where is Farever installed? (folder containing "
                      "hlboot.dat, Enter to skip): ").strip().strip('"')
        except (EOFError, RuntimeError):
            return None
        if not d:
            return None
        p = Path(d)
        cand = p if p.is_file() else p / "hlboot.dat"
        if cand.is_file():
            return cand
        print(f"    [!] no hlboot.dat at {p}", file=sys.stderr)


def main():
    seed_analysis()
    check_for_update()          # background; the notice lands when it lands
    claim_single_instance()
    # Only after claiming: before it, the flag on disk may still be the one
    # aimed at the instance we just displaced.
    watch_for_quit_request()
    session = PartySession()
    ui_state = GameUIState()
    world = WorldSnapshot()

    # Up before anything that can block. Attaching waits for the game to launch
    # and the hook's memory scan can run for minutes on a slow machine — with no
    # console, an icon that only appeared afterwards would leave the user
    # staring at nothing, with Task Manager as their only way to change their
    # mind. Its quit callback works throughout, overlay or not.
    tray = TrayIcon(request_stop)
    tray.start()
    try:
        return _run(tray, session, ui_state, world)
    finally:
        tray.stop()
        # Here as well as on the paths inside _run, which miss the early
        # returns — stopping while still waiting for the game would otherwise
        # leave our pid sitting in the lock file. Unlinking twice is harmless.
        release_instance_lock()


def _run(tray, session, ui_state, world):
    device = frida.get_local_device()
    proc = find_game_process(device)
    if proc is None:
        return                      # stopped from the tray before we attached
    pid = proc.pid

    # Match the data files to the build that is ACTUALLY RUNNING before
    # hooking: hlboot.dat is taken from the attached process's own install
    # directory, so a version/install mismatch — the usual cause of a slow or
    # failed table search — is impossible. Skipped when the file is unchanged.
    hlboot = locate_hlboot(pid)
    if hlboot is None:
        print("[meter] using the shipped data files as-is (couldn't locate "
              "hlboot.dat to verify them).", file=sys.stderr)
    else:
        print(f"[*] game data: {hlboot}", file=sys.stderr)
        regenerate_data(hlboot)   # best-effort; falls back to existing files

    print(f"[*] attaching to {TARGET_PROCESS} (pid {pid}) ...", file=sys.stderr)
    try:
        fsession = device.attach(pid)
    except frida.ProcessNotFoundError:
        sys.exit(f"[!] {TARGET_PROCESS} (pid {pid}) exited before attach. "
                 "Relaunch the game, then the meter.")
    except frida.PermissionDeniedError:
        sys.exit("[!] permission denied attaching — if Farever runs as "
                 "administrator, run the meter from an elevated terminal too.")

    ready = {"ok": None}
    ready_evt = threading.Event()
    liveness = {"t": time.monotonic(), "printed": 0.0}
    hero_id = {"name": None}           # last local hero, to keep the log quiet

    def on_message(message, data):
        liveness["t"] = time.monotonic()   # any agent traffic counts as alive
        if message["type"] == "error":
            print("[JS]", message.get("description"), file=sys.stderr)
            return
        p = message.get("payload") or {}
        k = p.get("kind")
        if k == "hit":
            session.record(p)
        elif k == "heal":
            session.record_heal(p)
        elif k == "combat":
            session.set_combat(p.get("state") or {})
        elif k == "world":
            world.update(p)
        elif k == "rift":
            state = bool(p.get("state"))
            ui_state.set_rift(state)
            print(f"[meter] rift: {state}", file=sys.stderr)
        elif k == "window":
            name, is_open = p.get("name"), bool(p.get("open"))
            ui_state.set_window(name, is_open)
            # Logged because every class the game opens now hides the overlay
            # (MENU_IGNORE_WINDOWS aside) — if the meter goes missing and stays
            # missing, these lines name the window that's holding it down.
            print(f"[meter] game window {name} {'open' if is_open else 'closed'}",
                  file=sys.stderr)
        elif k == "zone":
            ui_state.clear()      # the UI is rebuilt across a loading screen
            session.reset()
            print(f"[meter] zone change ({p.get('sig')!r}) — meter reset",
                  file=sys.stderr)
        elif k == "hero":
            # The hook re-reports the local hero every 3s so it survives a
            # respawn or zone change, so only the first identification and a
            # genuine change are worth a line. The name itself is tracked but
            # never printed — the console is often on screen next to the game,
            # and the overlay's own `*` row already says which player is you.
            name = p.get("name")
            # The roster rides along on this message; the minimap needs it to
            # ring group members, including ones who haven't fought yet.
            world.set_hero(name, p.get("party"))
            if name and name != hero_id["name"]:
                first = hero_id["name"] is None
                hero_id["name"] = name
                print("[meter] local hero "
                      + ("identified." if first else "changed."), file=sys.stderr)
        elif k == "log":
            print("[hook]", p.get("msg"), file=sys.stderr)
        elif k == "progress":
            now = time.monotonic()
            if now - liveness["printed"] > 5.0:     # throttle the status line
                liveness["printed"] = now
                print(f"[meter] hook scanning memory ... "
                      f"({p.get('done')}/{p.get('total')} regions)",
                      file=sys.stderr)
        elif k == "ready":
            ready["ok"] = p.get("ok")
            print(f"[meter] hook ready ok={p.get('ok')}", file=sys.stderr)
            ready_evt.set()

    def load_hook():
        sc = fsession.create_script(build_script_source())
        sc.on("message", on_message)
        sc.load()   # returns promptly; the hook sets up asynchronously
        return sc

    def wait_ready(max_total=240.0, idle_grace=30.0):
        """Wait for the hook's ready message. The scan can legitimately take
        minutes on a slow/loaded machine, and unloading a live scan restarts it
        from zero — so as long as the agent keeps talking (progress heartbeats),
        keep waiting. Give up only when it goes silent or hits the hard cap."""
        start = time.monotonic()
        while True:
            if ready_evt.wait(timeout=0.5):
                return True
            if STOP.is_set():
                return False        # asked to quit mid-scan
            now = time.monotonic()
            if now - start > max_total:
                print("[meter] hook scan exceeded the time cap.", file=sys.stderr)
                return False
            if now - liveness["t"] > idle_grace:
                print("[meter] hook went silent — treating it as dead.",
                      file=sys.stderr)
                return False

    # Bring the hook up with clean, bounded retries. The scan runs async in the
    # agent, so a genuinely dead init times out here — we unload cleanly and
    # retry rather than leaving a half-attached agent (which is what destabilises
    # the game when people force-kill and relaunch repeatedly).
    script = None
    for attempt in range(1, 4):
        if STOP.is_set():
            break
        ready["ok"] = None
        ready_evt.clear()
        liveness["t"] = time.monotonic()
        try:
            script = load_hook()
        except Exception as e:
            print(f"[meter] load attempt {attempt} failed: {e}", file=sys.stderr)
            script = None
        if script is not None and wait_ready() and ready["ok"]:
            break
        print(f"[meter] hook didn't come up (attempt {attempt}/3); "
              "cleaning up and retrying ...", file=sys.stderr)
        if script is not None:
            try:
                script.unload()
            except Exception:
                pass
            script = None
        if ready["ok"] is False:            # search concluded, table not found
            regenerate_data(hlboot, force=True)   # => refresh data and retry
        time.sleep(1.0)

    if STOP.is_set():
        # Stopped from the tray during startup. Same teardown the overlay's
        # finally does — the hook may be half-loaded, and leaving it attached is
        # what destabilises the game.
        print("[meter] stopped during startup.", file=sys.stderr)
        try:
            if script is not None:
                script.unload()
            fsession.detach()
        except Exception:
            pass
        release_instance_lock()
        return

    if script is None or ready["ok"] is not True:
        print("[meter] could not initialise the hook after 3 attempts.\n"
              "        Fully close Farever and reopen it, then relaunch the meter.\n"
              "        If it keeps happening, send the full log above to whoever\n"
              "        gave you the meter — the [hook] lines say where it stopped.\n"
              "        (Avoid repeatedly relaunching against a stuck session — "
              "that can crash the game.)", file=sys.stderr)
        # The overlay still comes up, so without this the windowed build would
        # put two empty windows on screen and never say why they stay empty.
        if not HAS_CONSOLE:
            message_box(
                "The meter couldn't hook into Farever, so it won't show any "
                "numbers.\n\nFully close Farever, reopen it, and start the "
                f"meter again.\n\nThe details are in:\n{LOG_FILE}",
                "Farever+ Meter — couldn't attach", 0x30)   # MB_ICONWARNING

    print("[*] overlay starting. Open the game's escape menu for the control "
          "menu (and to drag the windows / click a row to inspect). Only "
          "hotkey: Shift+\\ resets the encounter.", file=sys.stderr)

    overlay = Overlay(session, pid, ui_state, world)
    # From here the overlay owns shutdown: it's the only thing that can return
    # from the mainloop and let the finally below unload the hook and detach.
    _OVERLAY["ref"] = overlay
    if STOP.is_set():
        # Asked to stop during the hook's setup, which the overlay didn't exist
        # to hear. Honour it rather than putting windows on screen.
        overlay.request_quit()
    try:
        overlay.run()
    finally:
        _OVERLAY["ref"] = None
        try:
            script.unload()
            fsession.detach()
        except Exception:
            pass
        release_instance_lock()


def _cli():
    # Tool mode first: this is the frozen build standing in for python.exe to
    # run one of the bundled hltools generators, and it must not start a meter.
    if len(sys.argv) > 2 and sys.argv[1] == TOOL_FLAG:
        run_bundled_tool(sys.argv[2], sys.argv[3:])
        return
    setup_logging()
    try:
        main()
    except KeyboardInterrupt:
        # Ctrl+C is a documented way to stop a from-source run, so it shouldn't
        # look like a crash: main()'s finally has already unloaded the hook and
        # detached by the time this runs. Printing (and exiting 0) also lets a
        # launcher tell a normal stop from a real failure.
        print("[meter] stopped.", file=sys.stderr)
    except SystemExit as e:
        # sys.exit() carries the startup failures — messages written for a
        # console that the windowed build doesn't have. Put them on screen
        # instead of exiting silently, which would look like nothing happened.
        if not HAS_CONSOLE and e.code not in (0, None):
            print(f"[meter] {e.code}", file=sys.stderr)
            message_box(e.code, "Farever+ Meter — can't start", 0x10)
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        if not HAS_CONSOLE:
            message_box(
                "The meter hit an unexpected error and stopped.\n\n"
                f"The details are in:\n{LOG_FILE}\n\n"
                "Send that file to whoever gave you the meter.",
                "Farever+ Meter — error", 0x10)
        raise


if __name__ == "__main__":
    _cli()
