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
# Settings live apart from window positions on purpose: "Reset window
# positions" clears that file, and it has no business resetting your theme and
# your Show/hide ticks along with it.
SETTINGS_CACHE = _WRITABLE / ".meter_settings.json"
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
# Extra see-through on top of that, from the Transparency slider. 0 leaves the
# overlay exactly as it has always looked; the cap stops short of a UI you can
# no longer read, which is a setting people find by dragging and then can't
# find their way back from.
#
# This is WINDOW opacity, which is the only kind Windows gives a layered window
# — so it takes the whole window with it: panel, header bar and text alike.
# There is no way to fade a background out from under its own text here.
TRANSPARENCY_MAX = 80
# The windows it applies to: everything that belongs to the game view. The
# control menu and its hint are exempt because they're what you're reading
# while you drag the slider, and the rift prompt because it's a question that
# has to be answered.
TRANSPARENCY_EXEMPT = ("menu", "hint", "prompt")
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
# Each overlay window is Show / Hide / Show in ESC rather than a tick, so
# "hidden while playing but there when I open the menu" is something you can
# ask for directly. It used to be what an unticked box did, which meant there
# was no way to say "hidden, and I mean it".
ELEMENT_MODES = ("Show", "Hide", "Show in ESC")
ELEMENT_SHOW, ELEMENT_HIDE, ELEMENT_ESC = ELEMENT_MODES

TOGGLEABLE_ELEMENTS = (
    ("meter", "Damage meter"),
    ("detail", "Breakdown"),
    ("rift", "Rift timer"),
    ("minimap", "Minimap"),
    ("compass", "Compass"),
)

# Elements the out-of-combat rule doesn't touch. The rift countdown is most use
# exactly when you're standing around between pulls, so hiding it out of combat
# would hide it for its whole useful life. The minimap is the same case only
# more so: its whole job is telling you what's around while you're travelling,
# which is by definition out of combat.
OOC_EXEMPT = ("rift", "minimap", "compass")

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

# How often the hook sweeps the world. Higher costs a little CPU in the game
# process (~1ms per sweep) and a message per tick; below about 8/sec the dots
# visibly step rather than glide, which is what makes a minimap feel laggy.
# Ultra sits just above the agent's own 30ms floor. At ~30/sec the sweep costs
# a few percent of one core in the game process plus a message per tick, which
# is why it's opt-in rather than the default.
MINIMAP_RATES = (("Ultra", 33), ("High", 60), ("Medium", 110), ("Low", 250))
MINIMAP_RATE_MS = dict(MINIMAP_RATES)
MINIMAP_RATE_NAMES = [n for n, _ in MINIMAP_RATES]
MINIMAP_SIZE = 405          # square, in pixels at 100% UI scale
# World units from centre to edge. This and MINIMAP_SIZE move together: what
# matters is units-per-pixel, not either number alone. 120 units on the old
# 270px panel was the density that made a fight readable; 175 on 405px is
# looser than that and still shows half again as much ground as 120 did.
# Landed on by eye, from 250 — which fit more in but had started to shrink a
# fight back toward the couple of pixels the range exists to avoid.
MINIMAP_RANGE = 175
# The canvas is redrawn at roughly twice the sweep rate. Matching them exactly
# would beat against the hook's timer and drop or double frames; drawing a bit
# faster than the data arrives keeps motion even.

# The floor and ceiling the range is allowed to take. Nothing moves it at
# runtime today — there's no zoom control — so the ceiling is documentation for
# whatever adds one. It stops short of the hook's 600u foe cull on purpose:
# past that the map would show chests and players with the mobs thinning out
# around them, which reads as the map breaking rather than as the cull it is.
MINIMAP_RANGE_MIN, MINIMAP_RANGE_MAX = 80, 175

# How each category is drawn: colour, radius in pixels, and shape. Kept in one
# table so the legend, the draw pass and any future re-skin can't disagree.
# Drawn in list order, so later entries land on top — see the note inside.
# Tuned for a DARK panel, which both themes now use — a map is easier to read
# when the markers are the bright thing on it rather than the background being
# brightest. The meter's own body colour is deliberately not reused here: the
# damage tables want to look like parchment and a map does not.
MINIMAP_STYLE = (
    # Players go FIRST, which puts them UNDERNEATH everything else. There are
    # usually several, they cluster on the same spot, and a stack of chevrons
    # will happily bury the one chest you were looking for. The map is for
    # finding things in the world; the people are the part already on screen.
    ("hero",     {"fill": "#5FAEFF", "r": 5.5, "shape": "chevron"}),
    # Teal. Close to the respawn point's mint on a colour wheel, which would
    # matter if they shared a shape — a diamond against a small square is the
    # thing telling them apart, and this one is bluer and much more saturated.
    ("activity", {"fill": "#25D0D0", "r": 4.0, "shape": "diamond"}),
    # Half again the size of the rest. An obelisk is a fixed landmark you
    # navigate by rather than something you might walk past, and the monolith
    # silhouette — a tall block with a dark eye — is the one glyph here that
    # carries detail worth seeing.
    ("obelisk",  {"fill": "#C48CFF", "r": 6.6, "shape": "monolith"}),
    # The soulstone's own colour, taken off the thing in the world rather than
    # picked from a palette: it is a hot magenta crystal with a lighter core,
    # and matching it is what makes the marker identifiable before you've
    # learned the legend. Purple enough to sit near the obelisk's lavender, so
    # the two are told apart by SHAPE — a shard against a standing stone.
    ("soulstone", {"fill": "#FF3DC4", "r": 4.6, "shape": "shard"}),
    # Dark blue, but only as dark as the panel allows: the Dark themes draw on
    # a deep navy, and a respawn point any deeper than this stops being a
    # marker and becomes a hole in the map. Measured against that body it still
    # comes out about three times its brightness. Distinct from the players'
    # light blue by being far darker, and from everything else by shape.
    ("respawn",  {"fill": "#2B5FD9", "r": 3.0, "shape": "square"}),
    # A plain square. It briefly had a black cross through it to separate it
    # from the respawn point, which is also a square — at nine pixels that read
    # as busy rather than as a chest, and the two are told apart by colour
    # perfectly well.
    ("chest",    {"fill": "#FF9E3D", "r": 3.5, "shape": "square"}),
    ("orb",      {"fill": "#FFD400", "r": 3.6, "shape": "dot",
                  "ring": "#A24BE0"}),
    # The smallest thing on the map, and drawn last so it sits on top of
    # everything. There are far more of these than anything else — a pack is a
    # dozen dots on one spot — so size is what keeps them from swamping the
    # markers you navigate by. Hovering one still works: the hit test has its
    # own slack (MINIMAP_TIP_RADIUS) and doesn't shrink with the dot.
    ("foe",      {"fill": "#FF5348", "r": 2.4, "shape": "dot"}),
)
MINIMAP_STYLE_MAP = dict(MINIMAP_STYLE)
MINIMAP_ORDER = [k for k, _ in MINIMAP_STYLE]

# The minimap's own show/hide, grouped the way you'd think about them rather
# than one tick per sweep category: nobody wants orbs without chests.
#
# Obelisks, respawn points and soulstones are deliberately absent and always
# drawn. They're the landmarks you navigate BY — there are a handful in a zone,
# they never move, and they're the least likely thing anyone wants gone. A tick
# each would be four more rows of menu for a problem nobody has.
MINIMAP_FILTERS = (
    ("collect",    "Collectibles", ("orb", "chest")),
    ("players",    "Players",      ("hero",)),
    ("enemies",    "Enemies",      ("foe",)),
    ("activities", "Activities",   ("activity",)),
)
# category -> which tick governs it, built once rather than searched per marker.
MINIMAP_FILTER_OF = {cat: key for key, _label, cats in MINIMAP_FILTERS
                     for cat in cats}

# The compass gets its own pair, deliberately not shared with the map's. The
# two panels answer different questions — "what is around me" against "which
# way is that" — and wanting chests on one but not the other is an ordinary
# thing to want. Soulstones have no tick for the same reason obelisks have none
# on the map: there is at most one in range and it's the thing you're looking
# for.
COMPASS_FILTERS = (
    ("collect", "Collectibles", ("orb", "chest")),
    ("party",   "Party Members", ("hero",)),
)
COMPASS_FILTER_OF = {cat: key for key, _label, cats in COMPASS_FILTERS
                     for cat in cats}
# Every marker on the MAP is drawn this much larger than the table says. One
# multiplier rather than eight edited radii, so the relative sizes above — which
# are tuned against each other, not against the panel — survive a resize. The
# compass deliberately doesn't use it: markers there sit on a 38px strip with
# numbers under them, and have no room to grow.
MINIMAP_ICON_SCALE = 1.20

# What the hover strip says with nothing under the cursor. It doubles as the
# hint that hovering does anything, which is why it isn't blank.
# Two lines even when idle, so the box never changes height under the cursor.
# The second line says how to get a cursor at all: the map only takes the mouse
# once the game has let go of it, which isn't something you'd guess.
MINIMAP_TIP_IDLE = ("hover a marker for details\n"
                    "Press L-ALT or ESC to enable free mouse")
MINIMAP_TIP_RADIUS = 9          # px of slack around a marker, at 100% scale
# Names are elided rather than allowed to set the panel width. A long one
# ("Fragrant Garlic Seedling") otherwise stretches the whole minimap sideways
# on hover and snaps it back on leave, which moves the map out from under the
# cursor you were pointing with.
MINIMAP_TIP_MAXLEN = 22

# Hover labels. Separate from the style table because these are prose for a
# human, not drawing instructions.
# States that just mean "normal, still there". A marker only reaches the map if
# it's still worth going to, so saying "Closed" on every chest and obelisk is
# noise — "Obelisk" is the whole message. Anything NOT on this list is shown:
# Locked, or a state no one has seen yet, which is how an unfamiliar one makes
# itself known instead of passing for ordinary.
# "None" is on the list because a soulstone has no state machine at all — every
# one of them reads it, so "Soulstone · None" would be noise on every marker
# rather than the warning an unfamiliar state is meant to be.
MINIMAP_PLAIN_STATES = ("Closed", "Enabled", "Idle", "Active", "Default", "None")

# Short class tags for the meter. The game's own names come off ent.Unit.kind,
# which for a hero is its class rather than a creature id.
CLASS_ABBR = {"Warrior": "War", "Mage": "Mag", "Priest": "Pst", "Rogue": "Rog"}

# The meter's name and class columns, in monospace cells. They used to be one
# 17-cell field with the class in brackets after the name; a class of its own is
# both easier to scan down and immune to a long name pushing it about. The two
# still add up to 17, so the DMG column and MIN_W["meter"] are where they were.
METER_NAME_CELLS = 13
METER_CLASS_CELLS = 4


def _short_dist(units):
    """A distance narrow enough to sit under a compass marker.

    No unit suffix: every number on that strip is world units, and at four
    characters wide the "u" is the difference between two neighbouring markers
    reading cleanly and their labels touching. Thousands are abbreviated for the
    same reason — "2.7k" where "2731" would be, since nothing you do with a
    bearing that far out depends on the last two digits."""
    if units < 1000:
        return f"{units:.0f}"
    return f"{units / 1000.0:.1f}k"


def _class_tag(kind):
    """(War) for Warrior. Anything unrecognised falls back to its first three
    letters rather than disappearing — a new class should look odd, not absent."""
    if not kind:
        return ""
    return CLASS_ABBR.get(kind) or kind[:3].title()

MINIMAP_LABELS = {
    "hero": "Player", "foe": "Enemy", "chest": "Chest", "orb": "Orb",
    "obelisk": "Obelisk", "respawn": "Respawn point", "activity": "Activity",
    "soulstone": "Soulstone",
}

# ---------------------------------------------------------------------------
# Compass
# ---------------------------------------------------------------------------
# A strip of bearings across the top of the view. It answers a different
# question from the minimap: not "what is around me" but "which way is that",
# and it answers it out to the full sweep radius rather than the map's 120u —
# so the thing you're walking toward stays on screen long after it has left
# the map.
COMPASS_W = 460             # px at 100% scale
# Three bands, top to bottom: cardinals, markers, distances. The strip grew
# from 26px when the distances arrived — they need a line of their own, since
# tucking them beside the glyphs made two markers half a degree apart overlap
# into an unreadable smear.
COMPASS_H = 38
COMPASS_CARD_Y = 0.15       # cardinal letter, as a fraction of the height
COMPASS_TICK_TOP = 0.28     # its tick, below the letter
COMPASS_TICK_BOT = 0.36
# Marker centres. Lifted when the pill shrank: the tallest glyphs reach about
# 7px from their centre (the soulstone's shard, a player's chevron), and at
# 0.56 those were poking through the pill's lower edge — which reads as a
# drawing mistake, where the numbers hanging below it reads as a choice.
COMPASS_MARK_Y = 0.50
COMPASS_DIST_Y = 0.87       # the distance under each one
# How much of that height the pill actually covers. It stops just above the
# distances on purpose, so the numbers hang off its lower edge onto the game
# rather than sitting inside a band that has to be tall enough to hold them.
# The strip reads as a narrow bar with figures under it, which is a smaller
# thing on screen than the same information boxed in.
COMPASS_PILL_H = 0.72
# Minimum px between two distance labels at 100% scale. "1.2k ↑" is about this
# wide, so anything closer would be printing one number over another; the
# nearer marker keeps its label and the farther one goes without.
COMPASS_DIST_GAP = 30
# Each distance sits on its own little black badge, because it's the only text
# on the overlay that hangs off a panel onto the scenery. An outline was tried
# first and looked like exactly what it was: eight offset copies of the text,
# filling the gaps between thin italic strokes until the number read as a
# blot. A shape behind the text is both cleaner and cheaper.
#
# The badge carries its own contrast, so its ink is a constant rather than
# something derived from the panel — on the parchment theme the panel's ink is
# nearly black, and nearly black on black is not a badge.
COMPASS_DIST_BOX = "#000000"
COMPASS_DIST_BOX_INK = "#EDEFF5"
# Tk canvas items have no alpha, so the badge is knocked back with a stipple
# instead: a bitmap pattern that leaves a quarter of its pixels unpainted, and
# unpainted here means the transparency key, which means the game. Dithered
# rather than blended — at "gray75" it reads as a slightly softened black, and
# anything sparser starts to look like a screen door.
COMPASS_DIST_STIPPLE = "gray75"
COMPASS_DIST_PAD_X = 3.0        # px at 100%, around the text
COMPASS_DIST_PAD_Y = 0.5
# Square corners, and not for want of trying. The badge is 13px tall — a 12px
# linespace plus the padding — and Tk's only rounded shape is a smoothed
# polygon, whose spline overshoots at that size: measured at radius 3, 5 and 6
# on a 14px box, it clipped exactly one pixel per corner and filled the rest
# straight back in. A rectangle is what it was already drawing, minus the
# pretence and four extra points.
COMPASS_FOV = 180.0         # degrees of bearing shown, centred on your view
# No range limit, except where a category earns one. The agent sends these from
# the whole layer rather than a radius around you (see SWEEP_RADIUS_FOE in
# meter_hook.js), and the point of the strip is the thing you're walking to,
# which is exactly the thing that is far away. Measured: a world zone holds
# ~250 entities of which a handful are on this list, so "everything" is a
# smaller number than it sounds.
# Enemies, respawn points and activities are deliberately absent: the strip is
# for things you're travelling to, and a compass crowded with mobs is a smear.
# Obelisks are off it too — there are ten in a zone, they're permanent scenery,
# and at whole-map range they were most of what the strip was carrying.
COMPASS_CATS = ("chest", "orb", "hero", "soulstone", "obelisk")
# Two categories keep a radius, and they're the two that are worth knowing
# about when you're near one and noise when you aren't — which is the opposite
# of how the chests and party members on this strip behave. Obelisks came off
# the compass entirely at one point for that reason: ten in a zone, permanently
# there, and at whole-map range they were most of what the strip was carrying.
# With a radius they're useful again without being the wallpaper.
COMPASS_LIMITS = {"soulstone": 200.0, "obelisk": 200.0}
# The ground plane is left-handed against the screen — the same fact
# MINIMAP_MIRROR_X exists for — so the axis that trigonometry calls north is
# the game's SOUTH. Naming +y "north" gave a compass that was a mirror of a
# real one: facing its N put E on your left. E and W sit on the mirror axis and
# so are unmoved; only N and S trade places. If these ever look wrong again,
# check the handedness rather than the eye: with the heading set to N, E must
# come out RIGHT of centre through _compass_x. A reflected compass is
# self-consistent and looks perfectly ordinary until you compare it to the sky.
COMPASS_CARDINALS = ((0.0, "E"), (90.0, "S"), (180.0, "W"), (270.0, "N"))

# The player arrow: whatever stands furthest off the panel, so it's the
# brightest thing on a dark map and the darkest on a light one. A constant white
# arrow vanished on the Farever panel, which is exactly the failure the note
# below warns about — a colour that happens to equal its background.
MINIMAP_ME_LIFT = 0.92
# How far marker colours are darkened on a light panel. 0.35 keeps every hue
# recognisable (the orb stays yellow, the soulstone stays magenta) while
# clearing the parchment: measured against MAP_BODY_FAREVER, the palest marker
# still lands well below it in luma.
MINIMAP_LIGHT_DARKEN = 0.35

# The map panel gets its own background ("map_body" on each theme) rather than
# the meter's parchment body — the damage tables want to look like parchment
# and a map does not. Everything else on the panel (rings, the hover strip, the
# up/down carets) is DERIVED from that one colour rather than listed per theme,
# so a re-tint is a one-line change and can't leave a stale colour behind. That
# has bitten this file before: the view cone spent a release invisible because
# it was blended toward a constant that happened to equal the background.
# The view cone out of the player marker. Drawn under everything else and
# blended toward the panel, so it reads as a hint of where you're looking
# rather than as another object on the map.
# The view line, blended toward the theme's ACCENT rather than toward the
# player marker's own colour — the marker is near enough the panel that blending
# toward it draws nothing, which is how the old view cone spent a release
# invisible.
MINIMAP_VIEW_LINE = 0.60

# The up/down caret is drawn in whichever of black or white stands out against
# the panel — black on the Farever parchment, white on the dark and rift ones.
# Chosen from the body's brightness rather than listed per theme, so it
# stays right on its own if the palette is ever retuned. It's a symbol rather
# than a shade of the marker it belongs to, and it has to read on something
# already faded halfway into the background.
MINIMAP_Z_MARK_DARK = "#000000"
MINIMAP_Z_MARK_LIGHT = "#FFFFFF"
MINIMAP_PARTY_RING = "#9BE8FF"  # the ring that marks a group member

# Angles from the game need no correction at all, which took a while to
# establish and two wrong guesses along the way.
#
# The camera's curDirection was measured against ground truth — the azimuth
# from the render camera's own pos to its target, read out of h3d.Camera — and
# the two agree to 0.00 degrees through a full swing. So curDirection IS the
# world azimuth of the view, in the same frame as posx/posy, and rotationZ is
# the same convention for entities.
#
# The sign flip and half-turn offset that used to live here were compensating
# for a misread, not for the game. What they actually did was mirror the map,
# which is why it never quite made sense to look at: on a mirrored map every
# turn goes the wrong way and no single fix ever makes it right.
#
# The game's ground plane is the opposite handedness to the screen's, so with
# forward drawn up, the player's right-hand side comes out on the LEFT. Found
# the honest way: an enemy standing to the left was being drawn to the right.
#
# Mirroring the horizontal axis once, here, is the whole fix. It is also what
# the two earlier "corrections" in this file were flailing at — a yaw sign flip
# and a half-turn offset, both of which rotate rather than mirror, and no
# amount of rotation turns a mirrored map the right way round. That is why
# every fix moved the problem somewhere else instead of ending it.
#
# One consequence worth noting, since it reads as a bug either way: on the
# correct map, turning the camera left sweeps the world left. The map shows the
# world relative to you, and both axes have to agree about which way that is.
MINIMAP_MIRROR_X = -1.0

# A flat map can't tell you that a mob is on the gantry above you or in the
# tunnel below, and those are very different news. Anything further than this
# in elevation is drawn faded toward the background rather than hidden, so it
# still reads as present but not as something you can walk to.
# 30 rather than something tighter because the measured distribution is
# bimodal, not gradual: everything on your own floor came in at 0-12 units of
# elevation (slopes and ledges), and everything genuinely on another level at
# 154-173. Anywhere in that gap gives the same answer, so this sits clear of
# terrain rather than close to it.
# Smoothing for the game's own streaming churn — see WorldSnapshot._steady.
# A marker is drawn once it has been present ACROSS this long, which at any
# refresh rate means at least two sweeps and so never a single-frame flash.
#
# 0.25 rather than a round 0.30 because the default sweep is 150ms and 0.30 is
# exactly two of them: the third sighting then spans the threshold to within
# float error (measured 0.2999999999999545 >= 0.30 == False) and the marker
# waits an extra tick. A threshold that lands on a multiple of the tick rate is
# a coin flip; this one sits between two.
MARKER_SHOW_SECS = 0.25
# ...and kept this long after it stops arriving. Sized from the measured
# dropouts, which ran to 2.1s; under that and the marker still blinks, well
# over it and a looted chest sits on the map for no reason.
MARKER_KEEP_SECS = 2.5
# World units of player movement between sweeps that means "somewhere else
# entirely" — a teleport, a rift, a zone change. Held markers are wrong rather
# than late after one of those, so the tracker is dropped. Well above anything
# running or mounted covers in a sweep, and well below a zone hop.
MARKER_RESET_JUMP = 300.0

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
# Theme choices offered in the control menu. The two Dynamic ones are the
# interesting entries: they follow the game, so the overlay matches whatever
# you're standing in.
#
# Farever and Dark differ only in the MAP PANELS — the minimap's background and
# the compass ink. The meter and breakdown are parchment either way; that's the
# app's face and there's no dark version of it. Farever paints the map to match
# them; Dark leaves it the deep navy the panels have always been.
#
# There is deliberately no "Farever Rift" or "Dark Rift". A rift looks like a
# rift, and inside one both Dynamic modes go there — which is the whole point of
# them. Pinned Rift stays available for anyone who just likes the colours.
THEME_MODES = ("Farever Dynamic", "Dark Dynamic", "Farever", "Dark", "Rift")
# What a fresh install gets, and where an unrecognised saved value lands. Not
# THEME_MODES[0]: the dark panels are what the meter has always shipped with,
# and a new option shouldn't repaint anybody's overlay on upgrade.
THEME_MODE_DEFAULT = "Dark Dynamic"
# Settings written before the split said "Dynamic", which drew dark panels — so
# it maps to Dark Dynamic, not to the entry that merely has the same first word.
# The old "Farever" and "Rift" keep their names and now mean what they say.
THEME_MODE_ALIASES = {"Dynamic": "Dark Dynamic"}

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
# The independently-scaled window groups, in the order the menu lists them.
# Wide enough for the longest label in the menu ("Damage meter"), so the label
# column is uniform and every control lines up under it.
FIELD_LABEL_CHARS = 13

SCALE_GROUPS = (
    ("meter", "Meter"),
    ("detail", "Breakdown"),
    ("menu", "Settings"),
    ("minimap", "Minimap"),
    ("compass", "Compass"),
)
# Wider than the UI's: a minimap is worth making genuinely large on a big
# screen, and genuinely small when it's only there for a glance.
MINIMAP_SCALE_MIN, MINIMAP_SCALE_MAX = 50, 250
# Minimum widths, at 100%. They're pixel values, so the scale slider has to
# scale them too or scaling down just hits the floor and nothing moves.
MIN_W = {"meter": 360, "detail": 320, "menu": 230, "prompt": 320}
WARN_WRAP = 460                            # the red banner's wrap, at 100%

MAP_BODY_DARK = "#121C30"       # the deep navy the panels shipped with
MAP_BODY_FAREVER = BG_BODY_SOFT  # ...and the parchment version of the same

THEME_DEFAULT = {
    "border": BG_BORDER, "body": BG_BODY, "soft": BG_BODY_SOFT,
    "header": BG_HEADER, "header_combat": BG_HEADER_COMBAT,
    "header_unlocked": BG_HEADER_UNLOCKED, "track": BG_BAR_TRACK,
    "fg_header": FG_HEADER, "fg_header_dim": FG_HEADER_DIM,
    "fg_text": FG_TEXT, "fg_value": FG_VALUE, "fg_dim": FG_DIM,
    "accent": ACCENT, "dmg": DMG_BAR, "heal": HEAL_BAR,
    "header_off": "#4A4441",
    # The map matches the meter on this theme. The panel used to be dark on
    # every theme, on the reasoning that a map is not a damage table — still
    # true, and still why Dark exists; but "make it look like the rest of the
    # overlay" is a legitimate thing to want and it now has an entry.
    "map_body": MAP_BODY_FAREVER,
}
# The dark overlay: the same layout in the map panel's navy, meter and
# breakdown included. Built on top of Farever rather than from scratch so a key
# added to one theme can't be missing from this one — but almost every value is
# overridden, because a dark theme is not a light theme with a darker box.
#
# What deliberately does NOT change: the damage and healing bars keep their blue
# and green, and green still means "the overlay is unlocked". Those three carry
# meaning, and a theme that recoloured them would be renaming the language the
# meter is written in.
THEME_DARK = dict(
    THEME_DEFAULT,
    border="#080D18",       # near-black navy; the panel edge
    body="#141E33",         # one step up from the map, so the map reads as inset
    soft="#1C2942",         # separators and the breakdown's column rule
    header="#2E6B70",       # the teal, taken down to sit on a dark body
    header_combat="#A94F22",
    track="#22304D",        # bar troughs
    fg_header="#FFFFFF", fg_header_dim="#CFE3E5",
    fg_text="#C6D3E8", fg_value="#FFFFFF", fg_dim="#7E8CA6",
    accent="#7FD4D4",       # headings; the teal lifted to read on navy
    header_off="#2A3346",   # a header bar whose element is hidden
    map_body=MAP_BODY_DARK,
)
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
    "map_body": RIFT_BODY,
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


def _contrast_ink(bg, dark=MINIMAP_Z_MARK_DARK, light=MINIMAP_Z_MARK_LIGHT):
    """Black or white, whichever is readable on `bg`. Rec. 601 luma, which is
    close enough for picking between two extremes."""
    try:
        r, g, b = (int(bg[i:i + 2], 16) for i in (1, 3, 5))
    except (ValueError, IndexError):
        return dark
    return dark if (0.299 * r + 0.587 * g + 0.114 * b) > 140 else light


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
    so this just holds what arrived.

    ...with one exception, which is what `_steady` below is for. The game's own
    entity lists churn at distance, and it shows up on the map as markers
    blinking. Measured over 191 frames (28.7s):

      * five entities — a chest and three orbs in one distant cluster —
        appeared in a SINGLE frame and were never seen again, and
      * four distant markers (838-1240u) vanished together for 8 and then 14
        frames, about 1.2s and 2.1s, before coming back.

    Both are the game streaming, not the sweep: they arrive and leave in
    clusters, and nothing here can stop it. So the picture is smoothed on the
    way in — see MARKER_SHOW_SECS and MARKER_KEEP_SECS."""

    def __init__(self):
        self._lock = threading.Lock()
        self.me = {"x": 0, "y": 0, "r": 0.0}
        self.ents = []
        self.stamp = 0.0
        # key -> [entity, first_seen, last_seen]. Only what the game sends is
        # ever in here; this decides WHEN it's drawn, never what.
        self._tracked = {}
        # Both come from the hook's `hero` message, which already carries the
        # group roster it reads for the meter's party filter. Reusing it means
        # "who is in my group" has one answer, and it covers group members who
        # haven't dealt damage yet — which the meter's own per-player flag
        # can't, since that's only set when someone lands a hit.
        self.party = frozenset()
        self.local = None
        # name -> class, harvested from the sweep. The meter's own rows come
        # from damage events, which carry no class, so this is where it lives.
        self.classes = {}

    @staticmethod
    def _key(e):
        """A stable identity for an entity across frames, or None to pass it
        straight through unsmoothed.

        Static things are keyed by where they are — the sweep rounds positions
        to whole units and scenery doesn't move, so this is exact. Players are
        keyed by name, which follows them as they run. Foes get no key on
        purpose: they move, they're unnamed until the CDB lookup lands, and
        they're the one category where a delay would matter — a mob appearing
        late is worse than a mob flickering."""
        cat = e.get("c")
        if cat == "hero":
            return ("hero", e.get("n")) if e.get("n") else None
        if cat == "foe":
            return None
        return (cat, e.get("x"), e.get("y"), e.get("z"))

    def _steady(self, ents, now):
        """The entities worth drawing, given what's been seen recently.

        Two rules, one for each way the game's churn shows up:

        * nothing is drawn until it has been present for MARKER_SHOW_SECS, which
          is what kills the single-frame flashes, and
        * something that stops arriving is kept for MARKER_KEEP_SECS, which
          bridges the multi-second dropouts.

        The cost of the second rule is that a chest you just looted lingers for
        a moment. That's the right side to err on: the alternative is the map
        twitching at you while you're trying to read it."""
        fresh = {}
        for e in ents:
            key = self._key(e)
            if key is None:
                continue
            fresh[key] = e
        # Age out what hasn't been seen in a while, and refresh what has.
        for key, ent in fresh.items():
            row = self._tracked.get(key)
            if row is None:
                self._tracked[key] = [ent, now, now]
            else:
                row[0], row[2] = ent, now
        for key in [k for k, r in self._tracked.items()
                    if now - r[2] > MARKER_KEEP_SECS]:
            del self._tracked[key]
        out = [e for e in ents if self._key(e) is None]     # foes, unsmoothed
        for _key, (ent, first, last) in self._tracked.items():
            # Seen ACROSS at least that long — not "first seen that long ago".
            # A one-frame flash has last == first and never qualifies; were this
            # measured against now, the flash would sit in its grace period
            # quietly ageing until it passed the test, which is the exact
            # marker this exists to suppress.
            if last - first >= MARKER_SHOW_SECS:
                out.append(ent)
        return out

    def update(self, payload):
        with self._lock:
            me = payload.get("me") or self.me
            now = time.monotonic()
            # A big jump means a teleport, a rift or a zone change, and every
            # marker being held over from the last place is then wrong rather
            # than merely late. Cheaper and more reliable than watching for the
            # events that cause it, since it catches all of them.
            if math.hypot(me.get("x", 0) - self.me.get("x", 0),
                          me.get("y", 0) - self.me.get("y", 0)) > MARKER_RESET_JUMP:
                self._tracked.clear()
            self.me = me
            self.ents = self._steady(payload.get("ents") or [], now)
            self.stamp = now
            for e in self.ents:
                if e.get("c") == "hero" and e.get("n") and e.get("k"):
                    # Kept rather than replaced wholesale: a player who walks
                    # out of range shouldn't lose their tag on the meter while
                    # their damage is still on it.
                    self.classes[e["n"]] = e["k"]

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

    def class_of(self, name):
        with self._lock:
            return self.classes.get(name)

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


def _main_hwnd_of_pid(pid):
    """The process's largest visible top-level window, or None.

    Same enumeration _window_rect_of_pid does, kept separate because the caller
    wants the handle rather than the rectangle."""
    if sys.platform != "win32":
        return None
    u = ctypes.windll.user32
    u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                           ctypes.POINTER(wintypes.DWORD)]
    best = {"area": 0, "hwnd": None}
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
                best["area"], best["hwnd"] = area, hwnd
        return True

    try:
        u.EnumWindows(WNDENUMPROC(visit), 0)
    except Exception:
        return None
    return best["hwnd"]


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
WH_KEYBOARD_LL, WH_MOUSE_LL, HC_ACTION = 13, 14, 0
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
WM_MBUTTONDOWN, WM_XBUTTONDOWN = 0x0207, 0x020B
WM_HOTKEY = 0x0312
WM_REBIND = 0x0400 + 1               # WM_APP+1, posted to the hotkey thread
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x4000
# Thread id of the RegisterHotKey fallback's pump, or 0 while the low-level
# hook is doing the work (which needs no re-registration at all).
REBIND_TO = [0]

# Shift+\ (reset the encounter) is the only key the meter still owns — every
# other control moved onto the in-game control menu. Plain \ and / are left
# alone now so the game keeps them.
HK_RESET = 1

# The reset keybind, rebindable from the control menu. A dict rather than a
# constant because the hook thread reads it on every keypress: rebinding is
# then a matter of writing new values here, with no hook to tear down and
# reinstall. Mutated in place for the same reason — the thread closed over this
# object, not over the name.
RESET_BIND = {"vk": VK_OEM_5, "shift": True, "ctrl": False, "alt": False}
RESET_BIND_DEFAULT = dict(RESET_BIND)
# Virtual-key codes whose names aren't derivable. Everything else falls back to
# its character (A-Z, 0-9 are their own VK) or a bare hex code, so an unusual
# keyboard shows something rather than nothing.
VK_NAMES = {
    0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x13: "Pause",
    0x14: "Caps Lock", 0x1B: "Esc", 0x20: "Space", 0x21: "Page Up",
    0x22: "Page Down", 0x23: "End", 0x24: "Home", 0x25: "Left", 0x26: "Up",
    0x27: "Right", 0x28: "Down", 0x2D: "Insert", 0x2E: "Delete",
    0x6A: "Num *", 0x6B: "Num +", 0x6D: "Num -", 0x6E: "Num .", 0x6F: "Num /",
    0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/",
    0xC0: "`", 0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'",
}
VK_NAMES.update({0x60 + i: f"Num {i}" for i in range(10)})
VK_NAMES.update({0x70 + i: f"F{i + 1}" for i in range(24)})
# Mouse buttons are bindable too, but only these three. Left and right belong
# to the game and always will; the hook SWALLOWS whatever it fires on, and
# taking left-click away from somebody mid-fight is not a setting, it's a
# hostage situation. Middle and the two side buttons are fair game.
VK_MOUSE = {0x04: "Middle Click", 0x05: "Mouse 4", 0x06: "Mouse 5"}
VK_NAMES.update(VK_MOUSE)
# Modifiers can't be the key itself, and Escape is how you back out of the
# capture — binding it would leave no way to cancel.
VK_UNBINDABLE = frozenset({0x10, 0x11, 0x12, 0x1B, 0x5B, 0x5C,
                           0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5})


def _vk_name(vk):
    if vk in VK_NAMES:
        return VK_NAMES[vk]
    if 0x30 <= vk <= 0x5A:          # 0-9 and A-Z share their ASCII codes
        return chr(vk)
    return f"VK {vk:#04x}"


def bind_label(bind=None):
    """"Shift + \\" — what the menu button and the floating hint both show, so
    they can't drift apart."""
    b = bind or RESET_BIND
    parts = [n for n, k in (("Ctrl", "ctrl"), ("Alt", "alt"), ("Shift", "shift"))
             if b.get(k)]
    parts.append(_vk_name(b.get("vk", VK_OEM_5)))
    return " + ".join(parts)


def reset_hint_text():
    return f"Reset FareverPlus - {bind_label()}"

# ---------------------------------------------------------------------------
# Version / update check
# ---------------------------------------------------------------------------
# Bump this on every release, and tag the repo with the same string — it's the
# left-hand side of the comparison below, so a release that forgets it tells
# everyone they're out of date forever.
VERSION = "2.3.1"

REPO = "brudrbear/FareverMeter"
REPO_URL = f"https://github.com/{REPO}"
UPDATE_API_RELEASE = f"https://api.github.com/repos/{REPO}/releases/latest"
UPDATE_API_TAGS = f"https://api.github.com/repos/{REPO}/tags"
UPDATE_TIMEOUT = 5.0
# Filled in by the checker thread, read by the overlay's refresh tick.
UPDATE = {"latest": None, "url": REPO_URL}

QUIT_LABEL = "Stop the meter"

# The top line of the control menu. It used to shout about Ctrl+C, from when
# the only way to run the meter was a console you could close out from under
# it. The shipped build has no console and two proper exits, so the line just
# says where they are — and doubles as the slot the update notice takes over.
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
            # The bound key only, and only while Farever has focus. Everything
            # else falls through untouched so the game keeps its own bindings —
            # which matters more than usual here, because the branch below
            # SWALLOWS the keypress.
            #
            # RESET_BIND is read fresh every time rather than captured: that's
            # what makes rebinding take effect immediately instead of at the
            # next launch.
            if vk != RESET_BIND.get("vk") or fg_pid() != target_pid:
                return u.CallNextHookEx(None, nCode, wParam, lParam)
            # Every modifier has to match exactly — a binding of Shift+\ must
            # not fire on Ctrl+Shift+\, which is somebody else's shortcut.
            if (pressed(VK_SHIFT) != bool(RESET_BIND.get("shift"))
                    or pressed(VK_CONTROL) != bool(RESET_BIND.get("ctrl"))
                    or pressed(VK_MENU) != bool(RESET_BIND.get("alt"))):
                return u.CallNextHookEx(None, nCode, wParam, lParam)
            cb = callbacks.get(HK_RESET)
            if cb:
                try:
                    cb()
                except Exception as e:
                    print("[hotkey]", e, file=sys.stderr)
            return 1

        class MSLL(ctypes.Structure):
            _fields_ = [("pt_x", wintypes.LONG), ("pt_y", wintypes.LONG),
                        ("mouseData", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.c_void_p)]

        def mouse_proc(nCode, wParam, lParam):
            # A separate hook because WH_KEYBOARD_LL cannot see mouse buttons
            # at all — nor can RegisterHotKey, which is why a mouse binding
            # only works on this path.
            # First line, and it matters: a low-level mouse hook is called for
            # every WM_MOUSEMOVE too, which on a 1000Hz mouse is a thousand
            # trips into Python a second, each one in front of the input it's
            # inspecting. Everything that isn't a button press leaves here.
            if wParam not in (WM_MBUTTONDOWN, WM_XBUTTONDOWN):
                return u.CallNextHookEx(None, nCode, wParam, lParam)
            if nCode != HC_ACTION:
                return u.CallNextHookEx(None, nCode, wParam, lParam)
            vk = 0
            if wParam == WM_MBUTTONDOWN:
                vk = 0x04
            elif wParam == WM_XBUTTONDOWN:
                # Which side button is in the HIGH word of mouseData: 1 or 2.
                ms = ctypes.cast(lParam, ctypes.POINTER(MSLL))[0]
                vk = 0x04 + ((ms.mouseData >> 16) & 0xFFFF)     # -> 0x05, 0x06
            if vk != RESET_BIND.get("vk") or fg_pid() != target_pid:
                return u.CallNextHookEx(None, nCode, wParam, lParam)
            if (pressed(VK_SHIFT) != bool(RESET_BIND.get("shift"))
                    or pressed(VK_CONTROL) != bool(RESET_BIND.get("ctrl"))
                    or pressed(VK_MENU) != bool(RESET_BIND.get("alt"))):
                return u.CallNextHookEx(None, nCode, wParam, lParam)
            cb = callbacks.get(HK_RESET)
            if cb:
                try:
                    cb()
                except Exception as e:
                    print("[hotkey]", e, file=sys.stderr)
            return 1

        cproc = HOOKPROC(proc)
        cmproc = HOOKPROC(mouse_proc)
        hMod = ctypes.windll.kernel32.GetModuleHandleW(None)
        hook = u.SetWindowsHookExW(WH_KEYBOARD_LL, cproc, hMod, 0)
        from ctypes import wintypes
        if hook:
            # Installed unconditionally rather than only when a mouse button is
            # bound: the binding can change at any moment from the menu, and a
            # hook that has to be installed from this thread can't be added
            # later without waking it up.
            if not u.SetWindowsHookExW(WH_MOUSE_LL, cmproc, hMod, 0):
                print("[meter] mouse hook failed; mouse buttons can't be bound.",
                      file=sys.stderr)
            print("[meter] focus-conditional hotkeys active.", file=sys.stderr)
            msg = wintypes.MSG()
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                u.TranslateMessage(ctypes.byref(msg))
                u.DispatchMessageW(ctypes.byref(msg))
            return
        # ---- Fallback: global RegisterHotKey (fires regardless of focus) ----
        print("[meter] LL hook failed; using global RegisterHotKey fallback.",
              file=sys.stderr)

        def register():
            u.UnregisterHotKey(None, HK_RESET)
            mods = MOD_NOREPEAT
            if RESET_BIND.get("shift"):
                mods |= MOD_SHIFT
            if RESET_BIND.get("ctrl"):
                mods |= MOD_CONTROL
            if RESET_BIND.get("alt"):
                mods |= MOD_ALT
            if not u.RegisterHotKey(None, HK_RESET, mods,
                                    RESET_BIND.get("vk", VK_OEM_5)):
                print(f"[meter] {bind_label()} unavailable (another app owns "
                      "it) — the encounter still resets itself on a zone "
                      "change or after a lull, but the manual reset won't "
                      "fire.", file=sys.stderr)

        register()
        # RegisterHotKey belongs to the thread that called it, so a rebind
        # can't just re-register from the Tk thread. The overlay posts
        # WM_REBIND here instead and this thread does it — see _rebind_reset.
        REBIND_TO[0] = ctypes.windll.kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_REBIND:
                register()
                continue
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


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hCursor", ctypes.c_void_p), ("ptScreenPos", POINT)]


CURSOR_SHOWING = 0x1


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
                 world=None, configure=None):
        self.session = session
        self.target_pid = target_pid
        self.ui_state = ui_state if ui_state is not None else GameUIState()
        self.world = world if world is not None else WorldSnapshot()
        # Pushes settings to the running hook (currently just the sweep rate).
        # A no-op when there's no hook, so the overlay stays testable on its own.
        self._configure = configure or (lambda **kw: None)
        self.focus_player = None       # drilled-in player name (None => local)
        self.mode = "party"            # "party" (group only) or "all"
        # The overlay is click-through unless the game has freed the cursor for
        # its escape menu. There is no manual lock any more — the game's own UI
        # state is the single source of truth.
        self._menu_unlock = False
        self._hide_ooc = False         # "hide out of combat" setting
        # _show is what the player asked for, _shown is what's actually mapped
        # (they differ while out-of-combat hiding is in effect).
        self._show = {k: ELEMENT_SHOW for k, _ in TOGGLEABLE_ELEMENTS}
        # Healing is columns inside the meter, not a window of its own, so it
        # stays a plain on/off rather than gaining a mode it can't honour.
        self._show_heal = True
        self._shown = {k: True for k, _ in TOGGLEABLE_ELEMENTS}
        self._heal_cols_shown = True   # last healing layout pushed to the widgets
        self._combat_seen_at = 0.0     # last moment a tracked player was fighting
        self._header_bg = BG_HEADER    # last tint pushed to the header bars
        self._theme = THEME_DEFAULT    # what's painted right now
        self._theme_mode = THEME_MODE_DEFAULT   # what the player asked for
        self._action_q = []
        self._q_lock = threading.Lock()
        self._quit_armed = False       # the Quit button's second-click window
        self._update_shown = False     # the update notice is applied once
        self._map_mode = MINIMAP_MODES[0]   # "Rotating" — you always face up
        self._transparency = 0              # percent, on top of OVERLAY_ALPHA
        # Per-category ticks for each panel; everything on until told otherwise.
        self._map_filters = {key: True for key, _label, _cats in MINIMAP_FILTERS}
        self._compass_filters = {key: True
                                 for key, _label, _cats in COMPASS_FILTERS}
        self._map_rate = "High"        # Ultra exists but is opt-in
        # One scale per window group. Each window wants a size that suits its
        # job — the meter one that suits reading numbers, the map one that
        # suits the screen it covers — and a single slider means at least one
        # of them is always wrong.
        self._scales = {group: 1.0 for group, _label in SCALE_GROUPS}
        self._game_hwnd = None         # cached; re-resolved if it goes stale
        self._cursor_free = False      # game has released the mouse (Alt, menus)
        self._focused = True           # Farever is the window you're looking at
        self._last_cam = None          # last camera heading seen; see _draw_minimap
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

        # Before any widget exists: the dropdowns and Show/hide ticks are built
        # from these, so loading afterwards would leave the menu disagreeing
        # with the state it is supposed to be showing.
        self._pending_scales = None
        self._load_settings()

        pos = self._load_positions()
        self.root = tk.Tk()
        self._ui_scale = 1.0
        # One font set per independently-scaled window group. Tk fonts are
        # shared objects, so resizing one would resize every widget using it —
        # separate scales mean separate sets, not a cleverer setter.
        #   fonts    the meter, and the small floating bits that belong with it
        #            (rift timer, hint, parse banner, rift prompt)
        #   fonts_d  the breakdown
        #   fonts_m  the control menu
        #   fonts_map the minimap
        def _font_set():
            out = {}
            for key, (family, size, *style) in FONT_SPECS.items():
                out[key] = tkfont.Font(
                    root=self.root, family=family, size=size,
                    weight="bold" if "bold" in style else "normal",
                    slant="italic" if "italic" in style else "roman")
            return out

        self.fonts = _font_set()
        self.fonts_d = _font_set()
        self.fonts_m = _font_set()
        self.fonts_map = _font_set()
        self.fonts_compass = _font_set()
        self._font_sets = {"meter": self.fonts, "detail": self.fonts_d,
                           "menu": self.fonts_m, "minimap": self.fonts_map,
                           "compass": self.fonts_compass}
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
        self.compasswin = tk.Toplevel(self.root)
        self.compasswin.title("Farever+ Compass")
        for win in (self.root, self.detail, self.menu, self.hintwin,
                    self.parsewin, self.promptwin, self.riftwin, self.mapwin,
                    self.compasswin):
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
                    self.mapwin, self.compasswin):
            win.attributes("-alpha", OVERLAY_ALPHA)
        # Keys must match TOGGLEABLE_ELEMENTS.
        self._element_win = {"meter": self.root, "detail": self.detail,
                             "rift": self.riftwin, "minimap": self.mapwin,
                             "compass": self.compasswin}
        # Every window that fades: the two toggleable ones, plus the control
        # menu and its hint, which follow the game's escape menu.
        self._fade_win = dict(self._element_win, menu=self.menu,
                              hint=self.hintwin, prompt=self.promptwin)
        self._shown["menu"] = self._shown["hint"] = False
        self._shown["prompt"] = False
        self._shown["rift"] = False     # nothing to show until a timer arrives
        # Live opacity of each faded window, driven by _step_fade. The menu pair
        # starts at zero: they're withdrawn until the escape menu opens.
        # Seeded from the saved slider, not from the constant: a restart should
        # come up wearing the transparency you left it on, without a visible
        # settle from full opacity.
        self._alpha = {k: self._alpha_for(k) for k in self._fade_win}
        self._alpha["menu"] = self._alpha["hint"] = 0.0
        self._alpha["prompt"] = self._alpha["rift"] = 0.0
        for key, win in self._fade_win.items():
            if self._alpha[key]:
                win.attributes("-alpha", self._alpha[key])
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
        self._build_compass()
        self.root.update_idletasks()
        self._place_windows(pos)
        # Restored scales can only be applied now: they resize the fonts every
        # window has already been packed against. The sliders are set from them
        # too, or the menu would read 100% while the window is at 125.
        for group, factor in (self._pending_scales or {}).items():
            if group in self._scales and abs(factor - 1.0) > 0.001:
                self._scale_vars[group].set(int(round(factor * 100)))
                self._set_group_scale(group, factor)
        self._pending_scales = None
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
        for key in ("meter", "detail", "menu", "rift", "minimap", "compass"):
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
        cp = pos.get("compass")
        if cp and self._pos_visible(*cp):
            self.compasswin.geometry(f"+{cp[0]}+{cp[1]}")
        else:
            self._default_compass_pos()
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

    def _default_compass_pos(self):
        """Top-centre of the game window, where a compass belongs and where
        nothing else of ours sits."""
        self.compasswin.update_idletasks()
        l, t, r, _b = self._game_rect()
        w = max(self.compasswin.winfo_reqwidth(),
                self.compasswin.winfo_width(), 200)
        self.compasswin.geometry(f"+{l + ((r - l) - w) // 2}+{t + 8}")

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

    def _load_settings(self):
        """Read the saved settings, before any widget is built from them.

        Every value is validated against what the build actually offers rather
        than trusted: a file written by a newer version, or hand-edited, should
        cost you that one setting and not the meter."""
        try:
            data = json.loads(SETTINGS_CACHE.read_text())
        except Exception:
            return                      # absent or unreadable => defaults
        if not isinstance(data, dict):
            return
        # Aliased before the check, so a value written by an older build lands
        # on the entry that draws what that build drew rather than falling
        # through to the default and quietly changing someone's overlay.
        theme = THEME_MODE_ALIASES.get(data.get("theme"), data.get("theme"))
        if theme in THEME_MODES:
            self._theme_mode = theme
        if data.get("map_mode") in MINIMAP_MODES:
            self._map_mode = data["map_mode"]
        if data.get("map_rate") in MINIMAP_RATE_MS:
            self._map_rate = data["map_rate"]
        t = data.get("transparency")
        if isinstance(t, int) and 0 <= t <= TRANSPARENCY_MAX:
            self._transparency = t
        # Validated key by key: a hand-edited or newer file shouldn't be able
        # to leave the meter with a binding the hook can't match.
        bind = data.get("reset_bind")
        if isinstance(bind, dict) and isinstance(bind.get("vk"), int):
            vk = bind["vk"]
            if 0 < vk <= 0xFF and vk not in VK_UNBINDABLE:
                RESET_BIND.update(
                    {"vk": vk} | {m: bool(bind.get(m))
                                  for m in ("shift", "ctrl", "alt")})
        # Per-key rather than wholesale, so a file written before a category
        # existed leaves that one at its default instead of dropping the lot.
        for name, table, into in (
                ("map_filters", MINIMAP_FILTERS, self._map_filters),
                ("compass_filters", COMPASS_FILTERS, self._compass_filters)):
            saved_filters = data.get(name)
            if not isinstance(saved_filters, dict):
                continue
            for key, _label, _cats in table:
                if isinstance(saved_filters.get(key), bool):
                    into[key] = saved_filters[key]
        if data.get("mode") in ("party", "all"):
            self.mode = data["mode"]
        if isinstance(data.get("hide_ooc"), bool):
            self._hide_ooc = data["hide_ooc"]
        # Scales can only be applied once the fonts exist, so they're parked
        # here and used after the windows are built.
        saved = data.get("scales")
        if not isinstance(saved, dict):
            # Written by the build with one global slider plus a separate
            # minimap one. The global value becomes the meter's, which is the
            # window it mostly stood for.
            saved = {"meter": data.get("ui_scale"),
                     "minimap": data.get("map_scale")}
        pending = {}
        for group, _label in SCALE_GROUPS:
            lo, hi = ((MINIMAP_SCALE_MIN, MINIMAP_SCALE_MAX) if group == "minimap"
                      else (UI_SCALE_MIN, UI_SCALE_MAX))
            try:
                v = float(saved.get(group))
            except (TypeError, ValueError):
                continue
            if lo <= v * 100 <= hi:
                pending[group] = v
        self._pending_scales = pending
        if isinstance(data.get("show_heal"), bool):
            self._show_heal = data["show_heal"]
        show = data.get("show")
        if isinstance(show, dict):
            for key, _label in TOGGLEABLE_ELEMENTS:
                v = show.get(key)
                if v in ELEMENT_MODES:
                    self._show[key] = v
                elif isinstance(v, bool):
                    # Written by a build that had ticks rather than modes. An
                    # unticked box then meant "hidden while playing, back when
                    # the menu opens", which is exactly Show in ESC — so the
                    # setting survives the upgrade instead of silently changing
                    # behaviour.
                    self._show[key] = ELEMENT_SHOW if v else ELEMENT_ESC
            self._shown = {k: True for k, _ in TOGGLEABLE_ELEMENTS}

    def _save_settings(self):
        """Write the settings out. Called on every change rather than at exit —
        the meter is normally stopped from the tray or displaced by a newer
        instance, and neither is a good moment to be doing first-time IO."""
        try:
            SETTINGS_CACHE.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_CACHE.write_text(json.dumps({
                "theme": self._theme_mode,
                "map_mode": self._map_mode,
                "map_rate": self._map_rate,
                "transparency": self._transparency,
                "reset_bind": dict(RESET_BIND),
                "map_filters": {k: bool(self._map_filters.get(k, True))
                                for k, _label, _cats in MINIMAP_FILTERS},
                "compass_filters": {
                    k: bool(self._compass_filters.get(k, True))
                    for k, _label, _cats in COMPASS_FILTERS},
                "mode": self.mode,
                "hide_ooc": self._hide_ooc,
                "scales": {g: round(self._scales[g], 3)
                           for g, _label in SCALE_GROUPS},
                "show": {k: self._show.get(k, ELEMENT_SHOW)
                         for k, _label in TOGGLEABLE_ELEMENTS},
                "show_heal": bool(self._show_heal),
            }, indent=2))
        except OSError as e:
            print(f"[meter] couldn't save settings: {e}", file=sys.stderr)

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
                "compass": {"x": self.compasswin.winfo_x(),
                            "y": self.compasswin.winfo_y()},
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
        self._bind_drag(self.root, (self.header, self.title_lbl),
                        unlocked=self._mouse_available)

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
        head = (f"  #  {'NAME':<{METER_NAME_CELLS}}{'CLS':<{METER_CLASS_CELLS}}"
                f"{'DMG':>9} {'DPS':>6} {'%':>4}")
        return head + (f"{'HEAL':>9}" if self._show_heal else "")

    def _build_detail(self):
        self.d_border = border = tk.Frame(self.detail, bg=BG_BORDER, padx=2, pady=2)
        border.pack(fill="both", expand=True)

        self.d_header = tk.Frame(border, bg=BG_HEADER)
        self.d_header.pack(fill="x")
        self.d_title = tk.Label(self.d_header, text="Breakdown",
                                bg=BG_HEADER, fg=FG_HEADER,
                                font=self.fonts_d["ui_b"], anchor="w",
                                padx=8, pady=4)
        self.d_title.pack(side="left")
        # Sits in the header rather than the body so it reads as a caption on
        # the window instead of another data row. It tints with the header.
        self.d_tip = tk.Label(self.d_header,
                              text="Click a player in the meter to view details",
                              bg=BG_HEADER, fg=FG_HEADER_DIM,
                              font=self.fonts_d["ui_tiny_i"], anchor="e", padx=8)
        self.d_tip.pack(side="right")
        self._bind_drag(self.detail, (self.d_header, self.d_title, self.d_tip))

        self.d_body = body = tk.Frame(border, bg=BG_BODY, padx=8, pady=6)
        body.pack(fill="both", expand=True)
        self.stats_lbl = tk.Label(body, text="", bg=BG_BODY, fg=FG_TEXT,
                                  font=self.fonts_d["mono"], anchor="w")
        self.stats_lbl.pack(fill="x")

        self.d_cols = cols = tk.Frame(body, bg=BG_BODY)
        cols.pack(fill="x", pady=(3, 2))
        self.dmg_col = SkillColumn(cols, "DAMAGE", DMG_BAR, self.fonts_d)
        self.dmg_col.f.pack(side="left", anchor="n")
        # Kept as attributes so the healing toggle can unpack them; re-packing
        # in this order puts them back to the right of the damage column.
        self.col_sep = tk.Frame(cols, bg=BG_BODY_SOFT, width=1)
        self.col_sep.pack(side="left", fill="y", padx=6)
        self.heal_col = SkillColumn(cols, "HEALING", HEAL_BAR, self.fonts_d)
        self.heal_col.f.pack(side="left", anchor="n")

        self.elem_lbl = tk.Label(body, text="", bg=BG_BODY, fg=FG_DIM,
                                 font=self.fonts_d["mono_sm"], anchor="w", justify="left")
        self.elem_lbl.pack(fill="x", pady=(3, 0))
        self.detail.minsize(MIN_W["detail"], 0)

    def _build_menu(self):
        """The control menu: what used to be hotkeys, as buttons. Only on screen
        while the game's escape menu is — which is also the only time the game
        has a usable cursor — so it never needs to be click-through."""
        # Every OptionMenu on the panel, so their popups can be dismissed with
        # it. A posted dropdown is its own toplevel and knows nothing about the
        # window it belongs to — hide the menu with one open and the list of
        # choices stays on screen by itself. See _unpost_menus.
        self._option_menus = []
        border = tk.Frame(self.menu, bg=BG_BORDER, padx=2, pady=2)
        border.pack(fill="both", expand=True)

        # Green like the other two headers get while it's on screen — the menu
        # only ever exists in the unlocked state, so this never changes.
        self.m_header = tk.Frame(border, bg=BG_HEADER_UNLOCKED)
        self.m_header.pack(fill="x")
        self.m_title = tk.Label(self.m_header, text="Farever+ Controls",
                                bg=BG_HEADER_UNLOCKED, fg=FG_HEADER,
                                font=self.fonts_m["ui_b"], anchor="w",
                                padx=8, pady=4)
        self.m_title.pack(side="left")
        self._bind_drag(self.menu, (self.m_header, self.m_title))

        body = tk.Frame(border, bg=BG_BODY, padx=8, pady=8)
        body.pack(fill="both", expand=True)

        # Top of the menu: how to stop the meter, quietly. Both exits unload
        # the hook and detach, so there's nothing left to warn about — and when
        # a new version is out this line becomes the notice for it.
        self.warn_lbl = tk.Label(body, text=SHUTDOWN_HINT,
                                 bg=BG_BODY, fg=FG_DIM,
                                 font=self.fonts_m["ui_sm_b"],
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
                     font=self.fonts_m["ui_sm_b"], anchor="w").pack(side="left")
            if note:
                # Quieter than the heading it hangs off: it's a note about how
                # the section behaves, not another thing to read every time.
                tk.Label(row, text=note, bg=BG_BODY, fg=FG_DIM,
                         font=self.fonts_m["ui_tiny_i"],
                         anchor="e").pack(side="right")

        def button(parent, cmd):
            b = tk.Button(parent, text="", command=cmd, anchor="w",
                          font=self.fonts_m["ui"], bg=BG_BODY_SOFT, fg=FG_TEXT,
                          activebackground=BG_BAR_TRACK, activeforeground=FG_VALUE,
                          relief="flat", bd=0, padx=10, pady=5,
                          highlightthickness=1, highlightbackground=BG_BAR_TRACK,
                          cursor="hand2")
            b.pack(fill="x", pady=2)
            return b

        def field(parent, label):
            """A labelled control row, for the things that aren't buttons.

            The label column is a fixed width so every control in it starts at
            the same x and ends up the same length. Left to size themselves,
            "Meter" and "Breakdown" hand their sliders different amounts of
            leftover row, and four sliders that should read identically at 100%
            end up visibly different lengths with their handles in different
            places."""
            row = tk.Frame(parent, bg=BG_BODY)
            row.pack(fill="x", pady=2)
            # `width` on a Label is authoritative — it's a requested size in
            # average character widths, and longer text doesn't grow it.
            tk.Label(row, text=label, bg=BG_BODY, fg=FG_TEXT,
                     font=self.fonts_m["ui"], anchor="w", padx=2,
                     width=FIELD_LABEL_CHARS).pack(side="left")
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
            padx=10, pady=3, font=self.fonts_m["ui"], cursor="hand2",
            highlightthickness=1, highlightbackground=BG_BAR_TRACK,
            direction="right")
        self.opt_theme["menu"].config(
            bg=BG_BODY, fg=FG_TEXT, activebackground=BTN_ON_BG,
            activeforeground=FG_HEADER, bd=0, relief="flat",
            font=self.fonts_m["ui"])
        self.opt_theme.pack(side="right", expand=True, fill="x", padx=(8, 0))
        self._option_menus.append(self.opt_theme)

        # Under Theme because that's what it is — how the overlay looks, not
        # what it does. Released rather than live, like the scale sliders: every
        # step reconfigures five windows.
        row = field(left, "Transparency")
        self._transp_var = tk.IntVar(value=self._transparency)
        scl = tk.Scale(
            row, from_=0, to=TRANSPARENCY_MAX, resolution=5,
            orient="horizontal", variable=self._transp_var, showvalue=True,
            bg=BG_BODY, fg=FG_DIM, troughcolor=BG_BAR_TRACK,
            activebackground=BTN_ON_BG, highlightthickness=0, bd=0,
            sliderrelief="flat", font=self.fonts_m["ui_tiny_i"], length=120,
            cursor="hand2")
        scl.bind("<ButtonRelease-1>", lambda _e: self._on_transparency_pick())
        scl.pack(side="right", expand=True, fill="x", padx=(8, 0))

        row = field(left, "Minimap")
        self._map_mode_var = tk.StringVar(value=self._map_mode)
        self.opt_map = tk.OptionMenu(row, self._map_mode_var, *MINIMAP_MODES,
                                     command=self._on_map_mode_pick)
        self.opt_map.config(
            bg=BG_BODY_SOFT, fg=FG_TEXT, activebackground=BG_BAR_TRACK,
            activeforeground=FG_VALUE, relief="flat", bd=0, anchor="w",
            padx=10, pady=3, font=self.fonts_m["ui"], cursor="hand2",
            highlightthickness=1, highlightbackground=BG_BAR_TRACK,
            direction="right")
        self.opt_map["menu"].config(
            bg=BG_BODY, fg=FG_TEXT, activebackground=BTN_ON_BG,
            activeforeground=FG_HEADER, bd=0, relief="flat",
            font=self.fonts_m["ui"])
        self.opt_map.pack(side="right", expand=True, fill="x", padx=(8, 0))
        self._option_menus.append(self.opt_map)

        row = field(left, "Map refresh")
        self._map_rate_var = tk.StringVar(value=self._map_rate)
        self.opt_rate = tk.OptionMenu(row, self._map_rate_var,
                                      *MINIMAP_RATE_NAMES,
                                      command=self._on_map_rate_pick)
        self.opt_rate.config(
            bg=BG_BODY_SOFT, fg=FG_TEXT, activebackground=BG_BAR_TRACK,
            activeforeground=FG_VALUE, relief="flat", bd=0, anchor="w",
            padx=10, pady=3, font=self.fonts_m["ui"], cursor="hand2",
            highlightthickness=1, highlightbackground=BG_BAR_TRACK,
            direction="right")
        self.opt_rate["menu"].config(
            bg=BG_BODY, fg=FG_TEXT, activebackground=BTN_ON_BG,
            activeforeground=FG_HEADER, bd=0, relief="flat",
            font=self.fonts_m["ui"])
        self.opt_rate.pack(side="right", expand=True, fill="x", padx=(8, 0))
        self._option_menus.append(self.opt_rate)

        # Every font in the overlay is a named Tk font, so dragging this resizes
        # the lot. Released rather than live: repainting the whole tree on each
        # pixel of drag is visibly slow.
        section(left, "SCALING")
        self._scale_vars = {}
        for group, label in SCALE_GROUPS:
            row = field(left, label)
            var = tk.IntVar(value=int(round(self._scales[group] * 100)))
            self._scale_vars[group] = var
            lo, hi = (MINIMAP_SCALE_MIN, MINIMAP_SCALE_MAX) if group == "minimap"                 else (UI_SCALE_MIN, UI_SCALE_MAX)
            scl = tk.Scale(
                row, from_=lo, to=hi, resolution=5, orient="horizontal",
                variable=var, showvalue=True, bg=BG_BODY, fg=FG_DIM,
                troughcolor=BG_BAR_TRACK, activebackground=BTN_ON_BG,
                highlightthickness=0, bd=0, sliderrelief="flat",
                font=self.fonts_m["ui_tiny_i"], length=120, cursor="hand2")
            # Released rather than live: repainting a whole window on each
            # pixel of drag is visibly slow.
            scl.bind("<ButtonRelease-1>",
                     lambda _e, g=group: self._on_scale_pick(g))
            scl.pack(side="right", expand=True, fill="x", padx=(8, 0))

        section(left, "SHOW / HIDE")
        self.element_vars = {}
        for key, label in TOGGLEABLE_ELEMENTS:
            row = field(left, label)
            var = tk.StringVar(value=self._show.get(key, ELEMENT_SHOW))
            self.element_vars[key] = var
            opt = tk.OptionMenu(row, var, *ELEMENT_MODES,
                                command=lambda v, k=key: self._on_element_pick(k, v))
            opt.config(
                bg=BG_BODY_SOFT, fg=FG_TEXT, activebackground=BG_BAR_TRACK,
                activeforeground=FG_VALUE, relief="flat", bd=0, anchor="w",
                padx=8, pady=2, font=self.fonts_m["ui"], cursor="hand2",
                highlightthickness=1, highlightbackground=BG_BAR_TRACK,
                direction="right")
            opt["menu"].config(
                bg=BG_BODY, fg=FG_TEXT, activebackground=BTN_ON_BG,
                activeforeground=FG_HEADER, bd=0, relief="flat",
                font=self.fonts_m["ui"])
            opt.pack(side="right", expand=True, fill="x", padx=(8, 0))
            self._option_menus.append(opt)
        # Columns inside the meter rather than a window, so it keeps its tick.
        self.btn_heal = button(left, self._enqueue(self._toggle_heal))
        # Last in this section rather than under OPTIONS: it hides the same
        # windows the dropdowns above do, just on a condition instead of a
        # choice.
        self.btn_hide_ooc = button(left, self._enqueue(self._toggle_hide_ooc))

        # Above ACTIONS: these are settings you leave alone for hours, and the
        # buttons below them are the ones you came here to press.
        section(right, "CONTROLS", first=True)
        row = field(right, "Reset data")
        self.btn_bind = tk.Button(
            row, text="", command=self._begin_bind_capture, anchor="w",
            font=self.fonts_m["ui"], bg=BG_BODY_SOFT, fg=FG_TEXT,
            activebackground=BG_BAR_TRACK, activeforeground=FG_VALUE,
            relief="flat", bd=0, padx=10, pady=3, highlightthickness=1,
            highlightbackground=BG_BAR_TRACK, cursor="hand2")
        self.btn_bind.pack(side="right", expand=True, fill="x", padx=(8, 0))
        # True while the button is listening for a keypress.
        self._binding_now = False

        section(right, "COMPASS")
        self.btn_compass_filter = {}
        for key, label, _cats in COMPASS_FILTERS:
            self.btn_compass_filter[key] = button(
                right,
                self._enqueue(lambda k=key: self._toggle_compass_filter(k)))

        section(right, "MINIMAP")
        self.btn_map_filter = {}
        for key, label, _cats in MINIMAP_FILTERS:
            self.btn_map_filter[key] = button(
                right, self._enqueue(lambda k=key: self._toggle_map_filter(k)))

        section(right, "ACTIONS")
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
            text=f"Reset encounter data   ({bind_label()})")
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
        text = reset_hint_text()
        w = f.measure(text) + pad * 2 + off
        h = f.metrics("linespace") + pad * 2 + off
        c.config(width=w, height=h)
        c.delete("all")
        c.create_text(pad + off, pad + off, text=text, font=f,
                      fill=BG_BORDER, anchor="nw")
        c.create_text(pad, pad, text=text, font=f,
                      fill=BG_BODY, anchor="nw")

    def _build_minimap(self):
        """A square, north-up map of what's around you.

        One canvas, redrawn wholesale each tick. That sounds wasteful and isn't:
        at ~40 entities it's a few dozen canvas items, and tracking item
        identity across ticks — entities appear, move and despawn constantly —
        costs more in bookkeeping than it saves in redraws."""
        self.map_border = tk.Frame(
            self.mapwin, bg=_lerp_hex(THEME_DEFAULT["map_body"], "#000000", 0.45),
            padx=2, pady=2)
        self.map_border.pack(fill="both", expand=True)
        # No title bar. A map that is already a labelled square doesn't need a
        # strip saying "Nearby" over it, and the bar was the tallest piece of
        # chrome on the smallest window. The hover box below takes over as the
        # drag handle — see the note at the end of this method.
        _mb = THEME_DEFAULT["map_body"]
        self.map_canvas = tk.Canvas(self.map_border, bg=_mb,
                                    highlightthickness=0, bd=0,
                                    width=MINIMAP_SIZE, height=MINIMAP_SIZE)
        self.map_canvas.pack()
        # Hover readout: its own box under the map, always present rather than
        # appearing on hover — otherwise the panel changes height under the
        # cursor and shoves the map out from under you mid-read.
        self.map_tipbox = tk.Frame(self.map_border,
                                   bg=_lerp_hex(_mb, "#FFFFFF", 0.16),
                                   padx=1, pady=1)
        self.map_tipbox.pack(fill="x", pady=(3, 0))
        # ...but only while there's a pointer to hover with — see _sync_map_tip.
        self._map_tip_shown = True
        self.map_tip = tk.Label(self.map_tipbox, text=MINIMAP_TIP_IDLE,
                                bg=_lerp_hex(_mb, "#000000", 0.30),
                                fg=_lerp_hex(_mb, "#FFFFFF", 0.55),
                                font=self.fonts_map["ui"], anchor="w",
                                justify="left", padx=8, pady=5)
        self.map_tip.pack(fill="x")
        # Hit targets from the last draw: (x, y, radius, label, dist, dz).
        # Rebuilt every frame, which is also what keeps it honest — a stale
        # entry would describe something that has already moved.
        self._map_hits = []
        # Where the pointer is over the canvas, or None. The hit test runs on
        # every frame off this rather than only on mouse movement, so the ring
        # and the readout follow an entity as it moves instead of describing
        # where it used to be while the cursor sits still.
        self._map_cursor = None
        self.map_canvas.bind("<Motion>", self._on_map_hover)
        self.map_canvas.bind("<Leave>",
                             lambda _e: self._clear_map_tip(drop_cursor=True))
        # With the header gone, the hover box is the handle. It's the only other
        # part of the panel that isn't the map itself, it's always present
        # rather than appearing on hover, and its idle text already says the map
        # takes the mouse — so it reads as the part you grab.
        self._bind_drag(self.mapwin, (self.map_tipbox, self.map_tip),
                        unlocked=self._mouse_available)
        # Dragging the map body would fight with the click-to-inspect idea if
        # that ever lands, so the canvas stays out of it — same as the meter.
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
        return half + MINIMAP_MIRROR_X * dx * scale, half - dy * scale

    def _draw_minimap(self):
        if not self._shown.get("minimap"):
            return
        c = self.map_canvas
        me, ents, _stamp = self.world.read()
        c.delete("all")
        size = int(MINIMAP_SIZE * self._scales["minimap"])
        if int(c["width"]) != size:
            c.config(width=size, height=size)
        half = size / 2.0
        scale = half / float(self._map_range)

        c.configure(bg=self._theme.get("map_body", BG_BODY))
        if not self.world.fresh():
            # Say so rather than showing an empty box: a blank map and a map of
            # an empty area look identical, and only one of them is a problem.
            c.create_text(half, half, text="waiting for the game",
                          fill=self._map_ink(0.45),
                          font=self.fonts_map["ui_tiny_i"])
            self._map_hits = []
            return

        theme = self._theme
        body = theme.get("map_body", BG_BODY)
        c.configure(bg=body)
        mez = me.get("z", 0)
        # Rotating mode turns the world under a fixed arrow; fixed mode leaves
        # the world alone and turns the arrow instead.
        # Camera first: the map should turn with what you're looking at, not
        # with where the character happens to be pointing — they diverge
        # constantly, since the character turns to face its target. `c` is None
        # until the camera hook has seen a frame, and the character's own facing
        # is the fallback rather than snapping the map to zero.
        # Sticky. A missing camera reading means the hook is between cameras —
        # a zone change retires the old object and the next frame re-latches —
        # and it lasts a tick or two. Falling back to the character's facing
        # there swung the map to an unrelated heading and back, which read as
        # the orientation randomly breaking after a rift (the same event that
        # recolours the panel). Holding the last known heading is both steadier
        # and closer to true, since the camera hasn't actually moved.
        cam = me.get("c")
        if cam is not None:
            self._last_cam = float(cam)
        heading = float(self._last_cam if self._last_cam is not None
                        else (me.get("r", 0.0) or 0.0))
        rot = ((math.cos(heading), math.sin(heading))
               if self._map_mode == "Rotating" else None)
        # The marker shows where the CAMERA is pointing, nothing else. In
        # rotating mode that is the top of the map by construction, so it's a
        # constant rather than a rotation that has to agree with one.
        me_dir = ((0.0, -1.0) if rot is not None
                  else self._facing_screen(heading, heading, False))

        # The minimap always shows everyone nearby, regardless of the meter's
        # party/all mode — a map that hid the player standing next to you would
        # be misleading. Group members are marked with a ring instead.
        local, roster = self.world.who()

        self._draw_view_line(c, half, me_dir[0], me_dir[1], body)

        hits = []
        by_cat = {}
        for e in ents:
            by_cat.setdefault(e.get("c"), []).append(e)
        for cat in MINIMAP_ORDER:
            # Whole categories can be ticked off in the control menu. Skipped
            # here rather than filtered out of the snapshot, because the compass
            # reads the same snapshot and these ticks are the MAP's.
            if not self._map_filters.get(MINIMAP_FILTER_OF.get(cat), True):
                continue
            style = MINIMAP_STYLE_MAP[cat]
            for e in by_cat.get(cat, ()):
                x, y = self._minimap_px(e.get("x", 0), e.get("y", 0), me,
                                        half, scale, rot)
                if not (0 <= x <= size and 0 <= y <= size):
                    continue        # outside the square; the hook's cull is round
                r = style["r"] * MINIMAP_ICON_SCALE * self._scales["minimap"]
                # The local player is drawn last, as an arrow, not a dot.
                if cat == "hero" and e.get("n") and e["n"] == local:
                    continue
                # Off-level. Everything gets the up/down caret for it, but only
                # players and enemies are dimmed: those matter because they can
                # reach you, so "not on your floor" changes what they mean. A
                # chest or obelisk is a place to go either way, and fading it
                # just makes the thing you're navigating to harder to see.
                far = abs(e.get("z", mez) - mez) > MINIMAP_Z_FADE
                fade = far and cat in ("hero", "foe")
                fill = self._marker_fill(style["fill"])
                if fade:
                    fill = _lerp_hex(body, fill, MINIMAP_Z_DIM)
                # Other players point where they're facing. In rotating mode
                # that has to be taken relative to the camera, or everyone would
                # keep their world heading while the map turned under them.
                facing = None
                if cat == "hero" and e.get("r") is not None:
                    facing = self._facing_screen(float(e["r"]), heading,
                                                 rot is not None)
                # Outlined against the panel, not black: on the rift theme a
                # hard black edge on a dark body reads as a hole.
                edge = _lerp_hex(body, BG_BORDER, 0.35 if fade else 0.8)
                self._map_glyph(c, x, y, r, style, fill, facing, edge,
                                ring=self._marker_fill(style.get("ring")))
                if far:
                    self._map_z_marker(c, x, y, r, e.get("z", mez) - mez,
                                       _contrast_ink(body))
                hits.append((x, y, r, cat, self._marker_label(cat, e, roster),
                             math.hypot(e.get("x", 0) - me.get("x", 0),
                                        e.get("y", 0) - me.get("y", 0)),
                             e.get("z", mez) - mez))
                if cat == "hero" and e.get("n") in roster:
                    # Party members get a ring rather than a different colour:
                    # colour already means category, and overloading it would
                    # make a grouped player read as a different kind of thing.
                    rr = r + 2.5 * self._scales["minimap"]
                    party = self._marker_fill(MINIMAP_PARTY_RING)
                    ring = (_lerp_hex(body, party, MINIMAP_Z_DIM)
                            if fade else party)
                    c.create_oval(x - rr, y - rr, x + rr, y + rr,
                                  outline=ring, width=2)

        self._draw_me_arrow(c, half, me_dir[0], me_dir[1])
        self._map_hits = hits
        # Last, so the ring sits over everything including the player marker.
        hit = self._update_map_tip()
        if hit is not None:
            hx, hy, hr = hit[0], hit[1], hit[2]
            rr = hr + 4.0 * self._scales["minimap"]
            c.create_oval(hx - rr, hy - rr, hx + rr, hy + rr,
                          outline=self._map_ink(0.95),
                          width=max(1, int(round(self._scales["minimap"] * 2))))

    def _facing_screen(self, world_angle, heading, rotating):
        """A world heading as a screen-space unit vector.

        Facing is (cos a, sin a) in world terms. Fixed mode only has to flip y,
        since screen y grows downward. Rotating mode also has to take the angle
        relative to the camera, because the map itself has already turned by
        that much — otherwise everything would keep its world heading while the
        ground moved under it."""
        # Mirrored on the same axis as the positions, or a marker would point
        # somewhere the map disagrees with.
        if rotating:
            d = world_angle - heading
            return (MINIMAP_MIRROR_X * -math.sin(d), -math.cos(d))
        return (MINIMAP_MIRROR_X * math.cos(world_angle),
                -math.sin(world_angle))

    def _draw_view_line(self, c, half, dx, dy, body):
        """A line out of the player marker to the edge of the panel, showing
        where you're looking. Drawn before the entities so it never hides one."""
        accent = self._theme.get("accent", ACCENT)
        # The centre line runs all the way out to the edge of the panel. The
        # canvas is square, so the ray leaves through whichever side it reaches
        # first — the smaller of the two axis crossings.
        far = half
        if abs(dx) > 1e-9:
            far = min(far, half / abs(dx))
        if abs(dy) > 1e-9:
            far = min(far, half / abs(dy))
        c.create_line(half, half, half + dx * far, half + dy * far,
                      fill=_lerp_hex(body, accent, MINIMAP_VIEW_LINE),
                      width=max(1, int(self._scales["minimap"])))

    def _on_map_hover(self, event):
        """Only reachable while the game's escape menu is open, because that's
        the only time the overlay isn't click-through — which is also the only
        time you have a cursor to hover with, so the two line up."""
        self._map_cursor = (event.x, event.y)
        self._update_map_tip()

    def _map_hover_hit(self):
        """The marker under the cursor, or None. Nearest wins, so a crowd
        resolves to the one you're actually pointing at."""
        if self._map_cursor is None:
            return None
        cx, cy = self._map_cursor
        best, best_d2 = None, None
        slack = MINIMAP_TIP_RADIUS * self._scales["minimap"]
        for hit in self._map_hits:
            hx, hy, r = hit[0], hit[1], hit[2]
            reach = max(r, slack)
            d2 = (cx - hx) ** 2 + (cy - hy) ** 2
            if d2 <= reach * reach and (best_d2 is None or d2 < best_d2):
                best, best_d2 = hit, d2
        return best

    def _update_map_tip(self):
        """Name whatever is under the cursor and say how far away it is.
        Returns the hit so the draw pass can ring it."""
        hit = self._map_hover_hit()
        if hit is None:
            self._clear_map_tip()
            return None
        _hx, _hy, _r, cat, label, dist, dz = hit
        # Ground distance and height are reported separately on purpose: a
        # chest 8 units away and 40 below you is not 8 units away in any sense
        # that helps, and one combined number would hide exactly that.
        updown = "level" if abs(dz) < 1 else (f"{abs(dz):.0f} up" if dz > 0
                                              else f"{abs(dz):.0f} down")
        # Name on its own line, position under it: a long name would otherwise
        # set the width of the whole panel and shove the map sideways on hover.
        self.map_tip.config(text=f"{label}\n{dist:.0f}u away   ·   {updown}",
                            fg=self._map_ink(0.95))
        return hit

    @staticmethod
    def _elide(text, limit=MINIMAP_TIP_MAXLEN):
        text = (text or "").strip()
        return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"

    def _marker_label(self, cat, e, roster):
        """What the hover line calls this marker.

        Names alone aren't enough to read: player names are arbitrary and a
        foe's is just a creature, so each carries what KIND of thing it is in
        brackets. Party membership rides on the same line rather than being
        left to the ring colour, since that's the thing you're hovering to
        find out."""
        name = e.get("n")
        # Only the name is elided — the bracketed kind is the part that makes
        # the line readable, so it must survive.
        if cat == "hero":
            if not name:
                return "Player"
            kind = "Party" if name in roster else "Player"
            return f"{self._elide(name)} ({kind})"
        if cat == "foe":
            # The CDB display name once the agent has resolved it; until then
            # the internal id, prettified, so a foe is never just "Enemy".
            nm = (name or "").strip() or _pretty_id(e.get("k") or "")
            return f"{self._elide(nm)} (Enemy)" if nm else "Enemy"
        label = MINIMAP_LABELS.get(cat, cat.title())
        state = (e.get("s") or "").strip()
        if state and state not in MINIMAP_PLAIN_STATES:
            # e.g. "Chest · Locked" — the bit you'd want to know before walking
            # over to it.
            label = f"{label} · {state}"
        return label

    def _clear_map_tip(self, drop_cursor=False):
        if drop_cursor:
            self._map_cursor = None
        try:
            self.map_tip.config(text=MINIMAP_TIP_IDLE, fg=self._map_ink(0.5))
        except tk.TclError:
            pass

    def _map_ink(self, amount, bg=None):
        """Text and lines for the map panel, lifted off its background by
        `amount` — toward white on a dark panel, toward black on a light one.

        Derived rather than named because the panel is a different colour on
        every theme, and the meter's own FG_TEXT is a brown picked for
        parchment, which is how the hover line once ended up dark-on-dark. The
        direction has to be derived too, now that Farever's panel is light:
        lifting toward white on parchment is how you'd get it back.

        Everything on the panel that isn't a marker goes through here."""
        bg = bg or self._theme.get("map_body", BG_BODY)
        return _lerp_hex(bg, _contrast_ink(bg), amount)

    def _map_is_light(self):
        return _contrast_ink(self._theme.get("map_body", BG_BODY)) == \
            MINIMAP_Z_MARK_DARK

    def _marker_fill(self, fill):
        """A marker colour, adjusted for the panel it lands on.

        MINIMAP_STYLE is tuned for a dark panel — the markers are meant to be
        the bright thing on it. On parchment those same colours wash out
        (#FFD400 on #E8D5B8 is barely a marker at all), so they're darkened
        toward the same hue rather than being listed twice per category: one
        table of colours, and the light theme can't drift out of step with it."""
        if not fill or not self._map_is_light():
            return fill
        return _lerp_hex(fill, "#000000", MINIMAP_LIGHT_DARKEN)

    def _map_glyph(self, c, x, y, r, style, fill, facing=None, edge=None,
                   ring=None):
        """`fill` and `ring` arrive already adjusted for wherever this is being
        drawn — both the map and the compass tone them for their panel — so
        nothing in here consults the theme."""
        shape = style["shape"]
        if shape == "chevron" and facing is not None:
            # Same arrow as the player marker, smaller. The outline is what
            # keeps a stack of players readable: several chevrons on the same
            # spot merge into one unreadable blob without it.
            dx, dy = facing
            px, py = -dy, dx
            c.create_polygon(
                x + dx * 1.3 * r, y + dy * 1.3 * r,
                x - dx * r + px * 0.85 * r, y - dy * r + py * 0.85 * r,
                x - dx * 0.35 * r, y - dy * 0.35 * r,
                x - dx * r - px * 0.85 * r, y - dy * r - py * 0.85 * r,
                fill=fill, outline=edge or "", width=1)
            return
        if shape == "monolith":
            # A standing stone seen from above doesn't read as anything, so
            # this is the stone seen from the side: a tall block with a single
            # dark eye in its upper half. Distinct in silhouette from the
            # squares and dots around it, which is what a glance is sorting by.
            hw, hh = r * 0.62, r * 1.15
            c.create_rectangle(x - hw, y - hh, x + hw, y + hh,
                               fill=fill, outline=edge or "", width=1)
            dr = max(1.0, r * 0.30)
            dy = y - hh * 0.44
            c.create_oval(x - dr, dy - dr, x + dr, dy + dr,
                          fill="#000000", outline="")
            return
        if shape == "shard":
            # The soulstone in the world is a cluster of angular crystals
            # throwing off a magenta glow. At nine pixels that whole formation
            # is one blob, so what's drawn is what survives the shrinking: a
            # four-pointed shard with concave sides, a dim halo of the same
            # colour standing in for the glow, and a lighter core for the lit
            # middle. Nothing else on the map has points, which is what makes
            # it findable at a glance.
            def _star(rx, ry, waist):
                # Taller than it is wide, and with a fat waist: a thin one came
                # out as a pink plus sign next to the solid dots, which is the
                # one thing a crystal shouldn't look like. The waist is what
                # gives it a body to see; the points are what make it a shard.
                pts = []
                for i in range(8):
                    a = math.pi / 2.0 * (i / 2.0)
                    k = 1.0 if i % 2 == 0 else waist
                    pts.extend((x + math.cos(a) * rx * k,
                                y + math.sin(a) * ry * k))
                return pts
            # Halo first, under everything: the fill darkened rather than a
            # colour of its own, so a faded marker on another floor fades its
            # glow with it instead of keeping a bright ring around a dim shard.
            c.create_polygon(*_star(r * 1.15, r * 1.62, 0.50),
                             fill=_lerp_hex(fill, "#000000", 0.42), outline="")
            c.create_polygon(*_star(r * 0.84, r * 1.22, 0.55),
                             fill=fill, outline=edge or "", width=1)
            c.create_polygon(*_star(r * 0.32, r * 0.48, 0.62),
                             fill=_lerp_hex(fill, "#FFFFFF", 0.62), outline="")
            return
        if shape in ("dot", "chevron"):
            # `ring` is a second colour around the dot, for markers the game
            # itself gives two — the orbs are a yellow core in a purple glow.
            if ring:
                rr = r + 1.6 * self._scales["minimap"]
                c.create_oval(x - rr, y - rr, x + rr, y + rr,
                              outline=ring, fill="",
                              width=max(1, int(round(self._scales["minimap"] * 2))))
            c.create_oval(x - r, y - r, x + r, y + r, fill=fill, outline="")
        elif shape == "square":
            c.create_rectangle(x - r, y - r, x + r, y + r, fill=fill, outline="")
        else:   # diamond
            c.create_polygon(x, y - r, x + r, y, x, y + r, x - r, y,
                             fill=fill, outline="")

    def _map_z_marker(self, c, x, y, r, dz, ink):
        """A caret above or below a faded marker saying which way it is.

        The fade alone only tells you something is on another floor; this says
        whether you need to go up or down to reach it, which is the part you
        act on."""
        s = r * 0.8
        gap = r + 2.0 * self._scales["minimap"]
        if dz > 0:
            pts = (x - s, y - gap, x + s, y - gap, x, y - gap - s)
        else:
            pts = (x - s, y + gap, x + s, y + gap, x, y + gap + s)
        c.create_polygon(*pts, fill=ink, outline="")

    def _draw_me_arrow(self, c, half, dx, dy):
        """You, at the centre. `dx, dy` is the way you're pointing in SCREEN
        space — already resolved by the caller, because the two modes disagree
        about it: fixed mode turns the arrow, rotating mode turned the world
        instead and leaves the arrow pointing at the top of the map."""
        # Grows with the markers: an arrow left at its old size next to markers
        # 20% larger reads as the map having shrunk around you.
        s = 6.0 * MINIMAP_ICON_SCALE * self._scales["minimap"]
        px, py = -dy, dx        # perpendicular, for the two back corners
        tip = (half + dx * 1.4 * s, half + dy * 1.4 * s)
        tail = (half - dx * 0.4 * s, half - dy * 0.4 * s)
        left = (half - dx * s + px * 0.9 * s, half - dy * s + py * 0.9 * s)
        right = (half - dx * s - px * 0.9 * s, half - dy * s - py * 0.9 * s)
        c.create_polygon(*tip, *left, *tail, *right,
                         fill=self._map_ink(MINIMAP_ME_LIFT),
                         outline=BG_BORDER, width=1)

    def _build_compass(self):
        """A bearing strip on a pill-shaped panel, in the map's colours.

        The canvas itself is transparent and _draw_compass paints the pill onto
        it — two half-circle caps and the rectangle between them. It has to be
        drawn rather than configured: Tk has no rounded rectangle, and DWM's
        corner rounding gives a fixed small radius and a drop shadow with it.

        It spent a version with no panel at all — markers and numbers straight
        on the game — which reads beautifully over dark scenery and poorly over
        anything bright, and left nothing to grab hold of but the markers.

        With something behind it again, everything on it is coloured off that
        something: `_map_ink` for the text and lines, `_marker_fill` for the
        glyphs, exactly as on the minimap. Those two are what keep the strip
        legible on the parchment theme, where a panel-blind colour scheme puts
        cream text on a cream background."""
        self.compass_canvas = tk.Canvas(
            self.compasswin, bg=TRANSPARENT_KEY,
            highlightthickness=0, bd=0, width=COMPASS_W, height=COMPASS_H)
        self.compass_canvas.pack()
        self._bind_drag(self.compasswin, (self.compass_canvas,),
                        unlocked=self._mouse_available)

    def _compass_x(self, dx, dy, rot, half_w, centre=None):
        """Screen x for a world offset, or None if it's outside the arc.

        Routed through the same rotation the map uses, mirror included, so a
        marker can't sit left on the compass and right on the minimap.

        `half_w` is how far the arc reaches from `centre`, which is not the same
        as half the canvas: the strip is a pill, and the bearings are mapped
        across its straight section so that nothing lands on a rounded end where
        there'd be no panel under it."""
        ca, sa = rot
        mx = MINIMAP_MIRROR_X * (dx * sa - dy * ca)   # right of view, on screen
        fy = dx * ca + dy * sa                        # ahead of view
        rel = math.atan2(mx, fy)                      # 0 = straight ahead
        span = math.radians(COMPASS_FOV) / 2.0
        if abs(rel) > span:
            return None
        return (half_w if centre is None else centre) + (rel / span) * half_w

    def _draw_compass(self):
        if not self._shown.get("compass"):
            return
        c = self.compass_canvas
        me, ents, _stamp = self.world.read()
        c.delete("all")
        scale = self._scales["compass"]
        w, h = int(COMPASS_W * scale), int(COMPASS_H * scale)
        if int(c["width"]) != w or int(c["height"]) != h:
            c.config(width=w, height=h)
        theme = self._theme
        body = theme.get("map_body", BG_BODY)
        c.configure(bg=TRANSPARENT_KEY)
        # The panel is a pill: two half-circle caps and the rectangle between
        # them, painted onto a transparent canvas rather than being the canvas's
        # own background. Tk has no rounded rectangle and DWM's corner rounding
        # is both a fixed small radius and a request for the drop shadow, so
        # this is the only way to get a fully round end.
        centre = w / 2.0
        ph = h * COMPASS_PILL_H          # the pill's own height; see the note
        # Bearings map onto the STRAIGHT section only. Run them to the canvas
        # edge instead and a marker at the far left sits on the tip of a cap,
        # where the panel under it is a couple of pixels tall.
        half_w = max(1.0, (w - ph) / 2.0)
        c.create_oval(0, 0, ph, ph, fill=body, outline="")
        c.create_oval(w - ph, 0, w, ph, fill=body, outline="")
        c.create_rectangle(ph / 2.0, 0, w - ph / 2.0, ph, fill=body, outline="")

        if not self.world.fresh():
            return
        heading = float(self._last_cam if self._last_cam is not None
                        else (me.get("r", 0.0) or 0.0))
        rot = (math.cos(heading), math.sin(heading))

        # Cardinals first, so markers sit over them. They live in the top band,
        # clear of the distances along the bottom, and are quieter than the
        # numbers — a bearing letter is orientation, not information.
        ink = self._map_ink(0.55)
        for deg, letter in COMPASS_CARDINALS:
            a = math.radians(deg)
            x = self._compass_x(math.cos(a), math.sin(a), rot, half_w,
                                centre)
            if x is None:
                continue
            c.create_line(x, h * COMPASS_TICK_TOP, x, h * COMPASS_TICK_BOT,
                          fill=ink)
            # Bold and upright, unlike the distances under the markers: a
            # cardinal is a landmark you find at a glance rather than something
            # you read, and the italic 7pt it shared with the numbers was doing
            # neither job well over a moving background.
            c.create_text(x, h * COMPASS_CARD_Y, text=letter, fill=ink,
                          font=self.fonts_compass["ui_sm_b"])
        # Dead ahead. Brighter than the cardinals, because it's the one mark
        # you read the whole strip against.
        c.create_line(centre, 0, centre, h * COMPASS_TICK_BOT,
                      fill=self._map_ink(0.85))

        mez = me.get("z", 0)
        local, roster = self.world.who()
        rows = []
        for e in ents:
            cat = e.get("c")
            if cat not in COMPASS_CATS:
                continue
            # Ticked off in the control menu. The compass keeps its own pair of
            # these; hiding chests here doesn't hide them on the map.
            if not self._compass_filters.get(COMPASS_FILTER_OF.get(cat), True):
                continue
            # Party only — a compass full of strangers tells you nothing about
            # where your group went. And never yourself: you are dead ahead by
            # construction, so the marker sat permanently over the centre tick
            # saying nothing.
            if cat == "hero" and (e.get("n") not in roster
                                  or (local and e.get("n") == local)):
                continue
            dx = e.get("x", 0) - me.get("x", 0)
            dy = e.get("y", 0) - me.get("y", 0)
            dist = math.hypot(dx, dy)
            # Only categories in COMPASS_LIMITS have a range at all; everything
            # else is carried however far away it is.
            limit = COMPASS_LIMITS.get(cat)
            if limit is not None and dist > limit:
                continue
            x = self._compass_x(dx, dy, rot, half_w, centre)
            if x is None:
                continue        # behind you
            rows.append((dist, cat, x, e))
        # Farthest first, so the nearer marker wins an overlap.
        rows.sort(key=lambda t: -t[0])
        my = h * COMPASS_MARK_Y
        for dist, cat, x, e in rows:
            style = MINIMAP_STYLE_MAP[cat]
            r = style["r"] * scale
            facing = None
            if cat == "hero":
                # Always up, never their heading. On the MAP a chevron pointing
                # where someone is running tells you where the group is going;
                # on a bearing strip there's no ground for it to point across,
                # so it just spun in place and read as noise. The strip answers
                # "which way is that", and an arrow is the shape that says it.
                facing = (0.0, -1.0)
            # Toned for the panel, like the map's — the strip has one again, and
            # the yellow orb on parchment is exactly as unreadable here.
            edge = _lerp_hex(body, BG_BORDER, 0.8)
            self._map_glyph(c, x, my, r, style, self._marker_fill(style["fill"]),
                            facing, edge,
                            ring=self._marker_fill(style.get("ring")))
        self._draw_compass_dists(c, rows, h, scale, mez)

    def _draw_compass_dists(self, c, rows, h, scale, mez):
        """How far away each marker is, on the line under it.

        A second pass rather than part of the glyph loop, because the two want
        opposite orders. Glyphs draw farthest-first so the nearer one lands on
        top; labels have to be placed NEAREST-first, since when two of them
        collide the one worth keeping is the near one — and unlike overlapping
        glyphs, overlapping text isn't a marker half-hidden, it's four digits
        of neither number.

        Elevation rides on this line as an arrow rather than as the map's
        caret. The caret hangs below the glyph, which is where the number now
        is, and a strip this short has no row of pixels to spare for both."""
        # Brighter than the cardinals: the numbers are the part you actually
        # read. They also hang off the pill's lower edge onto the game, so they
        # get an outline — the only text on the overlay that sits on scenery
        # rather than on a panel, and the scenery is any colour it likes.
        y = h * COMPASS_DIST_Y
        font = self.fonts_compass["ui_tiny_i"]
        line_h = font.metrics("linespace")
        pad_x, pad_y = COMPASS_DIST_PAD_X * scale, COMPASS_DIST_PAD_Y * scale
        placed = []
        for dist, _cat, x, e in reversed(rows):
            if any(abs(x - px) < COMPASS_DIST_GAP * scale for px in placed):
                continue
            placed.append(x)
            dz = e.get("z", mez) - mez
            arrow = ""
            if abs(dz) > MINIMAP_Z_FADE:
                arrow = " ↑" if dz > 0 else " ↓"
            label = _short_dist(dist) + arrow
            # Sized from the text rather than a fixed width: "8" and "2.7k ↑"
            # are very different strings, and a badge wide enough for the
            # second is a slab under the first.
            half_tw = font.measure(label) / 2.0
            c.create_rectangle(x - half_tw - pad_x, y - line_h / 2.0 - pad_y,
                               x + half_tw + pad_x, y + line_h / 2.0 + pad_y,
                               fill=COMPASS_DIST_BOX, outline="",
                               stipple=COMPASS_DIST_STIPPLE)
            c.create_text(x, y, text=label, fill=COMPASS_DIST_BOX_INK,
                          font=font)

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

    def _bind_drag(self, win, widgets, unlocked=None):
        """Drag any of `widgets` to move `win` (while unlocked).

        `unlocked` overrides what counts as unlocked, for windows that don't
        follow the overlay-wide rule — the minimap answers to the cursor."""
        state = {}
        free = unlocked or (lambda: not self._is_locked())

        def start(e):
            if not free():
                return
            state["dx"] = e.x_root - win.winfo_x()
            state["dy"] = e.y_root - win.winfo_y()
            state["on"] = True

        def move(e):
            if not free() or not state.get("on"):
                return
            win.geometry(f"+{e.x_root - state['dx']}+{e.y_root - state['dy']}")

        def end(e):
            if state.pop("on", None):
                self._save_pos()
                self._refocus_game()

        for w in widgets:
            w.bind("<Button-1>", start)
            w.bind("<B1-Motion>", move)
            w.bind("<ButtonRelease-1>", end)

    def _foreground_pid(self):
        if sys.platform != "win32":
            return 0
        try:
            u = ctypes.windll.user32
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(u.GetForegroundWindow(),
                                       ctypes.byref(pid))
            return pid.value
        except Exception:
            return 0

    def _game_has_focus(self):
        """Is Farever the window you're actually looking at?

        Our own windows count as the game having focus. They're WS_EX_NOACTIVATE
        so they shouldn't take it, but Tk's dropdown menus are its own windows
        and do — without this, opening the Theme dropdown would hide the very
        menu you opened it from."""
        if sys.platform != "win32" or not self.target_pid:
            return True         # can't tell => don't start hiding things
        fg = self._foreground_pid()
        return fg in (self.target_pid, os.getpid()) or fg == 0

    def _cursor_is_free(self):
        """Has the game let go of the mouse?

        Farever frees the cursor on Alt, and the overlay should be usable when
        it does. Detected from the OS cursor being visible rather than from the
        key: measured, GetAsyncKeyState never sees that Alt — the game takes it
        — and it behaves as a toggle rather than a hold, so watching the key
        would have been wrong twice over. Reading the cursor also covers every
        other way the game hands the mouse back, including its own menus.

        Gated on the game being frontmost, or alt-tabbing away would leave the
        minimap eating clicks meant for whatever you switched to."""
        if sys.platform != "win32" or not self.target_pid:
            return False
        try:
            u = ctypes.windll.user32
            ci = CURSORINFO()
            ci.cbSize = ctypes.sizeof(CURSORINFO)
            if not u.GetCursorInfo(ctypes.byref(ci)):
                return False
            if not (ci.flags & CURSOR_SHOWING):
                return False
            return self._foreground_pid() == self.target_pid
        except Exception:
            return False

    def _mouse_available(self):
        """There's a pointer to use: the escape menu is open, or the game has
        released the mouse (Alt).

        Deliberately narrower than the overlay-wide unlock — freeing the cursor
        lets you point at things, it doesn't summon the control menu over the
        game. Only the windows you'd want to click answer to it: the meter, for
        picking a player, and the minimap."""
        return self._menu_unlock or self._cursor_free

    def _refocus_game(self):
        """Hand keyboard focus back to Farever.

        The overlay windows carry WS_EX_NOACTIVATE so clicking them shouldn't
        steal focus — but Tk's dropdown menus are its own windows and don't,
        and once one of those has been opened the game stops seeing keystrokes.
        The symptom is Esc not closing the game's menu until you click the game
        first. Rather than leaving that to the player, every interaction with
        the control menu ends by giving the game the foreground back."""
        if sys.platform != "win32" or not self.target_pid:
            return
        hwnd = self._game_hwnd
        if not hwnd or not ctypes.windll.user32.IsWindow(hwnd):
            hwnd = self._game_hwnd = _main_hwnd_of_pid(self.target_pid)
        if not hwnd:
            return
        try:
            u = ctypes.windll.user32
            if u.GetForegroundWindow() != hwnd:
                u.SetForegroundWindow(hwnd)
        except Exception:
            pass

    def _on_row_click(self, name):
        # Same rule as the window's click-through, or the row would be
        # clickable-looking and inert while the mouse is free.
        if self._mouse_available() and name:
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
        # Not the compass either, for the same reason and one more: it is
        # transparent all the way to its edges, and rounding a window is also
        # what asks DWM for the drop shadow that goes with it.
        if win in (self.riftwin, self.compasswin):
            return
        self._round_win_corners(win)

    def _apply_clickthrough(self):
        locked = self._is_locked()
        # The minimap belongs in here: left out, it stays a solid, clickable,
        # always-on-top window over the game, so Windows draws a cursor over it
        # even while the game has the pointer captured, and a click that lands
        # on it goes to Tk instead of the game.
        for win in (self.detail, self.riftwin):
            self._set_win_clickthrough(win, locked)
        # These two answer to the cursor rather than to the escape menu, so they
        # can be pointed at whenever the game has released the mouse: the meter
        # to click a player's row, the minimap to hover a marker.
        pointable = not self._mouse_available()
        self._set_win_clickthrough(self.root, pointable)
        self._set_win_clickthrough(self.mapwin, pointable)
        # The compass joins them now that it has no background. Its transparent
        # pixels already pass clicks through on their own, but the markers and
        # numbers are real pixels sitting over the middle of the screen, and a
        # click landing on one of those was a click the game never saw.
        self._set_win_clickthrough(self.compasswin, pointable)
        # The hover box comes and goes with the same signal: it is only ever
        # useful when there's a cursor, and this is the one place both halves of
        # that answer (the escape menu and the freed mouse) are already known.
        self._sync_map_tip()
        # The control menu is always interactive (it is only ever shown while
        # the cursor is free); the floating hint and parse banner are text over
        # the game and must never take a click.
        self._set_win_clickthrough(self.menu, False)
        self._set_win_clickthrough(self.hintwin, True)
        self._set_win_clickthrough(self.parsewin, True)
        # The prompt must take clicks whenever it's up, regardless of lock
        # state — it's the one overlay window that has to be answered.
        self._set_win_clickthrough(self.promptwin, False)

    def _sync_map_tip(self):
        """Show the hover box only while the mouse is free.

        It can't tell you anything without a pointer, and the map spends most of
        its life being glanced at rather than pointed at — so for most of that
        life it was two lines of grey text under the map saying how to get a
        cursor. The panel shrinks to just the map when it goes, which is the
        decluttering; the map itself doesn't move, since the window is anchored
        by its top-left corner.

        It's also the drag handle, and that costs nothing: dragging was already
        gated on the same condition, so the handle is present exactly when it
        would have worked anyway."""
        want = self._mouse_available()
        if want == self._map_tip_shown:
            return
        self._map_tip_shown = want
        try:
            if want:
                self.map_tipbox.pack(fill="x", pady=(3, 0))
            else:
                self.map_tipbox.pack_forget()
        except tk.TclError:
            pass

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
        # Only while the game's menu is open — that's the only time the overlay
        # is interactive, and so the only time it can have taken focus.
        if q and self._menu_unlock:
            self._refocus_game()

    def _on_element_pick(self, key, value):
        self._enqueue(lambda: self._set_element_mode(key, value))()

    def _set_element_mode(self, key, value):
        """Show / Hide / Show in ESC for one overlay window. The control menu
        is never in TOGGLEABLE_ELEMENTS — it's how you get the others back."""
        if value not in ELEMENT_MODES:
            return
        self._show[key] = value
        self._save_settings()
        self._refresh_visibility()

    def _toggle_heal(self):
        self._show_heal = not self._show_heal
        self._save_settings()

    def _on_scale_pick(self, group):
        self._enqueue(lambda: self._set_group_scale(
            group, self._scale_vars[group].get() / 100))()

    def _set_group_scale(self, group, factor):
        """Resize one window group's fonts, which resizes the windows that pack
        to them. The two canvas-drawn banners measure their text at draw time,
        so they're re-drawn rather than left at the old size."""
        if abs(factor - self._scales.get(group, 1.0)) < 0.001:
            return
        self._scales[group] = factor
        for key, (_family, size, *_style) in FONT_SPECS.items():
            self._font_sets[group][key].configure(
                size=max(6, round(size * factor)))
        if group == "meter":
            # Kept in step because a pile of pixel constants (minimum widths,
            # the warning wrap) are still expressed against it.
            self._ui_scale = factor
        self._parse_text = None          # force the parse banner to re-measure
        self._save_settings()
        self._draw_hint()
        # Pixel floors and wrap widths don't come along for free — and each
        # belongs to ITS OWN group's scale, not to whichever slider happened to
        # move. Applying `factor` to all four made every window jump whenever
        # any one of them was resized. Recomputed from scratch rather than
        # patched for the group that changed, so they can't drift apart.
        for win, key, group_of in ((self.root, "meter", "meter"),
                                   (self.detail, "detail", "detail"),
                                   (self.menu, "menu", "menu"),
                                   # The rift prompt is drawn with the meter's
                                   # fonts, so it scales with the meter.
                                   (self.promptwin, "prompt", "meter")):
            win.minsize(int(MIN_W[key] * self._scales[group_of]), 0)
        self.warn_lbl.config(
            wraplength=int(WARN_WRAP * self._scales["menu"]))
        self.root.update_idletasks()
        print(f"[meter] {group} scale {factor:.2f}x", file=sys.stderr)

    def _on_theme_pick(self, value):
        # Queued like every other menu action: it mutates state the refresh
        # loop reads, and Tk isn't thread-safe.
        self._enqueue(lambda: self._set_theme_mode(value))()

    def _on_map_mode_pick(self, value):
        # Queued for the same reason as the theme pick: the draw pass reads it.
        self._enqueue(lambda: self._set_map_mode(value))()

    def _set_map_mode(self, value):
        self._map_mode = value
        self._save_settings()

    def _on_map_rate_pick(self, value):
        self._enqueue(lambda: self._set_map_rate(value))()

    def _set_map_rate(self, value):
        """Change how often the hook sweeps the world.

        The redraw timer picks the new rate up on its next tick by reading
        _map_rate, so only the agent side needs telling."""
        if value not in MINIMAP_RATE_MS:
            return
        self._map_rate = value
        self._save_settings()
        ms = MINIMAP_RATE_MS[value]
        print(f"[meter] minimap refresh {value} ({ms}ms)", file=sys.stderr)
        try:
            self._configure(worldTick=ms)
        except Exception as e:
            print(f"[meter] couldn't push the refresh rate: {e}",
                  file=sys.stderr)

    def _toggle_compass_filter(self, key):
        """One compass category group on or off. Same shape as the map's, and
        deliberately a separate setting — see COMPASS_FILTERS."""
        self._compass_filters[key] = not self._compass_filters.get(key, True)
        self._save_settings()

    def _toggle_map_filter(self, key):
        """One category group on or off. Nothing to redraw by hand — the map is
        repainted wholesale on the next tick and reads the ticks as it goes."""
        self._map_filters[key] = not self._map_filters.get(key, True)
        self._save_settings()

    def _toggle_hide_ooc(self):
        self._hide_ooc = not self._hide_ooc
        self._save_settings()
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
        means the control menu is never the only thing on screen.

        What the escape menu does NOT override is another game window on top of
        it. Options, feedback and the two confirmations are all reached through
        it, and while one of those is up the game has taken the screen back."""
        ooc_hidden = (self._hide_ooc and not self._menu_unlock and
                      (time.time() - self._combat_seen_at) >= HIDE_OOC_LINGER_SECS)
        # Any game window that isn't the escape menu (inventory, map, ...) owns
        # the screen while it's up — see MENU_IGNORE_WINDOWS. Unlike the OOC
        # rule this is unconditional: it isn't a setting the player can untick.
        #
        # It applies even while the escape menu is open, which is the whole
        # point: options, the feedback form and the "back to menu"/"exit game"
        # confirmations are all opened FROM the escape menu and sit on top of
        # it. Treating the escape menu as a blanket exemption left the overlay
        # sitting over every one of them.
        menu_hidden = self.ui_state.any_open_except(MENU_IGNORE_WINDOWS)
        # The rift prompt is modal: while it's up nothing else is on screen, not
        # even the control menu. That's deliberate — it leaves Esc free to hand
        # the cursor back so the question can actually be clicked.
        # Alt-tab away and the whole overlay goes with you. It's drawn on top
        # of everything, so leaving it up means a damage meter floating over
        # your browser — and worse, one you can't click past while the cursor
        # is free. The tray icon stays, which is how you'd stop the meter from
        # out here anyway.
        blanket = menu_hidden or self._prompt_open or not self._focused
        changed = False
        for key in self._element_win:
            hidden = blanket or (ooc_hidden and key not in OOC_EXEMPT)
            mode = self._show.get(key, ELEMENT_SHOW)
            if mode == ELEMENT_HIDE:
                base = False           # hidden, and stays hidden in the menu
            elif mode == ELEMENT_ESC:
                base = self._menu_unlock
            else:
                base = True
            want = base and not hidden
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
        # ...and the control menu goes with everything else when you alt-tab.
        # The game's escape menu stays open behind you, so _menu_unlock stays
        # true, and without this the one window that ignores every other hiding
        # rule sat over your browser — the exact thing the focus check exists to
        # prevent, on the largest window the overlay has.
        # ...and it goes when the game opens something over the escape menu,
        # for the same reason as everything else: "hide all UI" has to include
        # the meter's own settings panel, or the one window left on screen is
        # the one you were trying to get out of the way.
        menu_visible = (self._menu_unlock and not self._prompt_open
                        and self._focused and not menu_hidden)
        # Whatever route the menu leaves by — Esc, alt-tab, the game opening
        # something over it — its dropdowns leave with it.
        if not menu_visible:
            self._unpost_menus()
        changed |= self._want_visible("menu", menu_visible)
        changed |= self._want_visible("hint", menu_visible)
        changed |= self._want_visible("prompt", self._prompt_open)
        if changed:
            self._start_fade()

    def _pick_theme(self):
        """The mode names two things: which base to wear, and whether a rift
        overrides it.

        Pinned Rift means rift, always. It used to fall back to Farever while
        the escape menu was open, on the reasoning that the control menu is
        Farever-styled and the meter sits next to it — but somebody who pins
        Rift has asked for rift colours, and watching the overlay change theme
        every time they open the menu is a worse trade than a colour clash with
        a panel that isn't themed at all.

        The Dynamic modes still yield to the escape menu, and that's different:
        there the rift colours are something the game put you in rather than
        something you chose, so seeing the overlay's own palette while you're
        reading its settings is the more useful of the two."""
        base = (THEME_DARK if self._theme_mode in ("Dark", "Dark Dynamic")
                else THEME_DEFAULT)
        if self._theme_mode == "Rift":
            return THEME_RIFT
        if self._menu_unlock:
            return base
        if (self._theme_mode.endswith("Dynamic")
                and self.ui_state.in_rift()):
            return THEME_RIFT
        return base

    def _set_theme_mode(self, mode):
        self._theme_mode = mode
        self._save_settings()

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
        mb = t.get("map_body", BG_BODY)
        # The hover well is always DARKER than the panel, on both kinds of
        # panel — a sunk box that got lighter than its surroundings would read
        # as raised. Its text is then lifted off the well rather than off the
        # panel, since that's what it actually sits on.
        sunk = _lerp_hex(mb, "#000000", 0.30 if not self._map_is_light() else 0.12)
        self.map_tipbox.config(bg=self._map_ink(0.16))
        self.map_tip.config(bg=sunk, fg=self._map_ink(0.75, bg=sunk))
        self.map_canvas.config(bg=mb)
        # Derived from the map colour rather than the meter's palette: the
        # brown edge goes muddy against navy. Darkening works on either kind of
        # panel, which is why the edge doesn't need the contrast treatment the
        # text does. With the header gone this and the hover box are all the
        # theme has left to show on the minimap.
        self.map_border.config(bg=_lerp_hex(mb, "#000000", 0.45))
        # The compass has no chrome to repaint — see _build_compass.
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

    # ---- rebinding the reset key ----
    def _begin_bind_capture(self):
        """Listen for the next keypress and make it the reset bind.

        POLLED, not bound. The obvious version — focus the button and take a
        Tk <KeyPress> — silently never fires: every overlay window is
        overrideredirect and carries WS_EX_NOACTIVATE precisely so that
        clicking it can't steal focus from the game, which also means it never
        receives a keystroke. The keyboard belongs to Farever the entire time
        this panel is on screen.

        GetAsyncKeyState doesn't care who has focus, needs no hook, and reads
        the same virtual-key codes the hook will later match against — so what
        you press here is exactly what will fire in play."""
        if self._binding_now:
            self._end_bind_capture()
            return
        self._binding_now = True
        self._bind_poll_job = None
        self.btn_bind.config(text="press a key…  (Esc cancels)")
        # The Tk binding stays as well: it costs nothing, and it's what makes
        # the capture testable without a keyboard.
        self.btn_bind.bind("<KeyPress>", self._on_bind_key)
        self._poll_bind_capture()

    def _poll_bind_capture(self):
        """Watch the keyboard until something bindable is held down."""
        if not self._binding_now or sys.platform != "win32":
            return
        u = ctypes.windll.user32

        def down(vk):
            return bool(u.GetAsyncKeyState(vk) & 0x8000)

        if down(0x1B):                      # Esc — back out, bind unchanged
            self._end_bind_capture()
            return
        shift, ctrl, alt = down(VK_SHIFT), down(VK_CONTROL), down(VK_MENU)
        # Middle and the side buttons are offered; left and right never are,
        # and 0x03 is Break rather than a button at all.
        for vk in list(VK_MOUSE) + list(range(0x08, 0xFF)):
            if vk in VK_UNBINDABLE or not down(vk):
                continue
            # A modifier is required for anything that would otherwise be a
            # plain keystroke — the hook swallows what it fires on, so a bare
            # letter costs you that key in game. F-keys and the bindable mouse
            # buttons are exempt: nothing in Farever wants them by default, and
            # binding Mouse 4 on its own is the normal thing to do.
            if (not (shift or ctrl or alt) and not (0x70 <= vk <= 0x87)
                    and vk not in VK_MOUSE):
                self.btn_bind.config(
                    text="needs Ctrl, Shift or Alt  (or an F-key)")
                break                       # keep listening; they'll try again
            self._set_reset_bind({"vk": vk, "shift": shift, "ctrl": ctrl,
                                  "alt": alt})
            self._end_bind_capture()
            return
        self._bind_poll_job = self.root.after(40, self._poll_bind_capture)

    def _end_bind_capture(self):
        self._binding_now = False
        if getattr(self, "_bind_poll_job", None):
            try:
                self.root.after_cancel(self._bind_poll_job)
            except tk.TclError:
                pass
            self._bind_poll_job = None
        try:
            self.btn_bind.unbind("<KeyPress>")
        except tk.TclError:
            pass
        self._refresh_menu()

    def _on_bind_key(self, event):
        """Tk's `keycode` IS the Windows virtual-key code, which is exactly
        what the hook compares against — so nothing has to be translated."""
        vk = int(event.keycode)
        if vk == 0x1B or vk in VK_UNBINDABLE:      # Esc, or a bare modifier
            if vk == 0x1B:
                self._end_bind_capture()
            return "break"                          # modifiers: keep listening
        # Tk's state bitmask: 0x1 Shift, 0x4 Control, 0x20000 Alt on Windows.
        shift, ctrl = bool(event.state & 0x1), bool(event.state & 0x4)
        alt = bool(event.state & 0x20000)
        # A bare letter would be swallowed while you play — the hook eats the
        # key it fires on, so binding "W" costs you walking forwards. Function
        # keys are exempt: nothing in the game is bound to them by default and
        # they're the obvious thing to want here.
        if (not (shift or ctrl or alt) and not (0x70 <= vk <= 0x87)
                and vk not in VK_MOUSE):
            self.btn_bind.config(text="needs Ctrl, Shift or Alt  (or an F-key)")
            return "break"
        self._set_reset_bind({"vk": vk, "shift": shift, "ctrl": ctrl,
                              "alt": alt})
        self._end_bind_capture()
        return "break"

    def _set_reset_bind(self, bind):
        RESET_BIND.update(bind)
        self._save_settings()
        self._draw_hint()          # the floating hint carries the same label
        self.btn_reset_data.config(
            text=f"Reset encounter data   ({bind_label()})")
        # Only the RegisterHotKey fallback needs telling; the low-level hook
        # reads RESET_BIND on every keypress and has already picked it up.
        if RESET_BIND.get("vk") in VK_MOUSE and REBIND_TO[0]:
            print("[meter] mouse buttons need the low-level hook, which isn't "
                  "installed — this binding won't fire.", file=sys.stderr)
        if REBIND_TO[0] and sys.platform == "win32":
            try:
                ctypes.windll.user32.PostThreadMessageW(
                    REBIND_TO[0], WM_REBIND, 0, 0)
            except Exception as e:
                print(f"[meter] couldn't re-register the hotkey: {e}",
                      file=sys.stderr)
        print(f"[meter] reset bind is now {bind_label()}", file=sys.stderr)

    def _unpost_menus(self):
        """Take down any dropdown that's currently posted.

        Tk posts an OptionMenu's list as a separate toplevel with a grab on the
        pointer. Withdrawing the panel underneath doesn't touch it, so closing
        the escape menu — or alt-tabbing — while a dropdown was open left the
        choices floating over the game with nothing behind them. `unpost` on a
        menu that isn't posted is harmless, so this needs no bookkeeping about
        which one was open."""
        for opt in getattr(self, "_option_menus", ()):
            try:
                opt["menu"].unpost()
            except tk.TclError:
                pass
        # The grab goes with it: Tk holds one while a menu is posted, and a
        # stray grab is how the game stops seeing the mouse at all.
        try:
            self.menu.grab_release()
        except tk.TclError:
            pass

    def _alpha_for(self, key):
        """What this window's opacity should settle at when it's on screen.

        The slider is a percentage taken OFF the overlay's normal opacity, so 0
        is the look the meter has always had rather than a subtly different
        one. The exempt windows ignore it entirely."""
        if key in TRANSPARENCY_EXEMPT or not self._transparency:
            return OVERLAY_ALPHA
        return OVERLAY_ALPHA * (1.0 - min(TRANSPARENCY_MAX,
                                          self._transparency) / 100.0)

    def _set_transparency(self, percent):
        """Apply the slider. Windows already on screen are set outright rather
        than faded there: a fade is for something arriving or leaving, and this
        is neither — you're dragging a slider and watching the result."""
        percent = max(0, min(TRANSPARENCY_MAX, int(percent)))
        if percent == self._transparency:
            return
        self._transparency = percent
        for key, win in self._fade_win.items():
            if not self._shown.get(key):
                continue
            self._alpha[key] = self._alpha_for(key)
            try:
                win.attributes("-alpha", self._alpha[key])
            except tk.TclError:
                pass
        self._save_settings()
        print(f"[meter] transparency {percent}%", file=sys.stderr)

    def _on_transparency_pick(self):
        # Queued like every other menu action: it touches window state the
        # refresh loop also reads, and Tk isn't thread-safe.
        self._enqueue(lambda: self._set_transparency(self._transp_var.get()))()

    def _step_fade(self):
        """Walk every faded window one step towards its target opacity, and
        unmap it once it reaches zero. Reversing mid-fade needs no special
        handling: the target flips and the next step walks back from here."""
        self._fade_job = None
        fading = False
        for key, win in self._fade_win.items():
            target = self._alpha_for(key) if self._shown[key] else 0.0
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
        self._save_settings()
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
        show = self._show_heal
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
        self.btn_heal.config(
            text=("☑  Healing columns" if self._show_heal
                  else "☐  Healing columns"))
        for key, label, _cats in MINIMAP_FILTERS:
            on = self._map_filters.get(key, True)
            self.btn_map_filter[key].config(
                text=("☑  " if on else "☐  ") + label)
        if not self._binding_now:
            self.btn_bind.config(text=bind_label())
        for key, label, _cats in COMPASS_FILTERS:
            on = self._compass_filters.get(key, True)
            self.btn_compass_filter[key].config(
                text=("☑  " if on else "☐  ") + label)
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
        # Cheap (two user32 calls) and only acted on when it changes, so the
        # click-through style isn't rewritten four times a second.
        free = self._cursor_is_free()
        if free != self._cursor_free:
            self._cursor_free = free
            self._apply_clickthrough()
            if not free:
                self._clear_map_tip(drop_cursor=True)
        focused = self._game_has_focus()
        if focused != self._focused:
            self._focused = focused
            self._refresh_visibility()
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
                         show_heal=self._show_heal,
                         cls_tag=_class_tag(self.world.class_of(p.name)))
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
            if self._show_heal:
                stats.append(f"{int(fp.heal_total)} heal")
            if fp.kills:
                stats.append(f"{fp.kills} kills")
            self.stats_lbl.config(text=" · ".join(stats))
            self.dmg_col.show(self._merge_named(fp.skills), fp.total)
            if self._show_heal:
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
                self._draw_compass()
            except tk.TclError:
                return              # window went away; stop rescheduling
            self.root.after(max(25, MINIMAP_RATE_MS[self._map_rate] // 2),
                            map_loop)
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
        # Two labels, not one line of text. The left one holds the rank, name
        # and class; the right one holds nothing but ASCII digits. They live in
        # a grid whose first column has a fixed pixel width, which is what
        # actually pins the numbers: a monospace font is only monospace for the
        # glyphs it has, and a name in a fallback face (CJK measures 1.71 cells
        # per glyph in Consolas) drags everything after it out of true. Padding
        # with spaces gets close and can't get exact — two rows will round
        # opposite ways — so the width is enforced by the layout instead.
        self.top = tk.Frame(self.f, bg=BG_BODY)
        self.top.pack(fill="x")
        self.line = tk.Label(self.top, text="", bg=BG_BODY, fg=FG_TEXT,
                             font=self.fonts["mono_10"], anchor="w")
        self.cls = tk.Label(self.top, text="", bg=BG_BODY, fg=FG_TEXT,
                            font=self.fonts["mono_10"], anchor="w")
        self.nums = tk.Label(self.top, text="", bg=BG_BODY, fg=FG_TEXT,
                             font=self.fonts["mono_10"], anchor="w")
        self.line.grid(row=0, column=0, sticky="w")
        self.cls.grid(row=0, column=1, sticky="w")
        self.nums.grid(row=0, column=2, sticky="w")
        self.dmg_track = tk.Frame(self.f, bg=BG_BAR_TRACK, height=5)
        self.dmg_track.pack(fill="x")
        self.dmg_bar = tk.Frame(self.dmg_track, bg=DMG_BAR, height=5)
        self.dmg_bar.place(relwidth=0.0, relheight=1.0)
        self.heal_track = tk.Frame(self.f, bg=BG_BAR_TRACK, height=5)
        self.heal_track.pack(fill="x", pady=(1, 2))
        self.heal_bar = tk.Frame(self.heal_track, bg=HEAL_BAR, height=5)
        self.heal_bar.place(relwidth=0.0, relheight=1.0)
        for w in (self.f, self.top, self.line, self.cls, self.nums):
            w.bind("<Button-1>", lambda e: on_click(self._name))
        self._packed = False
        self._heal_packed = True
        self._name = None
        self._is_me = False

    def set_theme(self, t):
        self.f.config(bg=t["body"])
        self.top.config(bg=t["body"])
        ink = t["fg_value"] if self._is_me else t["fg_text"]
        for w in (self.line, self.cls, self.nums):
            w.config(bg=t["body"], fg=ink)
        self.dmg_track.config(bg=t["track"])
        self.dmg_bar.config(bg=t["dmg"])
        self.heal_track.config(bg=t["track"])
        self.heal_bar.config(bg=t["heal"])

    def _trim(self, text, cells):
        """`text` cut down until it fits `cells` monospace cells.

        Measured against the font rather than counted, because a character
        count is only a width for the glyphs the mono face actually carries.
        Nothing is padded — the grid column does that, exactly, which spaces
        cannot."""
        f = self.fonts["mono_10"]
        target = (f.measure(" ") or 1) * cells
        text = text or ""
        while text and f.measure(text) > target:
            text = text[:-1]
        return text

    def show(self, rank, p, dps, pct, focused, dmg_frac, heal_frac,
             show_heal=True, cls_tag=""):
        if not self._packed:
            self.f.pack(fill="x", pady=1)
            self._packed = True
        self._name = p.name
        tag = "▸ " if focused else "  "
        me = "*" if p.is_me else " "
        # Name and class are separate columns. The class is what tells you
        # whether the number next to it is good — a Priest at the bottom of a
        # damage meter is doing their job — and it reads far better down a
        # column of its own than trailing each name in brackets.
        #
        # Each column is a label of its own in a grid whose widths are set in
        # pixels, so nothing is padded and nothing can push its neighbour along.
        # The name is only ever TRIMMED to fit; grid does the rest. minsize is a
        # floor, not a ceiling — which is why the trim has to be measured, or a
        # wide name would simply widen its column and undo the whole exercise.
        cell = self.fonts["mono_10"].measure(" ") or 1
        self.top.grid_columnconfigure(0, minsize=cell * (5 + METER_NAME_CELLS))
        self.top.grid_columnconfigure(1, minsize=cell * METER_CLASS_CELLS)
        line = f"{tag}{rank}.{me}{self._trim(p.name, METER_NAME_CELLS)}"
        nums = f"{int(p.total):>9} {dps:>6.0f} {pct:>3.0f}%"
        if show_heal:
            nums += f"{int(p.heal_total):>9}"
        self._is_me = p.is_me
        ink = (self.theme["fg_value"] if p.is_me else self.theme["fg_text"])
        self.line.config(text=line, fg=ink)
        self.cls.config(text=cls_tag, fg=ink)
        self.nums.config(text=nums, fg=ink)
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

# Top-level keys the current hook needs out of the two generated files. Data
# generated by older tools predates some of these, and the hlboot.dat stamp
# alone can't tell (the *game* hasn't changed, our tools have) — so a file
# missing any of them forces a regenerate.
#
# BOTH files have to be checked, and that is not a detail. 2.3 shipped with
# only the resolver list here, and every upgrade from 2.2 came up with a dead
# minimap: %LOCALAPPDATA% still held offsets generated before the minimap
# existed, missing Entity, ArrayObj, Element and the rest. The game hadn't
# changed, so the stamp matched; the resolver keys listed here were all
# present, so the currency check passed; and sweepWorld's first line is
# `if (!OFF.Entity || !OFF.ArrayObj) return`, which fails silently forever.
# Add to these lists whenever the hook starts reading something new.
REQUIRED_RESOLVER_KEYS = ("anchors", "cam_targets", "count_targets", "funcs",
                          "ui_targets")
# The minimap's half of the offsets. The combat half (DamageResult, HitData,
# ...) is deliberately not listed: it has been there since the first release,
# so it can't be what an upgrade is missing, and a list that mentions
# everything is a list nobody maintains.
REQUIRED_OFFSET_KEYS = ("Activity", "ArrayObj", "Camera", "Element", "Entity",
                        "Foe", "GameLayer", "Hero", "Interactible", "State",
                        "String", "Unit")


def _data_is_current():
    """True if both generated files carry everything the hook reads.

    Missing keys are named rather than just counted: this runs before the
    overlay exists, so the log is the only place anyone can see why a
    multi-second regenerate just happened."""
    for name, required in (("resolver_data.json", REQUIRED_RESOLVER_KEYS),
                           ("meter_offsets.json", REQUIRED_OFFSET_KEYS)):
        try:
            d = json.loads((ANALYSIS / name).read_text())
        except Exception:
            return False
        missing = [k for k in required if not d.get(k)]
        if missing:
            print(f"[meter] {name} predates this build — missing "
                  f"{', '.join(missing)}; regenerating.", file=sys.stderr)
            return False
    return True


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

    def configure_hook(**kw):
        """Push a setting to the running agent. Wrapped so the overlay doesn't
        have to know about frida, and so a dead script is a logged failure
        rather than an exception in a menu callback."""
        if script is None:
            return
        script.post(dict(kw, type="config"))

    overlay = Overlay(session, pid, ui_state, world, configure=configure_hook)
    # Push the starting rate, since the agent boots on its own default.
    overlay._set_map_rate(overlay._map_rate)
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
