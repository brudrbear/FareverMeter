"""
farever_meter.py — Farever+ party damage meter (memory-reading edition).

Attaches to Farever via Frida, injects meter_hook.js (which hooks the game's
own ent.Unit.onInflictDamage and ent.Unit.playHitHealFX and streams every
player's damage and healing with real spell IDs), and renders two overlay
windows:

  * the METER: every player sorted by damage done, with damage/DPS/%, healing
    done and the share of it that was overheal, and
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

import colorsys
import ctypes
import json
import math
import os
import queue
import re
import sys
import tempfile
import threading
import time
import zlib
import tkinter as tk
from ctypes import wintypes
from tkinter import font as tkfont
from collections import defaultdict, deque
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
# Your fastest kill of each boss. Beside the two caches above rather than
# inside either: "Reset window positions" must not erase records, and the
# settings file is rewritten on every toggled checkbox — a record only needs
# writing when it's beaten. Same home as the positions, so it survives updates.
BEST_TIMES_CACHE = _WRITABLE / ".meter_besttimes.json"
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
# Hits the game reports a full `_amount` for but the target never takes.
#
# st.skill.DamageResult.blocker carries a _Data.$GameBeatKind_Impl_ name:
# AttackBlock, DamageDodge, Backstabbed, Critical, InvulnerableHit,
# BlockWellTimed, Missed. Measured against Ratsar's immune phase — 33 hits
# reported with blocker='InvulnerableHit', amount > 0 and _block == 0, every
# one of them counted by the meter and none of them touching his health.
#
# Only InvulnerableHit is listed, because only InvulnerableHit was measured.
# Missed and DamageDodge read like they belong here too, but a blocker that
# turns out to still deal damage would mean silently DROPPING real hits, which
# is a worse and far less visible bug than counting fake ones. Everything not
# listed is still reported by the mitigated-hit log, so adding one later is a
# one-line change backed by the same evidence this one was.
NULLIFIED_BLOCKERS = frozenset({"InvulnerableHit"})

# ---------------------------------------------------------------------------
# Patch quirks
# ---------------------------------------------------------------------------
# Things this build of Farever does that the meter has to work around, kept in
# one block so they can be removed in one edit when a patch fixes them. Nothing
# else in the file should grow a special case for a single skill: it goes here,
# or it doesn't go in.
#
# --- boosted damage (patch 2026-07, still live) ---
# "Swarmstrike Accord" (the Sword_Swarm weapon, "Beefury, Blessed Blade of the
# Farseeker") buffs every player in range so their hits deal bonus damage — and
# the game credits that bonus to the BUFF'S CASTER, not to whoever swung. In a
# rift, where the buff is landing on a dozen people who are all attacking, the
# wielder's row stops being a measurement of what they did: it is mostly other
# people's damage wearing their name.
#
# So hits from a boosted skill are pulled out of the damage total and counted
# in a column of their own. They are NOT dropped: the damage is real and it
# lands on the target, it just belongs to nobody in particular, and hiding it
# would leave a rift's numbers quietly short. Boost keeps the encounter alive
# (it is damage happening in a fight) but stays out of DMG, DPS, %, the element
# split and the skill breakdown, all of which are per-player claims.
#
# Matched by DISPLAY NAME, because that is what can be checked. The skill ids
# for this weapon are Sword_Swarm_{Combo,Skill1,Skill1_SelfBuff,Skill1_Status,
# Passive,Passive_Swarm,Passive_Poison} — and most of those are the wielder's
# own attacks, which must keep counting, so a Sword_Swarm_* prefix would throw
# away real damage. Which id carries the boost is not in hlboot.dat (display
# names live in the cdb, resolved live by the hook), so it has not been
# measured, and a guess here would be an invisible one. The host logs the id
# the first time a boosted hit arrives — see `boost_seen` in the message pump —
# and once that name is in the log it can be pinned in BOOST_SKILL_IDS below,
# which then also survives a non-English client.
#
# If the cdb name never resolves the match silently misses and the damage counts
# as damage again — i.e. what the meter did before this. The tell is a breakdown
# row reading "Sword Swarm ..." (the prettified id fallback): that id goes in
# BOOST_SKILL_IDS and the match is exact from then on.
#
# Lower-cased on both sides; the display name is whatever the cdb says.
BOOST_SKILL_NAMES = frozenset({"swarmstrike accord"})
BOOST_SKILL_IDS = frozenset()       # e.g. {"sword_swarm_passive_swarm"}


def is_boost_hit(ev) -> bool:
    """Is this hit damage the game credited to a player who didn't deal it?

    Reads the event the hook sends, so both aggregators and the ingest logging
    ask exactly the same question of exactly the same fields."""
    if (ev.get("name") or "").strip().lower() in BOOST_SKILL_NAMES:
        return True
    return (ev.get("skill") or "").strip().lower() in BOOST_SKILL_IDS


# How much damage a boss-pull reset keeps rather than wiping.
#
# The reset is driven by the game's boss healthbar, and that bar is refreshed on
# a 2/s timer — so up to half a second passes between the pull landing and the
# meter hearing about it, plus however long the engagement takes to register at
# all. A player opening on a boss dumps their whole burst into that gap, and a
# plain reset throws exactly the numbers they wanted away.
#
# So the reset rewinds instead: damage newer than this is replayed into the
# fresh encounter with its original timestamps. Long enough to cover an opening
# burst and the detection lag, short enough not to drag in the trash pack you
# finished on the way over.
BOSS_PULL_BACKLAG_SECS = 4.0
# Rolling event buffer backing that rewind. Bounded by count as well as age so a
# big party in a busy fight can't grow it without limit — at ~4s of backlag this
# is far more headroom than the window can use.
RECENT_EVENT_MAX = 2048
REFRESH_MS = 250
# The input pump's tick — how long the overlay can take to notice you opened
# the game's escape menu, clicked a menu button, or freed the cursor. Separate
# from REFRESH_MS because they answer different questions: 250 ms is plenty
# often to redraw damage numbers, and far too slow to feel like a keypress.
# ~30 fps costs two user32 calls and a queue drain per tick.
UI_TICK_MS = 33
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
# The boss kill-time toast, below all three: the hint and the parse banner
# share the strips above, and the rift panel's default sits at 190. A kill can
# coincide with any of them (a parsed boss, a world boss with a countdown up),
# so it gets its own line rather than a timeshare.
TOP_STRIP_KILL = 240
KILL_TOAST_SECS = 8.0       # how long the time stays on screen

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
# while you drag the slider, the rift prompt because it's a question that
# has to be answered, and the rift report because it's a page of numbers you
# stopped to read — the slider is for the things that sit over the fighting.
TRANSPARENCY_EXEMPT = ("menu", "hint", "prompt", "report")
FADE_SECS = 0.45
# The control menu and its hint don't fade AT ALL. They answer to a keypress,
# and a keypress wants a frame, not an animation: even a fast fade is time
# spent watching a panel arrive that you already asked for. Zero means the
# window is mapped at full opacity immediately — see _want_visible, which
# bypasses the fade driver entirely rather than running a one-step fade (the
# driver only wakes every FADE_STEP_MS, so "instant" through it would still
# cost a tick).
MENU_FADE_SECS = 0.0
# The rift prompt and the report card keep a short fade. They are not answers
# to a keypress — one interrupts you with a question, the other is a page of
# numbers that appears when a fight ends — and something arriving unbidden
# reads better easing in than snapping into existence.
PANEL_FADE_SECS = 0.15
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

# Elements that stand down while the game has a boss/elite healthbar up. The
# compass has to: the game's bar lands on the same strip of screen. The minimap
# joins it because a boss pull is when you are watching the fight rather than
# navigating — and because two navigation panels leaving together reads as
# intentional, where one leaving looks like a glitch.
# Not a setting: the escape menu brings both back, so nothing is unreachable.
BOSS_HIDDEN = ("compass", "minimap")

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
# The world-map backdrop under the minimap markers. Assets are built offline
# by hltools/build_map_assets.py from the game's own map tiles and committed —
# the meter never reads game files at runtime. The blend pulls the artwork
# toward the panel colour so the markers stay the loudest thing on the map.
MAPS_DIR = ROOT / "assets" / "maps"
MINIMAP_BG_TINT = 0.45
# The canvas is redrawn at roughly twice the sweep rate. Matching them exactly
# would beat against the hook's timer and drop or double frames; drawing a bit
# faster than the data arrives keeps motion even.

# The floor and ceiling the range is allowed to take. The Zoom control below
# moves it between these and nowhere else.
#
# The ceiling is the hook's foe cull (SWEEP_RADIUS_FOE, 600u) and not a pixel
# further. Everything else — chests, orbs, obelisks, respawn points,
# activities, players — is already swept from the WHOLE layer at any distance,
# so those keep appearing however far you zoom out; foes are the one category
# with a radius. Past 600 the map would show navigation markers with the mobs
# thinning out around them, which reads as the map breaking rather than as the
# cull it is. At 600 exactly, the two edges coincide and nothing looks wrong.
MINIMAP_RANGE_MIN, MINIMAP_RANGE_MAX = 80, 600

# Zoom is a percentage because that is what it looks like on screen: 100% is
# the range the map shipped with, larger numbers magnify, smaller ones pull
# back. BOTH ends are DERIVED from the range floor and ceiling rather than
# typed in, so they cannot drift apart from them — a hand-written bound is
# exactly how a slider ends up with a dead end after someone edits a constant.
# Rounded inward to whole slider steps so neither extreme can request a range
# fractionally outside what the clamp allows.
MINIMAP_ZOOM_STEP = 5
MINIMAP_ZOOM_MIN = -(-int(MINIMAP_RANGE / MINIMAP_RANGE_MAX * 100)
                     // MINIMAP_ZOOM_STEP) * MINIMAP_ZOOM_STEP
MINIMAP_ZOOM_MAX = (int(MINIMAP_RANGE / MINIMAP_RANGE_MIN * 100)
                    // MINIMAP_ZOOM_STEP) * MINIMAP_ZOOM_STEP

# Icon scale, as a percentage applied ON TOP of MINIMAP_ICON_SCALE. One
# multiplier over the whole style table is the entire trick: every marker keeps
# its size RELATIVE to the others (an obelisk stays half again a chest), so
# this makes them all bigger or smaller without flattening them to one size.
MINIMAP_ICONS_MIN, MINIMAP_ICONS_MAX = 60, 200

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
    # Gathering nodes, mineable only — the hook drops depleted ones outright
    # (hitPoints 0, waiting on their respawn timer), so a marker here always
    # means "walk over and you can gather it". Both get pictorial glyphs
    # rather than borrowed geometry: a silver rock cluster and a green leaf
    # are what the things ARE, which beats any legend. Slightly larger radii
    # than the squares and dots carry — an irregular silhouette has less
    # visual mass than a solid square of the same r, so these sit level with
    # the chest rather than over it.
    ("ore",      {"fill": "#C9D3DC", "r": 5.2, "shape": "rock"}),
    ("herb",     {"fill": "#5ED97A", "r": 5.0, "shape": "leaf"}),
    # The smallest thing on the map, and drawn last so it sits on top of
    # everything. There are far more of these than anything else — a pack is a
    # dozen dots on one spot — so size is what keeps them from swamping the
    # markers you navigate by. Hovering one still works: the hit test has its
    # own slack (MINIMAP_TIP_RADIUS) and doesn't shrink with the dot.
    ("foe",      {"fill": "#FF5348", "r": 2.4, "shape": "dot"}),
)
MINIMAP_STYLE_MAP = dict(MINIMAP_STYLE)
MINIMAP_ORDER = [k for k, _ in MINIMAP_STYLE]

# Per-material styling for gathering nodes, keyed by the CDB Gatherable row id
# the hook ships as `g` — prefix-matched, since rows come as Foo_Small /
# Foo_Large. The shape stays the category (rock = ore, leaf = herb); the fill
# is the material, roughly the colour of the thing itself. Rarity is the
# game's own data, not a judgement call: each node's hitLoot item in data.cdb
# carries a rarity, and TungsteneOre and ZealotusPetal are the two Rare ones —
# everything else reads Common. The rares get a halo ring and a size bump, the
# same "this one is special" grammar the orb's ring already speaks.
NODE_STYLES = (
    ("Ore_Copper",   {"fill": "#E28B58"}),   # copper: warm and brown
    ("Ore_Iron",     {"fill": "#C9D3DC"}),   # iron: the plain silver
    ("Ore_Tin",      {"fill": "#9FB8CE"}),   # tin: paler and colder than iron
    # Tungsten is dark metal under a bright halo — the dark body is what lets
    # the halo carry "rare" instead of the fill having to shout it.
    ("Tungstene",    {"fill": "#6E7F94", "ring": "#F2F7FF", "rare": True}),
    ("Madrigold",    {"fill": "#E8C558"}),   # marigold gold
    ("Lavendula",    {"fill": "#B08FE8"}),   # lavender
    ("AncientThyme", {"fill": "#3FA86B"}),   # deep thyme green
    ("Zealotus",     {"fill": "#E8506E", "ring": "#FFD9E4", "rare": True}),
)
NODE_RARE_SCALE = 1.18          # on top of the category radius


def _node_style(g):
    """The material override for a node's `g` (Gatherable row id), or None —
    None falls back to the category style, which is what an unmapped material
    added in a future patch should do rather than vanish."""
    if g:
        for prefix, st in NODE_STYLES:
            if g.startswith(prefix):
                return st
    return None


# The minimap's own show/hide, grouped the way you'd think about them rather
# than one tick per sweep category: nobody wants orbs without chests.
#
# Obelisks, respawn points and soulstones are deliberately absent and always
# drawn. They're the landmarks you navigate BY — there are a handful in a zone,
# they never move, and they're the least likely thing anyone wants gone. A tick
# each would be four more rows of menu for a problem nobody has.
MINIMAP_FILTERS = (
    ("collect",    "Collectibles", ("orb", "chest")),
    # One tick for both node kinds: there are no professions to specialise —
    # everyone gathers everything — so ore-without-herbs isn't a want the way
    # chests-without-orbs isn't.
    ("nodes",      "Ore & herb nodes", ("ore", "herb")),
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
    # Same single tick as the map's: no professions, so nobody farms ore
    # without herbs. The compass pair stays independent of the map's — see
    # the note above these tables.
    ("nodes",   "Ore & herb nodes", ("ore", "herb")),
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

# The Social tab tints each class so a long roster can be scanned by shape
# rather than read line by line. Muted on purpose: this is a list you look
# things up in, not a chart, and four saturated colours down a column fight the
# names for attention. Anything unrecognised (a class a patch adds) falls back
# to plain body text rather than picking a colour at random.
CLASS_COLORS = {"Warrior": "#D98A5A", "Mage": "#6FA8DC",
                "Priest": "#C9B87A", "Rogue": "#87B37A"}

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
    "soulstone": "Soulstone", "ore": "Ore", "herb": "Herb",
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
# Tk canvas items have no alpha, so the badge can't be knocked back where it's
# drawn: on the colorkey window a pixel is either a solid colour or a hole to
# the game, nothing between. It spent two versions faking it with a "gray75"
# stipple — three-quarters of the pixels painted, a quarter left as holes —
# which averages to the right darkness and reads as dither the moment you look
# at it. The boxes now live on their own layered window glued under the
# compass (see _build_compass), because whole-window opacity is the one kind
# of blending Windows does give us. This is that window's opacity, multiplied
# by whatever the compass itself is currently faded to (_sync_badgewin), so
# the badges keep exactly the knocked-back-relative-to-the-numbers look the
# stipple was approximating.
COMPASS_DIST_BOX_ALPHA = 0.75
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
COMPASS_CATS = ("chest", "orb", "hero", "soulstone", "obelisk", "ore", "herb")
# Two categories keep a radius, and they're the two that are worth knowing
# about when you're near one and noise when you aren't — which is the opposite
# of how the chests and party members on this strip behave. Obelisks came off
# the compass entirely at one point for that reason: ten in a zone, permanently
# there, and at whole-map range they were most of what the strip was carrying.
# With a radius they're useful again without being the wallpaper.
COMPASS_LIMITS = {"soulstone": 200.0, "obelisk": 200.0,
                  # Nodes are the obelisk case again: static, always some in
                  # the loaded area, and at whole-map range they'd be most of
                  # what the strip carries. Near one, the bearing is useful;
                  # the map is what answers "where's the nearest one at all".
                  "ore": 200.0, "herb": 200.0}
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

# ---- rift report leaderboard ----
# Medal colours for ranks 1-3, tuned to read on the near-black rift body —
# true silver (#C0C0C0) goes muddy there, so it leans bluer and lighter.
REPORT_MEDALS = ("#FFD24A", "#CDD6E0", "#D89B66")
REPORT_HEAL = "#8AE28A"         # the healer's colour, distinct from any medal

# Big mono while counting; smaller and quieter for the idle placeholder, which
# is only ever on screen so the window can be dragged into place.

ACCENT = "#3D7C7C"
DMG_BAR = "#5279B5"       # blue — damage bars
HEAL_BAR = "#5E9C4A"      # green — healing bars
# Healing done to yourself, drawn as the LEFT segment of every healing bar so
# the split reads at a glance down a column. A vivid green-teal — still the
# healing family, because healing yourself is still healing, but saturated
# where HEAL_BAR is muted. The shade has been round the houses: teal first
# (read as a shield), then off-white (too stark), then a washed-out green
# (#D8E9D0 — separated well from the green, but nearly landed on the Farever
# theme's tan track), and now this.
#
# The separation this one trades on is CHROMA AND HUE, not lightness. Against
# HEAL_BAR the greyscale ratio is only 1.35:1, which reads as a failure and
# isn't — measured as colour the gap is deltaE2000 12.3, five times the
# just-noticeable step, and it is a vivid-vs-muted jump rather than a
# light-vs-dark one. A contrast ratio cannot see that, which is why the colour
# is checked in Lab and on screen rather than by ratio.
#
# What it fixes: the old shade sat at deltaE 18.3 from the Farever track
# (#D9C09A) and 14.7 from that theme's body — close enough that a short self
# segment on a light theme could read as empty track. This one is at 31.3 and
# 32.9, and clears every theme's track and body by a wide margin. The binding
# constraint has moved back to the HEAL_BAR boundary, which is the one that
# only has to hold across a hard edge at five pixels tall.
SELF_HEAL_BAR = "#08BD71"
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
    # The rift report's leaderboard: an MVP name is the headline of the card
    # and reads like one; the top-three ranks sit between it and body text.
    "ui_mvp_b":   ("Segoe UI", 17, "bold"),
    "ui_rank_b":  ("Segoe UI", 12, "bold"),
    "ui_idle_i":  ("Segoe UI", 10, "italic"),
    "mono":       ("Consolas", 9),
    "mono_10":    ("Consolas", 10),
    "mono_sm":    ("Consolas", 8),
    "mono_xl_b":  ("Consolas", 18, "bold"),
}
# The floor is where the fonts stop moving: sizes are clamped at 6pt inside
# _set_group_scale, and every body font in FONT_SPECS has hit that clamp by
# ~55%. A slider that goes lower would keep moving while the window stayed
# put — the same lie the MIN_W comment below warns about.
UI_SCALE_MIN, UI_SCALE_MAX = 50, 175      # percent
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
# Where each group's slider starts when nothing is saved; absent means 100%.
# The settings panel defaults to 130: it's read at arm's length mid-game with
# the escape menu up, and the tabbed layout left it room to be bigger. Applied
# through the same path as a restored slider (see __init__), so the
# fonts-at-100 assumption inside _set_group_scale holds either way — a saved
# value, including an explicit 100, always wins over this table.
SCALE_DEFAULTS = {"menu": 1.30}
# Wider than the UI's: a minimap is worth making genuinely large on a big
# screen, and genuinely small when it's only there for a glance.
MINIMAP_SCALE_MIN, MINIMAP_SCALE_MAX = 50, 250
# How the mount feature picks from the favorites: a fresh roll per summon, or
# strict rotation through the list. The hook receives it lowercased.
MOUNT_MODES = ("Random", "Cycle")
# Minimum widths, at 100%. They're pixel values, so the scale slider has to
# scale them too or scaling down just hits the floor and nothing moves.
# The menu is wide because its tabs run down the LEFT rather than across the
# top: the navbar eats a fixed strip, and what is left has to still be a
# comfortable page. It also has to fit the Social tab's widest row — a name, a
# class, a level and two buttons — without that row deciding the window size on
# its own.
# The meter grew by one 6-cell column (OVER%) when healing stopped meaning
# "health restored" and started meaning "healing done", and the floor had to
# grow with it or the new column would be drawn off the right edge of every
# window narrow enough to be at the old minimum.
MIN_W = {"meter": 404, "detail": 320, "menu": 620, "prompt": 320,
         "update": 380}
# The update offer's body text wrap. Wider than the rift prompt's because it
# explains what pressing the button will do, which is two sentences rather than
# a question.
UPDATE_OFFER_WRAP = 400
MENU_NAV_W = 128                           # the tab strip, at 100%
# The Social list's viewport. Fixed rather than growing with the roster: the
# menu is already the tallest window the overlay puts on screen, and a hub of
# 40 people would otherwise run it off the bottom of the display.
SOCIAL_LIST_H = 300
# Monospace cells for the roster's name and class columns, so the buttons on
# the right all start at the same x however long the names are.
SOCIAL_NAME_CELLS = 17
SOCIAL_CLASS_CELLS = 8
STEAM_PROFILE_URL = "https://steamcommunity.com/profiles/{}"
# How the Social roster can be ordered. "name" is a directory and keeps YOU at
# the top, because the first thing you check is that the list is about the
# shard you think it is. "level" is a ranking, so it does not pin anyone —
# a leaderboard with someone glued to the first row is not a leaderboard.
SOCIAL_SORTS = ("name", "level")
SOCIAL_SORT_LABEL = {"name": "Sort: Name", "level": "Sort: Level"}
# The Social tab's two views. "shard" is live state and can show class/level;
# "session" is an accumulated log and deliberately cannot — see WorldSnapshot.
SOCIAL_PAGES = (("shard", "Current Shard"), ("session", "This session"))
# The session log's own ordering. Recency first by default: the log exists to
# answer "who was that just now", and the answer is at the top.
SESSION_SORTS = ("recent", "name")
SESSION_SORT_LABEL = {"recent": "Sort: Last seen", "name": "Sort: Name"}
SOCIAL_SEEN_CELLS = 7


def _seen_ago(secs):
    """How long ago, in the width of a table cell.

    Anyone still on your shard has their timestamp refreshed every sweep, so
    "now" is not an approximation — it is the column saying they are still
    here, which is the distinction the log is actually for.
    """
    if secs < 45:
        return "now"
    mins = int(secs // 60)
    if mins < 1:
        return "now"
    if mins < 60:
        return f"{mins}m"
    hrs, rem = divmod(mins, 60)
    return f"{hrs}h{rem:02d}m"
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
    "heal_self": SELF_HEAL_BAR,
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
    "heal_self": SELF_HEAL_BAR,
    "header_off": "#3B3036",
    "map_body": RIFT_BODY,
}

# The game's affinity vocabulary as it actually arrives off
# DamageResult.affinity (yes, Cheese), each with a colour. This is the single
# element-colour table — the rift report reads it through element_color().
ELEMENT_COLORS = {
    "Physical": "#B68A4E", "Magic": "#5279B5", "Fire": "#C9612A",
    "Spark": "#D9B43C", "Earth": "#7C5A2E", "Water": "#4B8FB5",
    "Faith": "#C8B280", "Light": "#E5C95A", "Raw": "#8A6A4A",
    "Cheese": "#D8C25E", "Chaos": "#8E4FB5", "None": "#9A8B7A",
}
_ELEMENT_FOLD = {k.lower(): v for k, v in ELEMENT_COLORS.items()}


def element_color(name):
    """The colour a damage type wears — table first (case-folded), then a
    stable pastel from the name hash, so an affinity a patch adds arrives
    tinted rather than invisible, and the same colour every session."""
    key = (name or "?").strip().lower()
    hit = _ELEMENT_FOLD.get(key)
    if hit:
        return hit
    h = (zlib.crc32(key.encode("utf-8")) % 360) / 360.0
    r, g, b = colorsys.hsv_to_rgb(h, 0.45, 0.95)
    return f"#{int(r * 255):02X}{int(g * 255):02X}{int(b * 255):02X}"

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


def _fetch_release_notes(tag):
    """The release body for a tag, or None. Same never-fail-loudly rule as the
    version check: a missing what's-new window is not worth a dialog."""
    try:
        rel = _fetch_json(UPDATE_API_RELEASE_TAG + str(tag))
    except Exception as e:
        print(f"[update] couldn't fetch the notes for {tag}: {e}",
              file=sys.stderr)
        return None
    body = (rel or {}).get("body")
    return body.strip() if body and body.strip() else None


# Deliberately crude: the notes are GitHub Markdown and this is a Tk text
# widget, so the goal is "reads cleanly", not fidelity. Headings keep their
# text, emphasis and code ticks come off, links keep their label, and the
# blockquote/rule furniture becomes whitespace.
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_HEAD = re.compile(r"^\s{0,3}#{1,6}\s*")
_MD_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")


def _markdown_to_text(md):
    out = []
    for line in md.splitlines():
        if _MD_RULE.match(line):
            out.append("")
            continue
        line = _MD_HEAD.sub("", line)
        line = re.sub(r"^\s{0,3}>\s?", "", line)
        line = _MD_LINK.sub(r"\1", line)
        line = line.replace("**", "").replace("`", "")
        out.append(line.rstrip())
    text = "\n".join(out)
    # Collapse the runs of blank lines the stripping leaves behind.
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _release_installer_asset(rel):
    """(download_url, size) of the release's Setup.exe, or (None, 0).

    Matched on the name the build script writes rather than "first asset":
    a release can grow extra attachments (notes, checksums) without the
    updater downloading one of those instead."""
    for a in rel.get("assets") or []:
        name = (a.get("name") or "")
        if name.startswith("FareverMeter-") and name.endswith("-Setup.exe") \
                and a.get("browser_download_url"):
            return a["browser_download_url"], int(a.get("size") or 0)
    return None, 0


def _latest_version():
    """The newest published version as (tuple, name, url, asset_url,
    asset_size), or None.

    Releases first, tags as the fallback: the repo has so far shipped tags
    without a Release object behind them, and /releases/latest answers 404 in
    that state — so tags-only has to work or the check never fires. Only a
    real Release carries an installer asset; the tag fallback leaves it None,
    which is what sends the notice down the open-the-browser path."""
    try:
        rel = _fetch_json(UPDATE_API_RELEASE)
        v = _version_tuple(rel.get("tag_name"))
        if v:
            asset_url, asset_size = _release_installer_asset(rel)
            return (v, rel.get("tag_name"), rel.get("html_url") or REPO_URL,
                    asset_url, asset_size)
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
            best = (v, t.get("name"), f"{REPO_URL}/releases", None, 0)
    return best


# The check runs at startup and again on every loading screen (see the zone
# handler), which is the closest thing the meter has to "the player is between
# things and would not mind hearing about a new version".
#
# Throttled, because loading screens are not rare: GitHub's unauthenticated API
# allows about 60 requests an hour per IP, and a rift session can put you
# through more zone changes than that. Once a newer version HAS been found
# there is nothing left to learn, so the checks stop entirely.
UPDATE_RECHECK_SECS = 900.0        # 15 minutes between checks at most
_update_checked_at = [0.0]         # time.monotonic of the last attempt
UPDATE_BTN_FLASH_MS = 4000         # how long the manual button shows a result


def _record_newer(found):
    """Publish a check result into UPDATE if it names a newer version than
    this build; returns whether it did. Shared by the automatic check and the
    menu's manual button, so the two can't drift on what "newer" means.
    "latest" is written last: the overlay's tick treats it as the ready flag,
    and the asset fields have to be in place before it fires."""
    mine = _version_tuple(VERSION)
    if not found or mine is None:
        return False
    v, name, url, asset_url, asset_size = found
    if v <= mine:
        return False
    UPDATE["asset"], UPDATE["asset_size"] = asset_url, asset_size
    UPDATE["latest"], UPDATE["url"] = name, url
    print(f"[update] {name} is available (running {VERSION}) — {url}"
          + ("" if asset_url else " (no installer asset — notice will "
             "open the browser instead of self-updating)"),
          file=sys.stderr)
    return True


def check_for_update(announce=False):
    """Ask GitHub whether there's a newer version, on a background thread.

    Never blocks startup and never fails loudly: being offline, rate-limited or
    caught in a GitHub outage should cost the notice, not the meter. Set
    FAREVER_NO_UPDATE_CHECK to skip the request entirely.

    `announce` also logs the boring "up to date" answer. The startup check does;
    the loading-screen ones don't, or the log fills with a line every 15 minutes
    saying nothing happened."""
    if os.environ.get("FAREVER_NO_UPDATE_CHECK"):
        if announce:
            print("[update] check disabled by FAREVER_NO_UPDATE_CHECK.",
                  file=sys.stderr)
        return
    if UPDATE["latest"]:
        return          # already found one; the notice is up, stop asking
    now = time.monotonic()
    # Set before the thread starts, so two zone changes in quick succession
    # can't put two requests in flight.
    if not announce and now - _update_checked_at[0] < UPDATE_RECHECK_SECS:
        return
    _update_checked_at[0] = now

    def work():
        found = _latest_version()
        if _record_newer(found):
            # Automatic discovery, so the overlay may offer the update rather
            # than only writing a line into the menu. Set after _record_newer,
            # which publishes the asset fields the offer needs.
            UPDATE["prompt"] = True
        elif found and announce:
            print(f"[update] up to date (running {VERSION}).", file=sys.stderr)

    threading.Thread(target=work, daemon=True, name="update-check").start()


# Finishing the update is the INSTALLER's job, not a helper's.
#
# This used to hand off to a detached, hidden PowerShell script that polled
# until this process died, ran the installer with /SILENT /SUPPRESSMSGBOXES,
# and relaunched the replaced exe. Every one of those steps is a step malware
# takes, and Windows Defender agreed: it quarantined FareverMeter.exe as
# `Behavior:Win32/DefenseEvasion.A!ml` — a BEHAVIOURAL detection, on more than
# one machine, each time right after an update.
#
# Hidden PowerShell with -ExecutionPolicy Bypass is the single most flagged
# pattern in Windows telemetry (ATT&CK T1059.001, and "defense evasion" is
# literally what the detection was named). Waiting for a parent to exit so you
# can overwrite its binary, then silently running an installer and relaunching
# it, is the rest of the same story.
#
# None of it was ever necessary. The helper existed only because a /SILENT run
# REFUSES to proceed while the meter is running (see AskToStopMeter in
# FareverMeter.iss — a silent run has nobody to answer its prompt, so it bails
# rather than hang), so something had to wait for us to die first. Run the
# installer the way a person would — visibly — and Inno asks politely on its
# own, and its [Run] entry offers to start the meter again afterwards. That
# entry is flagged `skipifsilent`, so under the old flow it never once ran.
#
# What is left is one ShellExecute of a file the user just agreed to install.


def open_installer(installer: Path):
    """Open the downloaded installer the same way a double-click would.

    os.startfile is ShellExecute: a normal, visible, user-facing launch with no
    interpreter, no hidden window and no policy bypass in sight. Raises on
    failure, and the caller falls back to the browser."""
    os.startfile(str(installer))  # noqa: S606 - a file the user chose to run


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
def _stamp_report_classes(report, world):
    """Freeze each player's class acronym into a finished rift report.

    Done once, at the kill, because the report is saved to disk and re-opened
    later — by then the world sweep has long forgotten a stranger who was in
    the rift, and the card would show a row of blanks. A player the sweep never
    saw gets "" and simply has no acronym."""
    for ph in report.get("phases", ()):
        for p in ph.get("players", ()):
            p["cls"] = _class_tag(world.class_of(p.get("name")))


def _report_name(p):
    """`Brudr (War)` for the report — the acronym only when one is known."""
    name = p.get("name") or "?"
    return f"{name} ({p['cls']})" if p.get("cls") else name


def _overheal_note(d, fmt=" ({:.0f}% over)"):
    """The overheal clause for a report dict, or "" when there is none to give.

    Rift reports are reloaded from JSON on disk, and a file written before
    healing meant RAW healing carries `heal` but no `heal_landed`. Treating
    that absence as zero would stamp every archived report 100% overheal, so
    an old report simply says nothing about overhealing — which is the truth
    about what it recorded."""
    landed = d.get("heal_landed")
    heal = d.get("heal") or 0.0
    if landed is None or heal <= 0.5:
        return ""
    return fmt.format(_overheal_pct(heal, landed))


def _overheal_pct(total, landed):
    """Share of `total` healing that restored no health, as a percentage.

    Clamped at 0 because the two figures come from different observations —
    a health rise can be attributed to a heal whose estimated size is smaller
    than the rise itself (a regen tick landing inside a heal's match window,
    say), and "-3% overheal" is not a thing to show anyone."""
    if not total or total <= 0.0:
        return 0.0
    return max(0.0, (total - landed) / total * 100.0)


class HealSizeEstimator:
    """How big was that heal? The client is never told, so this estimates it.

    Measured 2026-08-03 (`frida/run_heal.py`, 40 heal events across 6 healers
    and 4 skills): of the fifteen heal entry points in the build, ONLY
    `ent.Unit.playHitHealFX` runs client-side, and its `HitData.amount` reads
    0.000. `receiveHeal`, `computeHeal`, `evalHeal`, the four `*HealEval`
    callbacks, `applyHeal`, `rpcDisplayHeal(__impl)` and
    `ui.hud.EffectsFeed.displayHeal` never fire on a client at all. The only
    heal quantity observable here is the RISE in the target's replicated
    health — which is zero when the target is already full.

    A heal's size is therefore estimated as the HIGH-WATER MARK of what that
    player's casts of that skill have been seen to restore. A cast on a target
    missing more health than the heal restores lands in full, so the largest
    observation converges on the true per-cast value from below; every smaller
    one is a cast that was capped by the target's missing health, and every
    zero is a cast that was capped completely.

    Deliberately the maximum, not a mean or a quantile. Capping biases
    observations DOWN and there is no way to tell a capped observation from an
    uncapped one — `ent.UnitAttributes.maxHealth` reads 0 for heroes (measured
    in the same session), so "how hurt was the target" isn't available either.
    Averaging would report a healer as weaker the healthier their party was,
    which is the exact bug this replaces. The known cost is crits: once a skill
    has been seen to crit it is credited its crit value on every cast, so a
    crit-heavy healing build reads somewhat high.

    The window bounds that across a session — levels, gear and talent changes
    all move a skill's real value, and a lifetime maximum would pin the
    estimate to the best it ever was.

    Called only from the hook's message thread (the same thread that feeds
    PartySession), so it needs no lock of its own.
    """

    WINDOW = 64          # observations kept per (player, skill)

    def __init__(self, specs=None):
        self._obs: dict[tuple, deque] = defaultdict(
            lambda: deque(maxlen=self.WINDOW))
        # skill id -> {step index: [effect spec, ...]} out of the game's own
        # data.cdb (analysis_out/heal_specs.json). This is what makes a heal
        # on a full-health target countable at all, so its absence is worth
        # saying out loud rather than quietly falling back.
        self._specs = specs or {}
        self._computed = 0      # heals sized from the game's own numbers
        self._guessed = 0       # ...and heals that fell back to observation
        self._unsized = 0       # ...and heals nothing could size
        # skill -> (landed/computed, landed, computed) for the worst case seen
        self._audit: dict[str, tuple] = {}

    def size_from_spec(self, ev):
        """The heal's real size, computed the way the game computes it.

        `dyn` heals carry their amount in BaseSkill.dynVal1-3, which the server
        replicates; `scale` heals are a ratio on one of the caster's
        attributes. Both arrive on the event from the hook. Returns None when
        this skill isn't in the table, or when the inputs it needs are missing
        — a summon's attributes, say, or a step index that didn't match."""
        steps = self._specs.get(ev.get("skill"))
        if not steps:
            return None
        specs = steps.get(str(ev.get("step")))
        if specs is None:
            # One heal step is the common case (39 of 44 skills). When there is
            # exactly one, a step index that didn't line up doesn't matter.
            if len(steps) != 1:
                return None
            specs = next(iter(steps.values()))
        dyn, atb = ev.get("dyn") or [], ev.get("atb") or {}
        total = 0.0
        for spec in specs:
            amount = 0.0
            # A spec carrying both is a dyn with a floor: the dyn is the real
            # value and the base is what it falls back to.
            n = spec.get("dyn")
            if n and len(dyn) >= n and dyn[n - 1]:
                amount = float(dyn[n - 1])
            elif spec.get("scale"):
                for ratio, name in spec["scale"]:
                    if name not in atb:
                        return None      # MaxHealth/FoePower — not readable
                    amount += float(ratio) * float(atb[name])
            if not amount:
                amount = float(spec.get("base") or 0.0)
            total += amount
        return total if total > 0 else None

    def stamp(self, ev: dict) -> None:
        """Fold one heal event in and fill in its raw size.

        `landed` (what the health bar actually moved) arrives from the hook;
        `amount` leaves as the estimated size of the heal itself. Every
        downstream consumer already reads `amount`, so healing totals become
        raw healing without any of them knowing about the estimate."""
        landed = float(ev.get("landed", ev.get("amount", 0.0)) or 0.0)
        ev["landed"] = landed
        if not ev.get("est"):
            ev["amount"] = landed          # regen: observed AS the rise
            return
        obs = self._obs[(ev.get("player") or "?", ev.get("skill") or "?")]
        if landed > 0:
            obs.append(landed)
        # The game's own numbers first — they are right on the very first cast
        # and don't care whether the target had room for the heal. Observation
        # is only the fallback for the skills the table can't size.
        spec = self.size_from_spec(ev)
        if spec is not None:
            self._computed += 1
            ev["sized"] = "spec"
        else:
            spec = max(obs) if obs else 0.0
            if spec > 0:
                self._guessed += 1
                ev["sized"] = "seen"
            else:
                self._unsized += 1
                ev["sized"] = "none"
        # A size can never make a measured heal smaller than it actually was.
        ev["amount"] = max(landed, spec)
        # Ground truth, collected from ordinary play rather than a probe: a
        # heal that LANDED in full is a direct measurement of what that heal
        # was worth, so a computed size below it means the formula is wrong.
        # (Above it is expected and means nothing — the target was topped off.)
        if ev.get("sized") == "spec" and landed > 0:
            key = ev.get("skill") or "?"
            worst = self._audit.get(key)
            ratio = landed / spec if spec > 0 else 0.0
            if worst is None or ratio > worst[0]:
                self._audit[key] = (ratio, landed, spec)

    def drain_report(self):
        """A one-line summary for the log, or None when nothing has healed.

        The `under` entries are the ones worth reading: a skill whose computed
        size came out BELOW what it was measured to restore is a formula that
        needs fixing, not a rounding artifact."""
        computed, guessed, unsized = (self._computed, self._guessed,
                                      self._unsized)
        if not (computed or guessed or unsized):
            return None
        under = sorted((k, v) for k, v in self._audit.items() if v[0] > 1.02)
        self._computed = self._guessed = self._unsized = 0
        self._audit = {}
        line = (f"[meter] heal sizing: {computed} computed, "
                f"{guessed} from observation, {unsized} unsized")
        if under:
            line += "   UNDER-COMPUTED: " + ", ".join(
                f"{k} landed={v[1]:.0f} vs computed={v[2]:.0f}"
                for k, v in under[:4])
        return line


@dataclass
class PlayerAgg:
    name: str
    is_me: bool = False
    in_party: bool = False
    total: float = 0.0
    hits: int = 0
    crits: int = 0
    kills: int = 0
    heal_total: float = 0.0     # raw healing (see HealSizeEstimator)
    heal_landed: float = 0.0    # ...of which actually restored health
    heal_self: float = 0.0      # ...and of which the healer was the target
    heal_hits: int = 0
    # Damage the game credited to this player but somebody else swung for —
    # see "Patch quirks" at the top. Deliberately parallel to `total`/`hits`
    # rather than mixed into them: every other number on the row is a claim
    # about what this player did, and this one isn't.
    boost_total: float = 0.0
    boost_hits: int = 0
    boost_crits: int = 0
    # skill -> [hits, total, crits]  (damage)
    skills: dict[str, list] = field(default_factory=lambda: defaultdict(lambda: [0, 0.0, 0]))
    # skill -> [hits, total, crits, self_total]  (healing). The fourth column
    # is what makes a healing bar splittable: how much of that skill's healing
    # the caster put on themselves.
    heals: dict[str, list] = field(default_factory=lambda: defaultdict(lambda: [0, 0.0, 0, 0.0]))
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

    def record_boost(self, amount, crit, now):
        """A boosted hit: counted, timestamped, and kept out of everything
        else. It touches last_time because it IS this player being active in
        the fight — it just isn't their damage."""
        if self.first_time == 0.0:
            self.first_time = now
        self.last_time = now
        self.boost_total += amount
        self.boost_hits += 1
        self.boost_crits += crit

    def record_heal(self, skill, amount, crit, landed=0.0, is_self=False):
        self.heal_total += amount
        self.heal_landed += landed
        self.heal_hits += 1
        if is_self:
            self.heal_self += amount
        s = self.heals[skill]
        s[0] += 1; s[1] += amount; s[2] += crit
        s[3] += amount if is_self else 0.0

    @property
    def overheal_pct(self):
        """Share of this player's healing that restored no health.

        Zero — not "unknown" — when they have not healed at all: a row with no
        healing has nothing to have wasted."""
        return _overheal_pct(self.heal_total, self.heal_landed)


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
        # (timestamp, "hit"|"heal", event) for the last few seconds, so a
        # boss-pull reset can rewind instead of wiping. Only what was actually
        # recorded goes in here — anything the capture window rejected was never
        # part of the encounter and must not reappear.
        self._recent: deque = deque(maxlen=RECENT_EVENT_MAX)

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
        # A summon's hit carries `pet` — its raw Unit.kind. The damage already
        # merged into the owner's row upstream, so the breakdown is the only
        # place left that can show a pet was responsible for part of it.
        #
        # The KEY is prefixed with the raw kind (stable, and the same on a
        # non-English client), which also keeps a pet's ability separate from
        # an identically named one of the player's own. The NAME gets the
        # sheet's display name — "Nightling Terror: Attack".
        pet = ev.get("pet")
        if pet:
            sid = f"{pet}:{sid}"
            label = _summon_label(pet)
            nm = f"{label}: {nm}" if nm else label
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
            self._recent.append((now, "hit", ev))
            self._apply_hit(self._player_for(ev), ev, now)

    def _apply_hit(self, p, ev, ts):
        """Route one hit into the right bucket. Shared with the boss-pull
        rewind, so a replayed hit lands exactly where the live one did —
        including a boosted one, which must not reappear as damage."""
        if ev.get("boost"):
            p.record_boost(float(ev.get("amount", 0.0)),
                           int(ev.get("crit", 0)), ts)
        else:
            p.record(self._skill_of(ev), ev.get("element", "?"),
                     float(ev.get("amount", 0.0)), int(ev.get("crit", 0)),
                     int(ev.get("kill", 0)), ts)

    def record_heal(self, ev: dict):
        # Heals are recorded but never drive encounter boundaries: an
        # out-of-combat potion/regen must not roll the meter into a fresh
        # encounter (damage does that), so last_hit stays untouched.
        with self.lock:
            now = time.time()
            if not self._capturing(now):
                return
            self._recent.append((now, "heal", ev))
            p = self._player_for(ev)
            p.record_heal(self._skill_of(ev),
                          float(ev.get("amount", 0.0)), int(ev.get("crit", 0)),
                          float(ev.get("landed", 0.0)),
                          bool(ev.get("self")))

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
            self._recent.clear()
            self.epoch += 1

    def reset_keeping_recent(self, backlag=BOSS_PULL_BACKLAG_SECS):
        """Reset the encounter but carry the last `backlag` seconds forward.

        For the boss-pull reset. The healthbar the pull is detected from lags
        the pull itself, so a plain reset lands *after* the opening burst and
        deletes it — the single most interesting part of the parse. Replaying
        the buffered events with their ORIGINAL timestamps keeps the numbers,
        the per-player first/last times and the encounter start honest, rather
        than restamping everything to the moment the bar appeared and reporting
        a burst that took four seconds as instantaneous.

        Returns how many events were carried over, for the log."""
        with self.lock:
            now = time.time()
            cutoff = now - backlag
            keep = [e for e in self._recent if e[0] >= cutoff]
            self._reset(now)
            self.last_hit = 0.0
            self.in_combat = False
            self.capture_until = self.capture_start = None
            self._recent.clear()
            self.epoch += 1
            for ts, kind, ev in keep:
                self._recent.append((ts, kind, ev))
                p = self._player_for(ev)
                if kind == "hit":
                    if self.enc_start == 0.0:
                        self.enc_start = ts
                    self.last_hit = ts
                    self._apply_hit(p, ev, ts)
                else:
                    p.record_heal(self._skill_of(ev),
                                  float(ev.get("amount", 0.0)),
                                  int(ev.get("crit", 0)),
                                  float(ev.get("landed", 0.0)),
                                  bool(ev.get("self")))
            # Damage was landing, so the player was in combat for the whole
            # replayed stretch. Without this the duration clock would only start
            # at the next UI tick and those seconds would be missing from the
            # divisor — inflating the DPS of the very burst we just rescued.
            if self.enc_start:
                self.active_since = self.enc_start
                self.in_combat = True
            return len(keep)

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


class RiftRecorder:
    """Captures one rift run for the end-of-rift report.

    Fed the same hit/heal stream as PartySession but never reset by the
    player — its boundaries are the rift's own. Entering the rift starts
    phase 1 (the trash), the boss-pull edge starts phase 2 (the boss), and
    the kill that ends the fight freezes both into a report. Two phases and
    not a running meter, because that's the question the report answers:
    who carries the AoE clear and who carries the single-target, which are
    different players on purpose.

    A run that doesn't end in a kill — walking out, a wipe's loading screen —
    produces nothing. Half a rift isn't a rift report.

    Aggregates everything the hook sends rather than the meter's party/all
    mode: the mode can change mid-rift (the rift prompt exists to change it),
    and a report whose phase 1 and phase 2 counted different sets of players
    would be comparing nothing with nothing."""

    PHASE_LABELS = ("Rift phase", "Boss phase")

    def __init__(self):
        self.lock = threading.Lock()
        self.active = False
        self.phase = 0
        self._phases = [self._new_phase(), self._new_phase()]
        # (timestamp, phase, "hit"|"heal", event) — kept so the boss-pull
        # edge can move the opening burst across the phase boundary, same
        # trick (and same measured bar lag) as reset_keeping_recent().
        self._recent: deque = deque(maxlen=RECENT_EVENT_MAX)

    @staticmethod
    def _new_phase():
        return {"players": {}, "elements": defaultdict(float),
                "start": 0.0, "end": 0.0}

    @staticmethod
    def _player_of(ph, name):
        p = ph["players"].get(name)
        if p is None:
            p = {"name": name, "total": 0.0, "hits": 0, "crits": 0,
                 "kills": 0, "heal": 0.0, "heal_landed": 0.0, "heal_hits": 0,
                 "boost": 0.0, "boost_hits": 0}
            ph["players"][name] = p
        return p

    def _apply(self, ph, kind, ev, sign):
        """Add (or, for the phase-boundary rewind, subtract) one event. Every
        stat is a plain sum, which is what makes the rewind exact."""
        p = self._player_of(ph, ev.get("player") or "?")
        amount = sign * float(ev.get("amount", 0.0))
        if kind == "hit" and ev.get("boost"):
            # Same split as the live meter, and for the same reason: this is
            # the one number on the row that isn't a claim about the player.
            # See "Patch quirks".
            p["boost"] += amount
            p["boost_hits"] += sign
        elif kind == "hit":
            p["total"] += amount
            p["hits"] += sign
            p["crits"] += sign * int(ev.get("crit", 0))
            p["kills"] += sign * int(ev.get("kill", 0))
            ph["elements"][ev.get("element") or "?"] += amount
        else:
            p["heal"] += amount
            p["heal_landed"] += sign * float(ev.get("landed", 0.0))
            p["heal_hits"] += sign

    def set_rift(self, state: bool):
        with self.lock:
            if state:
                self.active = True
                self.phase = 0
                self._phases = [self._new_phase(), self._new_phase()]
                self._phases[0]["start"] = time.time()
                self._recent.clear()
            else:
                # Leaving normally happens after the kill, when the report has
                # already been taken; leaving mid-run abandons the recording.
                self.active = False

    def on_zone(self):
        """A loading screen means the player left the instance — a wipe or a
        walk-out. Whatever was building is not a finished rift."""
        with self.lock:
            self.active = False

    def record(self, kind, ev: dict):
        """kind is "hit" or "heal". Hits arrive already filtered of nullified
        damage — the caller drops those before the meter sees them too."""
        with self.lock:
            if not self.active:
                return
            now = time.time()
            self._recent.append((now, self.phase, kind, ev))
            self._apply(self._phases[self.phase], kind, ev, 1)

    def on_boss_pull(self, backlag=BOSS_PULL_BACKLAG_SECS):
        """The healthbar the pull is detected from lags the pull itself
        (fetchBosses is a 2/s timer), so the opening burst on the boss has
        already been recorded as trash. Move the last few seconds across the
        boundary — measured damage on the boss, miscounted only in which
        column it landed."""
        with self.lock:
            if not self.active or self.phase != 0:
                return
            self.phase = 1
            now = time.time()
            boundary = now
            cutoff = now - backlag
            for ts, ph, kind, ev in self._recent:
                if ph == 0 and ts >= cutoff:
                    self._apply(self._phases[0], kind, ev, -1)
                    self._apply(self._phases[1], kind, ev, 1)
                    boundary = min(boundary, ts)
            # The boundary is where the earliest moved event landed, not where
            # the bar rose — the durations should agree with the totals.
            self._phases[0]["end"] = boundary
            self._phases[1]["start"] = boundary

    def on_boss_kill(self):
        """The kill that ended the fight. Returns the finished report as plain
        data (safe to hand to the Tk thread), or None if nothing was recording.
        One report per rift: taking it stops the recording, so the walk to the
        exit portal can't dribble into the boss column."""
        with self.lock:
            if not self.active:
                return None
            self.active = False
            now = time.time()
            self._phases[self.phase]["end"] = now
            phases = []
            for label, ph in zip(self.PHASE_LABELS, self._phases):
                players = sorted((dict(p) for p in ph["players"].values()),
                                 key=lambda p: -p["total"])
                # The rewind leaves float dust (and a player who only acted in
                # the moved window ends up all-zero) — drop empty rows rather
                # than showing "0" lines.
                players = [p for p in players
                           if p["total"] > 0.5 or p["heal"] > 0.5
                           or p["boost"] > 0.5]
                total = sum(p["total"] for p in players)
                heal = sum(p["heal"] for p in players)
                heal_landed = sum(p["heal_landed"] for p in players)
                boost = sum(p["boost"] for p in players)
                elements = sorted(((el, amt) for el, amt
                                   in ph["elements"].items() if amt > 0.5),
                                  key=lambda kv: -kv[1])
                start, end = ph["start"] or now, ph["end"] or now
                phases.append({"label": label,
                               "duration": max(0.0, end - start),
                               "players": players, "total": total,
                               "heal": heal, "heal_landed": heal_landed,
                               "boost": boost, "elements": elements})
            return {"at": now, "phases": phases}


# The constant every SteamID64 is built on: the individual-account block.
# SteamID64 = STEAM64_BASE + account_id.
STEAM64_BASE = 76561197960265728


def steam64_from_uid(uid):
    """st.Player.uid -> the player's SteamID64, or None if it isn't one.

    The uid arrives as "S" followed by the Steam ACCOUNT ID's bytes in hex —
    but in LITTLE-ENDIAN order, the order they sit in memory, not the order you
    would write the number. So `S1688cc03` is not 0x1688cc03; the bytes are
    16 88 cc 03, which read back as 0x03cc8816 = 63735830.

    That trap is the whole reason this function exists rather than an inline
    int(uid[1:], 16): read it the natural way and you get a wrong number that
    still looks like a plausible account id, so nothing downstream complains —
    it just sends you to a stranger's profile. Measured and calibrated
    2026-08-02 against both steam_get_steam_id() and the Steam registry's
    ActiveUser value; see frida/steamid_probe.js.

    Trailing zero bytes are trimmed by the game, so short uids are normal
    (an old, low-numbered account), not corruption.
    """
    if not uid or not isinstance(uid, str) or uid[0] != "S":
        return None
    h = uid[1:]
    if not h or len(h) > 8:
        return None
    try:
        # An odd length means the leading (most significant) byte lost its zero
        # nibble; pad on the left so the byte boundaries line up again.
        raw = bytes.fromhex(h.zfill(len(h) + (len(h) & 1)))
    except ValueError:
        return None
    return STEAM64_BASE + int.from_bytes(raw, "little")


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
        # The whole-shard roster behind the Social tab: every player the client
        # holds state for, which is a much wider set than `ents` (the minimap
        # sweep is culled to what is near you). Rows are
        # {n, uid, k, lvl, me} exactly as the hook sent them.
        self.shard = []
        # Everyone seen since the meter started, accumulated from the shard
        # roster and never pruned — that is the whole point of it, since the
        # question it answers is "who was that earlier". Keyed by
        # (uid, name) rather than uid alone: one Steam account owns several
        # characters, and collapsing them would silently rename whichever alt
        # you saw first. Level and class are deliberately NOT kept — they are
        # only true while the player is on your layer, and a stale level is
        # worse than no level.
        self.seen = {}

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

    def set_shard(self, rows):
        with self._lock:
            self.shard = list(rows or ())
            # ONE timestamp for the whole batch, not one per row. Everyone
            # currently on the shard then shares an identical `last`, so a
            # recency sort puts them in a single stable block instead of
            # reshuffling them against each other every two seconds.
            now = time.monotonic()
            for r in self.shard:
                name, uid = r.get("n"), r.get("uid")
                if not name:
                    continue
                key = (uid, name)
                e = self.seen.get(key)
                if e is None:
                    self.seen[key] = {"n": name, "uid": uid,
                                      "me": bool(r.get("me")),
                                      "first": now, "last": now}
                else:
                    e["last"] = now

    def seen_players(self):
        """Everyone encountered this session, unordered.

        Copies rather than the stored dicts: the hook thread rewrites `last` on
        every sweep, and handing the UI the live objects would let it read a
        row mid-update. Ordering is the tab's business, as with roster()."""
        with self._lock:
            return [dict(v) for v in self.seen.values()]

    def roster(self):
        """The shard roster, as the hook last sent it.

        Deliberately unordered here: which order it is shown in is the tab's
        business (the user can pick), and sorting in both places is how the two
        end up disagreeing.
        """
        with self._lock:
            return list(self.shard)

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


class MapBackdrop:
    """The world map under the minimap markers.

    Loads assets/maps/<world>.webp plus the transform its builder wrote next
    to it: image_px = (world - origin) * px_per_unit, +y DOWN (the world's +y
    is south — TESTING.md, Geometry). The transform comes from the game's own
    tile grid (576 world units per tile), cross-checked against questlog.gg's
    markers, so there is nothing here to calibrate — only to crop.

    Pillow is optional exactly like it is for the parse screenshots: without
    it (or without the asset) every call returns None and the minimap simply
    keeps its flat panel."""

    SQRT2 = 1.4143          # crop margin so corners survive a rotation

    def __init__(self):
        self._world = None
        self._img = None       # PIL RGB image, or None
        self._meta = None
        self._failed = set()   # worlds not to retry every tick

    @staticmethod
    def available_world(zone_sig):
        """Match the hook's Main.getMapId() sig to a shipped asset. Fuzzy on
        purpose — the sig's exact spelling is the game's business; an asset
        named w1_siagarta answers to any sig that mentions siagarta."""
        if not zone_sig:
            return None
        s = str(zone_sig).lower()
        try:
            candidates = sorted(MAPS_DIR.glob("*.json"))
        except OSError:
            return None
        for p in candidates:
            world = p.stem
            frag = world.split("_", 1)[-1].lower()
            if frag and (frag in s or s in world.lower()):
                return world
        return None

    def _ensure(self, world):
        if world == self._world:
            return self._img is not None
        if world in self._failed:
            return False
        try:
            from PIL import Image
            meta = json.loads((MAPS_DIR / f"{world}.json").read_text())
            img = Image.open(MAPS_DIR / f"{world}.webp").convert("RGB")
        except ImportError:
            print("[meter] map backdrop needs Pillow (pip install pillow) — "
                  "keeping the flat minimap.", file=sys.stderr)
            self._failed.add(world)
            return False
        except Exception as e:
            print(f"[meter] couldn't load map asset {world!r}: {e}",
                  file=sys.stderr)
            self._failed.add(world)
            return False
        self._world, self._img, self._meta = world, img, meta
        print(f"[meter] map backdrop loaded: {world} "
              f"({meta['width']}x{meta['height']})", file=sys.stderr)
        return True

    def crop(self, world, wx, wy, half_units, out_px, heading=None):
        """An out_px-square PIL image of the map centred on world (wx, wy)
        showing ±half_units. `heading` None means fixed mode (north up);
        otherwise the camera azimuth, and the image is turned so that heading
        points up — the same convention as _minimap_px, and PROVEN against it
        by map_bg_check.py rather than trusted from the derivation. Returns
        None when there's nothing to draw (no asset, centre off the map)."""
        if not self._ensure(world):
            return None
        from PIL import Image
        m = self._meta
        s = float(m["px_per_unit"])
        cx = (wx - m["origin_x"]) * s
        cy = (wy - m["origin_y"]) * s
        if not (0 <= cx < m["width"] and 0 <= cy < m["height"]):
            return None          # an instance reusing odd coordinates
        half_px = half_units * s
        r = half_px * (self.SQRT2 if heading is not None else 1.0)
        box = (int(round(cx - r)), int(round(cy - r)),
               int(round(cx + r)), int(round(cy + r)))
        im = self._img.crop(box)   # pads with black past the world's edge
        if heading is not None:
            # PIL rotates content counterclockwise; heading+90deg brings the
            # camera azimuth to the top. See map_bg_check.py for the proof.
            im = im.rotate(math.degrees(heading) + 90.0,
                           resample=Image.BILINEAR)
            c = im.size[0] / 2.0
            hp = int(round(half_px))
            im = im.crop((int(round(c)) - hp, int(round(c)) - hp,
                          int(round(c)) + hp, int(round(c)) + hp))
        return im.resize((out_px, out_px), Image.BILINEAR)


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
        self._unlock_at = None         # when the game's escape menu opened
        self._boss_bar = 0
        self._zone_sig = None
        self._mounts: list[str] = []   # unlocked mount kinds, from the hook
        self._gliders: list[str] = []  # unlocked glider kinds, same shape

    def set_mounts(self, kinds):
        with self._lock:
            self._mounts = list(kinds)

    def mounts(self):
        with self._lock:
            return list(self._mounts)

    def set_gliders(self, kinds):
        with self._lock:
            self._gliders = list(kinds)

    def gliders(self):
        with self._lock:
            return list(self._gliders)

    def set_zone(self, sig):
        """layer.world.level from the hook — the loaded level's name, sent
        once at attach and then on every change. (Its predecessor,
        Main.getMapId(), turned out to return the machine hostname.)"""
        with self._lock:
            self._zone_sig = sig or None

    def zone_sig(self):
        with self._lock:
            return self._zone_sig

    def set_rift(self, state: bool):
        with self._lock:
            self._rift = bool(state)

    def in_rift(self) -> bool:
        with self._lock:
            return self._rift

    def set_boss_bar(self, count: int):
        """How many of the game's own boss/elite healthbars are on screen.

        The hook reads ui.hud.BossesInfo, so this counts bars the player can
        actually see — it goes to zero when they walk away and the boss resets,
        not just when something dies."""
        with self._lock:
            self._boss_bar = max(0, int(count))

    def boss_bar_up(self) -> bool:
        with self._lock:
            return self._boss_bar > 0

    def set_window(self, name: str, is_open: bool):
        if not name:
            return
        with self._lock:
            if is_open:
                self._open.add(name)
                # Stamped so the overlay can report how long IT took to react
                # to the game opening its menu. The hook side is an interceptor
                # on the window itself, so this is the moment the game did it;
                # anything after is ours.
                if name in UNLOCK_ON_WINDOWS:
                    self._unlock_at = time.monotonic()
            else:
                self._open.discard(name)

    def take_unlock_stamp(self):
        """The pending open-stamp, consumed. None if already reported."""
        with self._lock:
            t, self._unlock_at = self._unlock_at, None
            return t

    def clear(self):
        with self._lock:
            self._open.clear()
            # A loading screen tears the HUD down with it, so a bar that was up
            # on the way out must not leave the compass hidden in the new zone.
            self._boss_bar = 0

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


def _set_clickthrough(hwnd, enabled, activatable=False):
    """Set one window's click-through, and whether it may hold keyboard focus.

    `activatable` drops WS_EX_NOACTIVATE. Only the control menu asks for it,
    and only because it carries the Social tab's search boxes: a window that
    never activates never receives a keystroke, so a tk.Entry on one is inert
    no matter what is bound to it — the same wall _begin_bind_capture works
    around by polling the keyboard instead of binding to it."""
    if sys.platform != "win32":
        return
    u = ctypes.windll.user32
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        get, setl = u.GetWindowLongPtrW, u.SetWindowLongPtrW
    else:
        get, setl = u.GetWindowLongW, u.SetWindowLongW
    ex = get(hwnd, GWL_EXSTYLE) | WS_EX_LAYERED
    if activatable:
        ex &= ~WS_EX_NOACTIVATE
    else:
        ex |= WS_EX_NOACTIVATE
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
VERSION = "3.3.3"

REPO = "brudrbear/FareverMeter"
REPO_URL = f"https://github.com/{REPO}"
UPDATE_API_RELEASE = f"https://api.github.com/repos/{REPO}/releases/latest"
UPDATE_API_TAGS = f"https://api.github.com/repos/{REPO}/tags"
UPDATE_TIMEOUT = 5.0
# Filled in by the checker thread, read by the overlay's refresh tick.
# `asset` is the Setup.exe download URL when the release has one — that's what
# lets the notice self-update instead of just opening the browser.
# "prompt" is set ONLY by the automatic checks (startup and loading screens),
# never by the menu's manual button: a click already reports its own answer on
# the button, and popping a dialog at someone who just asked the question is
# telling them what they already know. The overlay consumes the flag.
UPDATE = {"latest": None, "url": REPO_URL, "asset": None, "asset_size": 0,
          "prompt": False}

# The self-updater's working directory: the downloaded installer and the
# helper script that runs it after the meter exits. Under DATA_HOME so an
# update never needs to write into the install directory it's replacing.
UPDATE_DIR = DATA_HOME / "updates"
# Left by the outgoing build, read and deleted by the one that replaces it —
# see _show_whats_new. Lives beside the installer rather than with the settings
# because it belongs to the update, not to the user's preferences.
UPDATED_MARKER = UPDATE_DIR / "just_updated.json"
UPDATE_API_RELEASE_TAG = f"https://api.github.com/repos/{REPO}/releases/tags/"

# True in the shipped build (PyInstaller sets it). Only that build can
# self-update: a from-source run has no installed copy to replace.
IS_FROZEN = bool(getattr(sys, "frozen", False))

QUIT_LABEL = "Stop the meter"

# The top line of the control menu. It used to shout about Ctrl+C, from when
# the only way to run the meter was a console you could close out from under
# it. The shipped build has no console and two proper exits, so the line just
# says where they are — and doubles as the slot the update notice takes over.
SHUTDOWN_HINT = ("Stop the meter with the button at the bottom, or from the "
                 "Farever+ tray icon by the clock.")


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

# ---- cue sounds ----
# All three ride the single "Enable sounds" setting; there is no per-cue switch.
SOUND_FILES = {
    "pull": ROOT / "assets" / "boss_pulled.wav",
    "victory": ROOT / "assets" / "boss_victory.mp3",
    "legendary": ROOT / "assets" / "legendary_pickup.mp3",
}
SOUND_VOLUME_DEFAULT = 60         # percent
SOUND_VOLUME_MAX = 100

# st.item.Weapon.rarity, read live off equipped gear. Capitalised, and one of
# a small set — Legendary / Epic / Rare were all observed. Compared exactly:
# a case-insensitive match would hide the day the game renames it.
LEGENDARY_RARITY = "Legendary"


class SoundPlayer:
    """Plays the boss-fight cues through Windows' MCI, via ctypes.

    MCI rather than winsound because winsound is WAV-only and has no volume
    control at all, and rather than a bundled audio library because this ships
    as a PyInstaller build where every dependency is megabytes the user
    downloads. Both files are opened with `type mpegvideo`: measured, that
    driver handles the .wav and the .mp3 alike AND honours `setaudio volume`,
    which the waveaudio driver does not.

    EVERY MCI call happens on this class's own worker thread, and that is not
    tidiness — it's required. MCI ties a device's lifetime to the thread that
    opened it. Opening the files on a short-lived helper thread and playing
    them from another looks fine (the open returns success) and is then
    silent: every later command fails with "the specified device is not open".
    Measured, and the reason this class owns a thread instead of a lock.

    Commands are queued, so callers never block: MCI `play` is asynchronous
    anyway, and the Tk thread must not wait on audio. Files are opened once and
    kept open, so a cue starts when it's asked for rather than after a disk
    read.
    """

    def __init__(self):
        self._lock = threading.Lock()         # guards _enabled/_volume/_broken
        self._enabled = False
        self._volume = SOUND_VOLUME_DEFAULT
        self._broken = False                  # give up quietly after a failure
        self._q: queue.Queue = queue.Queue()
        self._thread = None
        # Worker-thread-only state; no lock needed, nothing else touches it.
        self._open: dict[str, str] = {}       # key -> MCI alias
        self._checked = False                 # verified a cue actually played
        try:
            self._mci = ctypes.windll.winmm.mciSendStringW
        except Exception:
            self._mci = None
            self._broken = True

    # ---- worker thread ----

    def _send(self, cmd: str) -> str | None:
        """Returns MCI's reply on success (often ""), or None on failure."""
        if self._mci is None:
            return None
        try:
            buf = ctypes.create_unicode_buffer(256)
            if self._mci(cmd, buf, 256, None) != 0:
                return None
            return buf.value
        except Exception:
            return None

    def _alias_for(self, key: str) -> str | None:
        if key in self._open:
            return self._open[key]
        path = SOUND_FILES.get(key)
        if path is None or not path.is_file():
            return None
        alias = f"fmsnd_{key}_{os.getpid()}"
        if self._send(f'open "{path}" type mpegvideo alias {alias}') is None:
            return None
        self._open[key] = alias
        with self._lock:
            vol = self._volume
        self._send(f"setaudio {alias} volume to {vol * 10}")
        return alias

    def _do_play(self, key: str):
        alias = self._alias_for(key)
        if alias is None:
            with self._lock:
                self._broken = True       # missing/unplayable: stop retrying
            print(f"[meter] sound {key!r} unavailable; sounds disabled",
                  file=sys.stderr)
            return
        # `from 0` so a second pull retriggers the cue instead of being ignored
        # because the previous play hasn't finished.
        self._send(f"stop {alias}")
        if self._send(f"play {alias} from 0") is None:
            print(f"[meter] sound {key!r} failed to play", file=sys.stderr)
            return
        # Once per session, confirm the device really is producing audio rather
        # than accepting commands into the void — that failure mode has already
        # happened once here, and it is completely silent without this.
        if not self._checked:
            self._checked = True
            mode = self._send(f"status {alias} mode")
            if mode is not None and mode.strip() and mode.strip() != "playing":
                print(f"[meter] sound device reports {mode.strip()!r} rather "
                      "than 'playing' — cues may be silent", file=sys.stderr)

    def _run(self):
        while True:
            job = self._q.get()
            try:
                op = job[0]
                if op == "quit":
                    for alias in self._open.values():
                        self._send(f"stop {alias}")
                        self._send(f"close {alias}")
                    self._open.clear()
                    return
                if op == "prime":
                    for key in SOUND_FILES:
                        self._alias_for(key)
                elif op == "volume":
                    for alias in self._open.values():
                        self._send(f"setaudio {alias} volume to {job[1] * 10}")
                elif op == "play":
                    self._do_play(job[1])
            except Exception:
                pass          # a bad cue must never take the meter with it

    # ---- public API, callable from any thread ----

    def start(self):
        """Spin the worker up and open both files on it. Must be called before
        anything will play — the worker is the only thread MCI will accept
        commands from for these devices."""
        with self._lock:
            if self._mci is None or self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="farever-meter-sound")
            self._thread.start()
        self._q.put(("prime",))

    def set_enabled(self, on: bool):
        with self._lock:
            self._enabled = bool(on)

    def set_volume(self, pct: int):
        with self._lock:
            self._volume = max(0, min(SOUND_VOLUME_MAX, int(pct)))
            vol = self._volume
        self._q.put(("volume", vol))

    def play(self, key: str):
        with self._lock:
            if not self._enabled or self._broken or not self._volume:
                return
        self._q.put(("play", key))

    def close(self):
        with self._lock:
            if self._thread is None:
                return
            thread = self._thread
        self._q.put(("quit",))
        thread.join(timeout=2.0)


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
        # A search box on the control menu has keyboard focus, so the game is
        # not seeing keystrokes right now. Everything that would otherwise
        # shove focus back at the game defers to this — see _refocus_game.
        self._typing = False
        self._hide_ooc = False         # "hide out of combat" setting
        self._social_sort = "name"     # Social roster order; see SOCIAL_SORTS
        self._best_times = self._load_best_times()   # fastest boss kills, secs by kind
        self._session_sort = "recent"  # session log order; see SESSION_SORTS
        # _show is what the player asked for, _shown is what's actually mapped
        # (they differ while out-of-combat hiding is in effect).
        self._show = {k: ELEMENT_SHOW for k, _ in TOGGLEABLE_ELEMENTS}
        # Healing is columns inside the meter, not a window of its own, so it
        # stays a plain on/off rather than gaining a mode it can't honour.
        self._show_heal = True
        self._sort_heal = False        # rows ordered by healing, not damage
        # The BOOST column has no setting: it appears when the encounter
        # actually contains boosted damage and goes away again on reset. It is
        # a patch quirk, not a feature — nobody should have to know a toggle
        # exists to understand a number that only one weapon in the game can
        # produce, and nobody else should be looking at an empty column.
        self._show_boost = False
        self._mount_random = False     # random favorite mount on each summon
        self._mount_mode = "Random"    # how the pick is made (MOUNT_MODES)
        self._mount_favs: set[str] = set()   # mount kinds the swap may pick
        self._mounts_shown = None      # unlock list the checkboxes were built from
        self._glider_random = False    # re-equip a random favorite per glide
        self._glider_mode = "Random"   # how the pick is made (MOUNT_MODES)
        self._glider_favs: set[str] = set()  # glider kinds the equip may pick
        self._gliders_shown = None     # unlock list the checkboxes were built from
        self._shown = {k: True for k, _ in TOGGLEABLE_ELEMENTS}
        # (healing, boost) as last pushed to the widgets — see _apply_heal_columns
        self._cols_shown = (True, False)
        self._combat_seen_at = 0.0     # last moment a tracked player was fighting
        self._header_bg = BG_HEADER    # last tint pushed to the header bars
        self._theme = THEME_DEFAULT    # what's painted right now
        self._theme_mode = THEME_MODE_DEFAULT   # what the player asked for
        self._action_q = []
        self._q_lock = threading.Lock()
        self._quit_armed = False       # the Quit button's second-click window
        self._update_shown = False     # the update notice is applied once
        self._update_offer_open = False   # the offer popup is on screen
        self._update_offer_done = False   # ...and has been answered, once ever
        self._updating = False         # self-update running: overlay hidden
        self._upd_checking = False     # manual check in flight (button armed)
        self._upd_resolving = False    # re-asking GitHub for the installer
        self._upd_btn_after = None     # pending after() resetting the button
        self._dl = None                # download progress, written off-thread
        self._updwin = None            # the "Updating ..." progress window
        self._game_exit_win = None     # the "Farever has stopped" prompt
        self._game_gone = False        # the game process died; hide everything
        self._whats_new_win = None     # the post-update release notes
        self._map_mode = MINIMAP_MODES[0]   # "Rotating" — you always face up
        self._transparency = 0              # percent, on top of OVERLAY_ALPHA
        # Per-category ticks for each panel; everything on until told otherwise.
        self._map_filters = {key: True for key, _label, _cats in MINIMAP_FILTERS}
        self._compass_filters = {key: True
                                 for key, _label, _cats in COMPASS_FILTERS}
        self._map_rate = "High"        # Ultra exists but is opt-in
        # The world-map backdrop. On by default: it only ever draws when a
        # shipped asset matches the zone, so "on with no asset" costs nothing.
        self._map_bg_on = True
        # Both percentages, both defaulting to "as it shipped". Zoom drives
        # _map_range (see _apply_map_zoom); icons multiply the style table's
        # radii without touching their ratios.
        self._map_zoom = 100
        self._map_icons = 100
        self._map_backdrop = MapBackdrop()
        self._map_photo = None         # the reused PhotoImage, sized lazily
        self._map_bg_key = None        # (zone sig) -> resolved world, cached
        self._map_bg_world = None
        # Every cue — boss pull, boss kill, legendary drop — rides this one
        # setting. Off by default: an overlay that starts making noise on its
        # own the first time you meet a boss is a bad first impression, and the
        # checkbox plays a sample the moment you turn it on.
        self._sounds_on = False
        self._sound_volume = SOUND_VOLUME_DEFAULT
        self._auto_reset_boss = False
        self.sounds = SoundPlayer()
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
        # Standing answer to both halves of that prompt: switch to all-players
        # on the way into a rift and back to party-only on the way out, without
        # being asked. One setting rather than two, because the pair only makes
        # sense together — auto-switching in and then leaving you on all-players
        # in the open world is the state the leave prompt exists to prevent.
        self._rift_auto_view = False
        self._rift_pulse = False
        self._pulse_job = None
        self._rift_box = None      # which palette the box is wearing
        self._rift_seen = False
        # End-of-rift report: the frozen data the card is showing, and the
        # "Copied" flash timer. Seeded from the newest saved report so 'Last
        # Rift Report' works across sessions — last night's rift is still
        # there this morning.
        self._report_open = False
        self._report_data = self._load_last_rift_report()
        self._report_flash_job = None

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
        self.reportwin = tk.Toplevel(self.root)
        self.reportwin.title("Farever+ Rift Report")
        self.updatewin = tk.Toplevel(self.root)
        self.updatewin.title("Farever+ Update")
        self.riftwin = tk.Toplevel(self.root)
        self.riftwin.title("Farever+ Rift Timer")
        self.mapwin = tk.Toplevel(self.root)
        self.mapwin.title("Farever+ Minimap")
        self.compasswin = tk.Toplevel(self.root)
        self.compasswin.title("Farever+ Compass")
        # The compass's badge underlay — the translucent boxes behind the
        # distance numbers live here, one window down. See _build_compass.
        self.badgewin = tk.Toplevel(self.root)
        self.badgewin.title("Farever+ Compass Badges")
        self.killwin = tk.Toplevel(self.root)
        self.killwin.title("Farever+ Kill Time")
        for win in (self.root, self.detail, self.menu, self.hintwin,
                    self.parsewin, self.promptwin, self.reportwin,
                    self.updatewin, self.riftwin, self.mapwin,
                    self.compasswin, self.badgewin, self.killwin):
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
        # Not the badge underlay: its opacity is never its own — always the
        # compass's times COMPASS_DIST_BOX_ALPHA, applied by _sync_badgewin.
        # Invisible until the first sync so it can't flash solid black.
        self.badgewin.attributes("-alpha", 0.0)
        # Keys must match TOGGLEABLE_ELEMENTS.
        self._element_win = {"meter": self.root, "detail": self.detail,
                             "rift": self.riftwin, "minimap": self.mapwin,
                             "compass": self.compasswin}
        # Every window that fades: the two toggleable ones, plus the control
        # menu and its hint, which follow the game's escape menu.
        self._fade_win = dict(self._element_win, menu=self.menu,
                              hint=self.hintwin, prompt=self.promptwin,
                              report=self.reportwin, update=self.updatewin)
        self._shown["menu"] = self._shown["hint"] = False
        self._shown["prompt"] = False
        self._shown["report"] = False
        self._shown["update"] = False
        self._shown["rift"] = False     # nothing to show until a timer arrives
        # Live opacity of each faded window, driven by _step_fade. The menu pair
        # starts at zero: they're withdrawn until the escape menu opens.
        # Seeded from the saved slider, not from the constant: a restart should
        # come up wearing the transparency you left it on, without a visible
        # settle from full opacity.
        self._alpha = {k: self._alpha_for(k) for k in self._fade_win}
        self._alpha["menu"] = self._alpha["hint"] = 0.0
        self._alpha["prompt"] = self._alpha["rift"] = 0.0
        self._alpha["report"] = self._alpha["update"] = 0.0
        for key, win in self._fade_win.items():
            if self._alpha[key]:
                win.attributes("-alpha", self._alpha[key])
        self._fade_secs = {k: FADE_SECS for k in self._fade_win}
        self._fade_secs["menu"] = self._fade_secs["hint"] = MENU_FADE_SECS
        self._fade_secs["prompt"] = self._fade_secs["report"] = PANEL_FADE_SECS
        self._fade_secs["update"] = PANEL_FADE_SECS
        self._fade_job = None          # pending `after` id for the fade driver

        self._build_meter()
        self._build_detail()
        self._build_menu()
        self._build_hint()
        self._build_parse()
        self._build_kill_toast()
        self._build_prompt()
        self._build_report()
        self._build_update_offer()
        self._build_rift()
        self._build_minimap()
        self._build_compass()
        self.root.update_idletasks()
        self._place_windows(pos)
        # Restored scales can only be applied now: they resize the fonts every
        # window has already been packed against. The sliders are set from them
        # too, or the menu would read 100% while the window is at 125.
        # Defaults underneath, saved values on top — a group nobody has ever
        # touched starts at its SCALE_DEFAULTS entry, and a saved 100 stays a
        # saved 100 rather than being "upgraded" to the default.
        restored = dict(SCALE_DEFAULTS)
        restored.update(self._pending_scales or {})
        for group, factor in restored.items():
            if group in self._scales and abs(factor - 1.0) > 0.001:
                self._scale_vars[group].set(int(round(factor * 100)))
                self._set_group_scale(group, factor)
        self._pending_scales = None
        # Push the restored audio settings into the player, then start its
        # worker — the files are opened there, off this thread, because MCI
        # `open` touches the disk and would otherwise sit in front of the first
        # frame the overlay draws.
        self.sounds.set_enabled(self._sounds_on)
        self.sounds.set_volume(self._sound_volume)
        self.sounds.start()
        # The control menu and its hint only exist while the game's escape menu
        # is up; _sync_game_ui maps them in. The parse banner is mapped by parse
        # mode itself, and deliberately answers to nothing else — a countdown
        # you can't see is worse than useless.
        # Not a faded window — parse mode maps and unmaps it itself.
        self.parsewin.withdraw()
        # Nor this one: the kill-time toast maps itself when a boss dies.
        self.killwin.withdraw()
        # DERIVED from _shown rather than hand-listed, because the hand-listed
        # version had exactly one failure mode and it happened: add a faded
        # window, forget to add it here, and it starts MAPPED. A Toplevel left
        # at alpha 0 is not hidden — it is still topmost and still clickable,
        # so it paints whatever its widgets hold and eats clicks meant for the
        # game. The update offer shipped that way for one build: it sat on
        # screen as a bare header and a "Later" button, and clicking that
        # button appeared to do nothing, because _want_visible saw the target
        # it was already set to and returned without withdrawing anything.
        # This loop cannot be forgotten.
        for key, win in self._fade_win.items():
            if not self._shown[key]:
                win.withdraw()
        # The badge underlay isn't a faded window — it shadows the compass,
        # and this first sync is where it picks up the compass's real state.
        self._sync_badgewin()
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
        if isinstance(data.get("map_bg"), bool):
            self._map_bg_on = data["map_bg"]
        z = data.get("map_zoom")
        if isinstance(z, int) and MINIMAP_ZOOM_MIN <= z <= MINIMAP_ZOOM_MAX:
            self._map_zoom = z
        ic = data.get("map_icons")
        if isinstance(ic, int) and MINIMAP_ICONS_MIN <= ic <= MINIMAP_ICONS_MAX:
            self._map_icons = ic
        if isinstance(data.get("sounds_on"), bool):
            self._sounds_on = data["sounds_on"]
        if isinstance(data.get("auto_reset_boss"), bool):
            self._auto_reset_boss = data["auto_reset_boss"]
        if isinstance(data.get("rift_auto_view"), bool):
            self._rift_auto_view = data["rift_auto_view"]
        if data.get("social_sort") in SOCIAL_SORTS:
            self._social_sort = data["social_sort"]
        if data.get("session_sort") in SESSION_SORTS:
            self._session_sort = data["session_sort"]
        vol = data.get("sound_volume")
        if isinstance(vol, int) and 0 <= vol <= SOUND_VOLUME_MAX:
            self._sound_volume = vol
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
        # Heal sort can't outlive the column it sorts by, so a saved True is
        # only honoured while the healing columns are on (the toggles keep
        # that invariant; this covers a hand-edited file).
        if isinstance(data.get("sort_heal"), bool):
            self._sort_heal = data["sort_heal"] and self._show_heal
        if isinstance(data.get("mount_random"), bool):
            self._mount_random = data["mount_random"]
        if data.get("mount_mode") in MOUNT_MODES:
            self._mount_mode = data["mount_mode"]
        favs = data.get("mount_favorites")
        if isinstance(favs, list):
            self._mount_favs = {k for k in favs if isinstance(k, str)}
        if isinstance(data.get("glider_random"), bool):
            self._glider_random = data["glider_random"]
        if data.get("glider_mode") in MOUNT_MODES:
            self._glider_mode = data["glider_mode"]
        gfavs = data.get("glider_favorites")
        if isinstance(gfavs, list):
            self._glider_favs = {k for k in gfavs if isinstance(k, str)}
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
                "map_bg": bool(self._map_bg_on),
                "map_zoom": int(self._map_zoom),
                "map_icons": int(self._map_icons),
                "sounds_on": bool(self._sounds_on),
                "sound_volume": int(self._sound_volume),
                "auto_reset_boss": bool(self._auto_reset_boss),
                "rift_auto_view": bool(self._rift_auto_view),
                "scales": {g: round(self._scales[g], 3)
                           for g, _label in SCALE_GROUPS},
                "show": {k: self._show.get(k, ELEMENT_SHOW)
                         for k, _label in TOGGLEABLE_ELEMENTS},
                "show_heal": bool(self._show_heal),
                "sort_heal": bool(self._sort_heal),
                "mount_random": bool(self._mount_random),
                "mount_mode": self._mount_mode,
                "mount_favorites": sorted(self._mount_favs),
                "glider_random": bool(self._glider_random),
                "glider_mode": self._glider_mode,
                "glider_favorites": sorted(self._glider_favs),
                "social_sort": self._social_sort,
                "session_sort": self._session_sort,
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
        # Flips the row order between the two columns. The label is the
        # STATE, not the destination — "▼ Damage" while damage-sorted, like
        # a sorted column header — after the destination reading shipped
        # first and read as a lie about what the rows already showed.
        # Outside the drag binding below on purpose (the header drags, the
        # button clicks), and only on screen while the healing columns are:
        # sorting by a column that isn't drawn would order the rows by
        # invisible numbers.
        self.sort_btn = tk.Button(self.header, text=self._sort_btn_text(),
                                  command=self._enqueue(self._toggle_sort),
                                  bg=BG_HEADER, fg=FG_HEADER,
                                  activebackground=BG_HEADER,
                                  activeforeground=FG_HEADER,
                                  font=self.fonts["ui_sm_b"],
                                  relief="flat", bd=0, padx=6, pady=0,
                                  cursor="hand2", highlightthickness=0)
        if self._show_heal:
            self.sort_btn.pack(side="right")
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
        # BOOST sits next to the damage numbers it was taken out of, so the
        # two read together — and only while something is producing it.
        if self._show_boost:
            head += f"{'BOOST':>9}"
        # OVER% rides with the healing columns because it is a share OF them:
        # on its own, next to a damage table, it would be a percentage of a
        # number that isn't on screen.
        return head + (f"{'HEAL':>9}{'OVER':>6}" if self._show_heal else "")

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
        # Which build you're actually running, where you'd look for it. Dimmer
        # than the title: it answers a question rather than asking for
        # attention, and it's the first thing worth knowing when something
        # behaves differently from what the notes describe. Draggable along
        # with the rest of the header — a strip you can't grab is a strip that
        # feels broken.
        self.m_version = tk.Label(self.m_header, text=f"v{VERSION}",
                                  bg=BG_HEADER_UNLOCKED, fg=FG_HEADER,
                                  font=self.fonts_m["ui_tiny_i"], anchor="e",
                                  padx=8, pady=4)
        self.m_version.pack(side="right")
        # The same link the Actions tab carries, up where the eye goes for
        # "what is this thing" — beside the version. A real button, and kept
        # OUT of the drag binding below on purpose: the header drags, the
        # button clicks, and no widget does both.
        self.m_repo = tk.Button(self.m_header, text="GitHub",
                                command=self._enqueue(self._open_repo),
                                bg=BG_HEADER_UNLOCKED, fg=FG_HEADER,
                                activebackground=BG_HEADER,
                                activeforeground=FG_HEADER,
                                font=self.fonts_m["ui_tiny_i"],
                                relief="flat", bd=0, padx=6, pady=0,
                                cursor="hand2", highlightthickness=0)
        self.m_repo.pack(side="right", pady=4)
        # Its neighbour: ask GitHub for a newer build right now, answered on
        # the button itself. The automatic check already runs at startup and
        # on loading screens, but silently — this is for "did my update
        # land?" and "am I current?", asked deliberately. It shares the
        # automatic check's plumbing but not its 15-minute throttle: a click
        # is a question, and a question deserves a fresh answer.
        self.m_update = tk.Button(self.m_header, text="Check updates",
                                  command=self._enqueue(
                                      self._check_updates_clicked),
                                  bg=BG_HEADER_UNLOCKED, fg=FG_HEADER,
                                  activebackground=BG_HEADER,
                                  activeforeground=FG_HEADER,
                                  font=self.fonts_m["ui_tiny_i"],
                                  relief="flat", bd=0, padx=6, pady=0,
                                  cursor="hand2", highlightthickness=0)
        self.m_update.pack(side="right", pady=4)
        self._bind_drag(self.menu,
                        (self.m_header, self.m_title, self.m_version))

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
        self.warn_lbl.pack(fill="x", pady=(0, 6))

        # Tabs rather than the old two-column wall. The menu had grown a row at
        # a time until every visit meant reading all of it; four pages mean the
        # page you're on is the only thing asking to be read. All four frames
        # sit stacked in ONE grid cell and the active one is raised, so the
        # holder takes the size of the largest page and the window never
        # changes size when you switch — a settings panel that jumps around
        # under the cursor is worse than a dense one.
        tabs_wrap = tk.Frame(body, bg=BG_BODY)
        tabs_wrap.pack(fill="both", expand=True)
        # The navbar is a fixed-width column with propagation off, so a long
        # tab name widens the label and not the strip — otherwise adding one
        # verbose tab would shove every page sideways.
        navbar = self.menu_nav = tk.Frame(
            tabs_wrap, bg=BG_BODY,
            width=int(MENU_NAV_W * self._scales["menu"]))
        navbar.pack(side="left", fill="y")
        navbar.pack_propagate(False)
        tk.Frame(tabs_wrap, bg=BG_BAR_TRACK, width=1).pack(
            side="left", fill="y", padx=(8, 0))
        holder = tk.Frame(tabs_wrap, bg=BG_BODY)
        holder.pack(side="left", fill="both", expand=True, padx=(10, 0))
        holder.grid_rowconfigure(0, weight=1)
        holder.grid_columnconfigure(0, weight=1)
        self._menu_tab = "General"
        self._menu_tab_btns = {}
        self._menu_tab_frames = {}
        # General stays the landing page — it is what you open the menu for
        # most of the time. Social sits directly under it because it is the
        # other page you open to READ rather than to change something; the
        # configuration pages follow.
        for name in ("General", "Social", "Actions", "Windows", "Map",
                     "Mounts", "Gliders"):
            b = tk.Button(navbar, text=name, anchor="w",
                          command=self._enqueue(
                              lambda n=name: self._set_menu_tab(n)),
                          font=self.fonts_m["ui_b"], relief="flat", bd=0,
                          padx=12, pady=6, cursor="hand2",
                          highlightthickness=1)
            b.pack(fill="x", pady=(0, 3))
            self._menu_tab_btns[name] = b
            f = tk.Frame(holder, bg=BG_BODY)
            f.grid(row=0, column=0, sticky="nsew")
            self._menu_tab_frames[name] = f
        soc = self._menu_tab_frames["Social"]
        gen = self._menu_tab_frames["General"]
        winb = self._menu_tab_frames["Windows"]
        mp = self._menu_tab_frames["Map"]
        mnt = self._menu_tab_frames["Mounts"]
        gld = self._menu_tab_frames["Gliders"]
        act = self._menu_tab_frames["Actions"]

        def section(parent, text, first=False):
            row = tk.Frame(parent, bg=BG_BODY)
            row.pack(fill="x", pady=(2 if first else 10, 4))
            tk.Label(row, text=text, bg=BG_BODY, fg=ACCENT,
                     font=self.fonts_m["ui_sm_b"], anchor="w").pack(side="left")
            # The rule line carries the heading across the row, which is what
            # lets the headings be quiet: the eye finds the break, not the word.
            tk.Frame(row, bg=BG_BAR_TRACK, height=1).pack(
                side="left", fill="x", expand=True, padx=(8, 0))

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

        # One styling for every dropdown and every slider, in one place each —
        # the old menu configured five OptionMenus by hand, identically, and
        # they only stayed identical by luck.
        def dropdown(row, var, values, command, width=None):
            opt = tk.OptionMenu(row, var, *values, command=command)
            opt.config(
                bg=BG_BODY_SOFT, fg=FG_TEXT, activebackground=BG_BAR_TRACK,
                activeforeground=FG_VALUE, relief="flat", bd=0, anchor="w",
                padx=10, pady=3, font=self.fonts_m["ui"], cursor="hand2",
                highlightthickness=1, highlightbackground=BG_BAR_TRACK,
                direction="right")
            opt["menu"].config(
                bg=BG_BODY, fg=FG_TEXT, activebackground=BTN_ON_BG,
                activeforeground=FG_HEADER, bd=0, relief="flat",
                font=self.fonts_m["ui"])
            if width is not None:
                opt.config(width=width)
            self._option_menus.append(opt)
            return opt

        def slider(row, var, lo, hi, on_release, length=120):
            scl = tk.Scale(
                row, from_=lo, to=hi, resolution=5, orient="horizontal",
                variable=var, showvalue=True, bg=BG_BODY, fg=FG_DIM,
                troughcolor=BG_BAR_TRACK, activebackground=BTN_ON_BG,
                highlightthickness=0, bd=0, sliderrelief="flat",
                font=self.fonts_m["ui_tiny_i"], length=length, cursor="hand2")
            scl.bind("<ButtonRelease-1>", on_release)
            return scl

        # ---- Social: two views of the same people ----
        # "Current Shard" is st.GameLayer.players — everyone the client holds
        # state for right now, which is far more than the people rendered
        # around you, and carries a class and level because their entities are
        # live. "This session" is the accumulated log of everyone seen since
        # the meter started, and deliberately carries NEITHER: those two facts
        # are only true while the player is on your layer, and showing a level
        # from twenty minutes ago would be worse than showing none.
        #
        # Sub-tabs run horizontally here precisely because the main navigation
        # is vertical — the change of axis is what makes them read as a second
        # level rather than as more of the same list.
        subbar = tk.Frame(soc, bg=BG_BODY)
        subbar.pack(fill="x", pady=(2, 8))
        sub_holder = tk.Frame(soc, bg=BG_BODY)
        sub_holder.pack(fill="both", expand=True)
        sub_holder.grid_rowconfigure(0, weight=1)
        sub_holder.grid_columnconfigure(0, weight=1)
        self._social_page = "shard"
        self._social_page_btns = {}
        self._social_page_frames = {}
        for key, label in SOCIAL_PAGES:
            b = tk.Button(subbar, text=label,
                          command=self._enqueue(
                              lambda k=key: self._set_social_page(k)),
                          font=self.fonts_m["ui_b"], relief="flat", bd=0,
                          padx=14, pady=3, cursor="hand2",
                          highlightthickness=1)
            b.pack(side="left", padx=(0, 4))
            self._social_page_btns[key] = b
            f = tk.Frame(sub_holder, bg=BG_BODY)
            f.grid(row=0, column=0, sticky="nsew")
            self._social_page_frames[key] = f
        # Top right, on the sub-tab row so it reads as belonging to the whole
        # tab: it reloads BOTH pages. The button exists because the pages
        # don't poll — see _reload_social.
        self.btn_social_refresh = tk.Button(
            subbar, text="Refresh",
            command=self._enqueue(self._refresh_social_clicked),
            font=self.fonts_m["ui"], bg=BG_BODY_SOFT, fg=FG_TEXT,
            activebackground=BG_BAR_TRACK, activeforeground=FG_VALUE,
            relief="flat", bd=0, padx=10, pady=2, highlightthickness=1,
            highlightbackground=BG_BAR_TRACK, cursor="hand2")
        self.btn_social_refresh.pack(side="right")

        def search_row(parent, var):
            """The search line shared by both pages. Returns the row (so a
            caller can add its own controls) and the count label."""
            row = tk.Frame(parent, bg=BG_BODY)
            row.pack(fill="x", pady=(0, 6))
            tk.Label(row, text="Search", bg=BG_BODY, fg=FG_TEXT,
                     font=self.fonts_m["ui"], anchor="w").pack(side="left")
            ent = tk.Entry(
                row, textvariable=var, font=self.fonts_m["ui"],
                bg=BG_BODY_SOFT, fg=FG_TEXT, insertbackground=FG_VALUE,
                relief="flat", bd=0, highlightthickness=1,
                highlightbackground=BG_BAR_TRACK, highlightcolor=ACCENT)
            ent.pack(side="left", fill="x", expand=True, padx=(8, 8))
            # focus_FORCE, not focus_set: the menu is an overrideredirect
            # window, so Tk does not believe it holds the focus and focus_set
            # alone leaves the caret dead even now that the window can activate.
            ent.bind("<Button-1>", lambda _e, w=ent: w.focus_force())
            ent.bind("<FocusIn>", lambda _e: self._set_typing(True))
            ent.bind("<FocusOut>", lambda _e: self._set_typing(False))
            # Esc and Return both mean "done typing". Esc matters most: while
            # this box holds the keyboard, the game cannot see its own Escape,
            # so the first press leaves the box and the second reaches the game
            # and closes its menu. Swallowed ("break") so the first press isn't
            # also read as something else on the way past.
            for seq in ("<Escape>", "<Return>", "<KP_Enter>"):
                ent.bind(seq, lambda _e: (self._stop_typing(), "break")[1])
            count = tk.Label(row, text="", bg=BG_BODY, fg=FG_DIM,
                             font=self.fonts_m["ui_tiny_i"], anchor="e")
            count.pack(side="right")
            return row, count

        def scroll_list(parent):
            """A scrolling viewport of real widgets.

            Canvas + inner frame is the only way tk gives you a scrollable
            stack of widgets, and each row carries buttons so a Listbox is out.
            Written once and used by both pages — two hand-built copies of this
            plumbing is two places for the scrollregion to go stale."""
            wrap = tk.Frame(parent, bg=BG_BODY)
            wrap.pack(fill="both", expand=True)
            canvas = tk.Canvas(
                wrap, bg=BG_BODY, highlightthickness=0, bd=0,
                height=int(SOCIAL_LIST_H * self._scales["menu"]))
            vsb = tk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
            canvas.configure(yscrollcommand=vsb.set)
            vsb.pack(side="right", fill="y")
            canvas.pack(side="left", fill="both", expand=True)
            inner = tk.Frame(canvas, bg=BG_BODY)
            sw = canvas.create_window((0, 0), window=inner, anchor="nw")
            # The inner frame drives the scrollregion; the canvas drives the
            # inner frame's WIDTH. Without the second half the rows keep their
            # natural width and the buttons never reach the right-hand edge.
            inner.bind("<Configure>",
                       lambda _e: canvas.configure(
                           scrollregion=canvas.bbox("all")))
            canvas.bind("<Configure>",
                        lambda e: canvas.itemconfigure(sw, width=e.width))

            def wheel(e):
                # Only scroll when there is somewhere to scroll to, or tk
                # clamps and the list twitches under the cursor on a short one.
                first, last = canvas.yview()
                if first <= 0.0 and last >= 1.0:
                    return
                canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")
            for w in (canvas, inner):
                w.bind("<MouseWheel>", wheel)
            return canvas, inner

        # -- Current Shard --
        shard_pg = self._social_page_frames["shard"]
        self._social_query = tk.StringVar()
        srow, self.social_count = search_row(shard_pg, self._social_query)
        # Labelled with the order it IS in, not the one it would switch to —
        # the same convention the rest of the menu's standing settings use.
        # Shard-only: the session log has no level to rank by.
        self.btn_social_sort = tk.Button(
            srow, text=SOCIAL_SORT_LABEL[self._social_sort],
            command=self._enqueue(self._toggle_social_sort),
            font=self.fonts_m["ui"], bg=BG_BODY_SOFT, fg=FG_TEXT,
            activebackground=BG_BAR_TRACK, activeforeground=FG_VALUE,
            relief="flat", bd=0, padx=10, pady=2, highlightthickness=1,
            highlightbackground=BG_BAR_TRACK, cursor="hand2")
        self.btn_social_sort.pack(side="right", padx=(0, 8))
        self.social_canvas, self.social_list = scroll_list(shard_pg)

        # -- This session --
        sess_pg = self._social_page_frames["session"]
        self._session_query = tk.StringVar()
        qrow, self.session_count = search_row(sess_pg, self._session_query)
        self.btn_session_sort = tk.Button(
            qrow, text=SESSION_SORT_LABEL[self._session_sort],
            command=self._enqueue(self._toggle_session_sort),
            font=self.fonts_m["ui"], bg=BG_BODY_SOFT, fg=FG_TEXT,
            activebackground=BG_BAR_TRACK, activeforeground=FG_VALUE,
            relief="flat", bd=0, padx=10, pady=2, highlightthickness=1,
            highlightbackground=BG_BAR_TRACK, cursor="hand2")
        self.btn_session_sort.pack(side="right", padx=(0, 8))
        self.session_canvas, self.session_list = scroll_list(sess_pg)

        # Rebuilt rows live here so a refresh can drop them wholesale. Rebuild
        # is cheap at this size and far simpler than diffing a list whose
        # members come and go as people zone in and out.
        self._social_row_widgets = []
        self._session_row_widgets = []
        self._social_sig = None
        self._session_sig = None
        self._social_note_job = None
        self._social_note_transient = False
        self._social_query.trace_add(
            "write", lambda *_a: self._rebuild_social())
        self._session_query.trace_add(
            "write", lambda *_a: self._rebuild_session())

        # Shared by both pages: the empty-state explanation, and the
        # confirmation line for a copy — see _social_note.
        self.social_note = tk.Label(
            soc, text="", bg=BG_BODY, fg=FG_DIM, font=self.fonts_m["ui_sm_b"],
            anchor="w", justify="left", wraplength=WARN_WRAP)
        self.social_note.pack(fill="x", pady=(6, 0))
        self._set_social_page("shard")

        # ---- General: what the meter does, and how the overlay looks ----
        # Commands are queued rather than run inline: they mutate overlay state
        # the refresh loop also touches, and _drain runs them on the Tk thread.
        section(gen, "METER", first=True)
        self.btn_mode = button(gen, self._enqueue(self._toggle_mode))
        # Directly under the mode button, because it is that button on a timer:
        # the rift decides when it gets pressed instead of you.
        self.btn_rift_auto_view = button(
            gen, self._enqueue(self._toggle_rift_auto_view))
        tk.Label(gen,
                 text=("Presses the button above for you at both rift "
                       "boundaries — all-players going in, party-only coming "
                       "out — instead of asking. Each switch resets the "
                       "encounter, as it does above."),
                 bg=BG_BODY, fg=FG_DIM, font=self.fonts_m["ui_tiny_i"],
                 anchor="w", justify="left",
                 wraplength=WARN_WRAP).pack(fill="x", pady=(0, 2))
        self.btn_auto_reset = button(gen,
                                     self._enqueue(self._toggle_auto_reset_boss))
        row = field(gen, "Reset data")
        self.btn_bind = tk.Button(
            row, text="", command=self._begin_bind_capture, anchor="w",
            font=self.fonts_m["ui"], bg=BG_BODY_SOFT, fg=FG_TEXT,
            activebackground=BG_BAR_TRACK, activeforeground=FG_VALUE,
            relief="flat", bd=0, padx=10, pady=3, highlightthickness=1,
            highlightbackground=BG_BAR_TRACK, cursor="hand2")
        self.btn_bind.pack(side="right", expand=True, fill="x", padx=(8, 0))
        # True while the button is listening for a keypress.
        self._binding_now = False

        section(gen, "SOUND")
        self.btn_sounds = button(gen, self._enqueue(self._toggle_sounds))
        # Live rather than on release, unlike Transparency — this one costs a
        # single MCI call, and hearing the level while you drag is the point.
        row = field(gen, "Volume")
        self._volume_var = tk.IntVar(value=self._sound_volume)
        slider(row, self._volume_var, 0, SOUND_VOLUME_MAX,
               lambda _e: self._on_volume_pick()).pack(
            side="right", expand=True, fill="x", padx=(8, 0))

        section(gen, "LOOK")
        row = field(gen, "Theme")
        self._theme_var = tk.StringVar(value=self._theme_mode)
        self.opt_theme = dropdown(row, self._theme_var, THEME_MODES,
                                  self._on_theme_pick)
        self.opt_theme.pack(side="right", expand=True, fill="x", padx=(8, 0))
        # Released rather than live, like the scale sliders: every step
        # reconfigures five windows.
        row = field(gen, "Transparency")
        self._transp_var = tk.IntVar(value=self._transparency)
        slider(row, self._transp_var, 0, TRANSPARENCY_MAX,
               lambda _e: self._on_transparency_pick()).pack(
            side="right", expand=True, fill="x", padx=(8, 0))

        # ---- Windows: one row per window — visibility and size together ----
        # The old menu split these across SCALING and SHOW / HIDE, which meant
        # the same five windows were listed twice, a screen apart. A window is
        # one thing; its row is one row.
        self._scale_vars = {}
        self.element_vars = {}

        def winrow(label, show_key=None, scale_group=None, note=None):
            row = field(winb, label)
            # The middle column: what shows this window. A fixed width keeps
            # the sliders in a straight line down the page.
            if show_key is not None:
                var = tk.StringVar(value=self._show.get(show_key, ELEMENT_SHOW))
                self.element_vars[show_key] = var
                opt = dropdown(row, var, ELEMENT_MODES,
                               lambda v, k=show_key: self._on_element_pick(k, v),
                               width=10)
                opt.pack(side="left", padx=(8, 0))
            else:
                # Same footprint as the dropdown it stands in for, so the
                # slider column stays a column.
                tk.Label(row, text=note or "", bg=BG_BODY, fg=FG_DIM,
                         font=self.fonts_m["ui_tiny_i"], anchor="w",
                         width=12, padx=10).pack(side="left", padx=(8, 0))
                note = None
            if scale_group is not None:
                var = tk.IntVar(
                    value=int(round(self._scales[scale_group] * 100)))
                self._scale_vars[scale_group] = var
                lo, hi = ((MINIMAP_SCALE_MIN, MINIMAP_SCALE_MAX)
                          if scale_group == "minimap"
                          else (UI_SCALE_MIN, UI_SCALE_MAX))
                # Released rather than live: repainting a whole window on each
                # pixel of drag is visibly slow.
                slider(row, var, lo, hi,
                       lambda _e, g=scale_group: self._on_scale_pick(g),
                       length=110).pack(side="right", expand=True, fill="x",
                                        padx=(8, 0))
            else:
                tk.Label(row, text=note or "", bg=BG_BODY, fg=FG_DIM,
                         font=self.fonts_m["ui_tiny_i"],
                         anchor="e").pack(side="right", expand=True, fill="x",
                                          padx=(8, 0))

        section(winb, "EACH WINDOW: VISIBILITY · SIZE", first=True)
        winrow("Damage meter", "meter", "meter")
        winrow("Breakdown", "detail", "detail")
        # The rift timer wears the meter's fonts, so it has no size of its own.
        winrow("Rift timer", "rift", None, note="sizes with Meter")
        winrow("Minimap", "minimap", "minimap")
        winrow("Compass", "compass", "compass")
        # ...and this panel is only ever on screen with the escape menu, so
        # visibility isn't a choice it can offer about itself.
        winrow("Settings", None, "menu", note="ESC only")

        section(winb, "CONTENT")
        # Columns inside the meter rather than a window, so it keeps its tick.
        self.btn_heal = button(winb, self._enqueue(self._toggle_heal))
        # Hides the same windows the dropdowns above do, just on a condition
        # instead of a choice.
        self.btn_hide_ooc = button(winb, self._enqueue(self._toggle_hide_ooc))

        # ---- Map: the minimap and compass, in one place ----
        section(mp, "MINIMAP", first=True)
        row = field(mp, "Style")
        self._map_mode_var = tk.StringVar(value=self._map_mode)
        self.opt_map = dropdown(row, self._map_mode_var, MINIMAP_MODES,
                                self._on_map_mode_pick)
        self.opt_map.pack(side="right", expand=True, fill="x", padx=(8, 0))
        row = field(mp, "Refresh")
        self._map_rate_var = tk.StringVar(value=self._map_rate)
        self.opt_rate = dropdown(row, self._map_rate_var, MINIMAP_RATE_NAMES,
                                 self._on_map_rate_pick)
        self.opt_rate.pack(side="right", expand=True, fill="x", padx=(8, 0))
        # Released rather than live, like the other sliders: each step
        # re-derives the range the whole draw pass is built on.
        row = field(mp, "Zoom")
        self._map_zoom_var = tk.IntVar(value=self._map_zoom)
        slider(row, self._map_zoom_var, MINIMAP_ZOOM_MIN, MINIMAP_ZOOM_MAX,
               lambda _e: self._on_map_zoom_pick()).pack(
            side="right", expand=True, fill="x", padx=(8, 0))
        # Scales every marker through ONE multiplier, so an obelisk stays
        # larger than a chest and a foe dot stays smaller — the sizes are
        # tuned against each other and this preserves that.
        row = field(mp, "Icon scale")
        self._map_icons_var = tk.IntVar(value=self._map_icons)
        slider(row, self._map_icons_var, MINIMAP_ICONS_MIN, MINIMAP_ICONS_MAX,
               lambda _e: self._on_map_icons_pick()).pack(
            side="right", expand=True, fill="x", padx=(8, 0))
        # The world-map backdrop. It only draws when a shipped asset matches
        # the zone, so the tick is safe to leave on everywhere.
        self.btn_map_bg = button(mp, self._enqueue(self._toggle_map_bg))
        self.btn_map_filter = {}
        for key, label, _cats in MINIMAP_FILTERS:
            self.btn_map_filter[key] = button(
                mp, self._enqueue(lambda k=key: self._toggle_map_filter(k)))

        section(mp, "COMPASS")
        self.btn_compass_filter = {}
        for key, label, _cats in COMPASS_FILTERS:
            self.btn_compass_filter[key] = button(
                mp,
                self._enqueue(lambda k=key: self._toggle_compass_filter(k)))

        # ---- Mounts: the random favorite mount ----
        section(mnt, "RANDOM FAVORITE MOUNT", first=True)
        # One row: the on/off toggle with the pick-mode dropdown to its
        # right — the dropdown modifies what the toggle turns on, so they
        # read as one control rather than two settings.
        row = tk.Frame(mnt, bg=BG_BODY)
        row.pack(fill="x")
        self.btn_mount_random = button(
            row, self._enqueue(self._toggle_mount_random))
        self.btn_mount_random.pack_configure(side="left", expand=True,
                                             padx=(0, 8))
        self._mount_mode_var = tk.StringVar(value=self._mount_mode)
        self.opt_mount_mode = dropdown(row, self._mount_mode_var,
                                       MOUNT_MODES, self._on_mount_mode_pick)
        self.opt_mount_mode.pack(side="right")
        tk.Label(mnt,
                 text=("Every summon becomes a pick from the favorites below "
                       "— rolled fresh each time, or cycled through in order. "
                       "Your equipped mount is never changed — only which one "
                       "actually appears."),
                 bg=BG_BODY, fg=FG_DIM, font=self.fonts_m["ui_tiny_i"],
                 anchor="w", justify="left",
                 wraplength=WARN_WRAP).pack(fill="x", pady=(0, 2))
        section(mnt, "FAVORITES")
        # Rebuilt from the hook's unlock list — see _rebuild_mounts.
        self.mounts_box = tk.Frame(mnt, bg=BG_BODY)
        self.mounts_box.pack(fill="x", pady=(2, 0))
        self._mount_vars = {}
        self._rebuild_mounts([])

        # ---- Gliders: the random favorite glider ----
        # Same layout as Mounts, but the mechanism is honest about being
        # different: gliders have no summon call to swap, so the meter
        # genuinely re-equips (the same call the collection UI makes).
        section(gld, "RANDOM FAVORITE GLIDER", first=True)
        row = tk.Frame(gld, bg=BG_BODY)
        row.pack(fill="x")
        self.btn_glider_random = button(
            row, self._enqueue(self._toggle_glider_random))
        self.btn_glider_random.pack_configure(side="left", expand=True,
                                              padx=(0, 8))
        self._glider_mode_var = tk.StringVar(value=self._glider_mode)
        self.opt_glider_mode = dropdown(row, self._glider_mode_var,
                                        MOUNT_MODES, self._on_glider_mode_pick)
        self.opt_glider_mode.pack(side="right")
        tk.Label(gld,
                 text=("The swap happens WHEN YOU LAND: a pick from the "
                       "favorites below is equipped for your next glide — "
                       "rolled fresh each time, or cycled through in order. "
                       "Unlike mounts this DOES change your equipped glider "
                       "(it's the same equip the collection screen performs), "
                       "so other players see it too."),
                 bg=BG_BODY, fg=FG_DIM, font=self.fonts_m["ui_tiny_i"],
                 anchor="w", justify="left",
                 wraplength=WARN_WRAP).pack(fill="x", pady=(0, 2))
        # Cosmetic side-effect, called out so it doesn't read as a bug: the
        # equip rebuilds the glider model, and that cuts the put-away
        # animation short. Nothing to fix on our side — the game does the
        # same thing when you change gliders from the collection screen.
        tk.Label(gld,
                 text=("Heads up: swapping mid-landing cuts the glider's "
                       "put-away animation short. Cosmetic only, and not "
                       "something the meter can smooth over."),
                 bg=BG_BODY, fg=FG_DIM, font=self.fonts_m["ui_tiny_i"],
                 anchor="w", justify="left",
                 wraplength=WARN_WRAP).pack(fill="x", pady=(0, 2))
        section(gld, "FAVORITES")
        # Rebuilt from the hook's unlock list — see _rebuild_gliders.
        self.gliders_box = tk.Frame(gld, bg=BG_BODY)
        self.gliders_box.pack(fill="x", pady=(2, 0))
        self._glider_vars = {}
        self._rebuild_gliders([])

        # ---- Actions: the things you came here to press ----
        section(act, "PARSE", first=True)
        self.btn_parse = button(act, self._enqueue(self._toggle_parse))
        # Brings the end-of-rift card back after a reflex-close. Greyed until
        # a rift has produced one — see _refresh_menu.
        self.btn_rift_report = button(act, self._enqueue(self._reopen_report))
        self.btn_parses = button(act, self._enqueue(self._open_parses))
        self.btn_parses.config(text="Parses & Rift Reports")

        # Both throw work away, so they want distance from the buttons above.
        section(act, "RESET")
        # Exactly what the hotkey fires, so the two can't diverge. Labelled with
        # the keybind because the hotkey is the one that's useful mid-fight,
        # when the escape menu (and so this button) isn't an option.
        self.btn_reset_data = button(act, self._enqueue(self.session.reset))
        self.btn_reset_data.config(
            text=f"Reset encounter data   ({bind_label()})")
        self.btn_reset_pos = button(act, self._enqueue(self._reset_pos))
        self.btn_reset_pos.config(text="Reset window positions")

        # Where the meter lives: the README, the releases, and the place to
        # report a bug. A button rather than a clickable version label,
        # because the header is a drag handle and a label that both drags and
        # navigates does one of them by surprise.
        section(act, "PROJECT")
        self.btn_repo = button(act, self._enqueue(self._open_repo))
        self.btn_repo.config(text="Farever+ on GitHub")

        # ---- footer: on every tab, because it ends the session ----
        # Here as well as on the tray icon because this is where the user
        # already is — mid-game, escape menu open — and because a tray icon
        # Windows 11 has filed into the overflow flyout is not somewhere you
        # can count on them finding.
        tk.Frame(body, bg=BG_BAR_TRACK, height=1).pack(fill="x", pady=(10, 6))
        self.btn_quit = button(body, self._enqueue(self._quit_clicked))
        self.btn_quit.config(text=QUIT_LABEL, fg=FG_WARN)
        self.menu.minsize(MIN_W["menu"], 0)
        self._set_menu_tab("General")

    def _set_menu_tab(self, name):
        """Raise one settings page and paint its tab as the active one. The
        frames all live in the same grid cell, so this is a lift, not a
        re-layout."""
        if name not in self._menu_tab_frames:
            return
        # Leaving Social means you're done typing. Tk focus survives a raise —
        # the search box is only hidden, not destroyed — so without this the
        # keyboard would still be pointed at an off-screen Entry, and a key
        # pressed over on Windows while rebinding would land in it too.
        if name != "Social":
            self._stop_typing()
        self._menu_tab = name
        self._menu_tab_frames[name].tkraise()
        for n, b in self._menu_tab_btns.items():
            self._paint_tab_btn(b, n == name)
        # Raising Social is one of its load moments — the pages don't poll,
        # so arriving at the tab is what fetches the current picture.
        if name == "Social":
            self._reload_social()
        # A dropdown posted from the page on the way out would float over the
        # one arriving.
        self._unpost_menus()

    @staticmethod
    def _paint_tab_btn(b, active):
        """Selected-tab styling, in one place. Both tab levels — the vertical
        navbar and the Social sub-tabs — call this, so a restyle of one cannot
        quietly leave the other looking like the old build."""
        b.config(bg=BTN_ON_BG if active else BG_BODY_SOFT,
                 fg=FG_HEADER if active else FG_TEXT,
                 activebackground=BTN_ON_BG_ACTIVE if active else BG_BAR_TRACK,
                 activeforeground=FG_HEADER if active else FG_VALUE,
                 highlightbackground=BTN_ON_BG_ACTIVE if active
                 else BG_BAR_TRACK)

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
        self._apply_map_zoom()

    def _minimap_px(self, ex, ey, me, half, scale, rot):
        """World -> canvas, relative to the player.

        The game's +y is drawn as up, which means the canvas y is negated: Tk's
        y grows downward and the world's does not.

        `rot` is (cos r, sin r) in rotating mode and None in fixed mode. The
        offset is turned so the direction the player faces lands at the top of
        the map — which is what lets the arrow stay still.

        Facing is (cos r, sin r), i.e. r is measured from +x. Turning that to
        screen-up is a rotation by (pi/2 - r), which reduces to the form below;
        substituting the facing vector gives (0, 1) as it should.

        Fixed mode is NOT its own geometry: north-up is the rotating formula
        evaluated at the north heading (270 deg — the game's +y is SOUTH, see
        COMPASS_CARDINALS). Substituting ca=0, sa=-1 collapses mirror and
        y-flip together into plain (+dx, +dy) — the mirror is still in there,
        folded in, so don't "fix" its absence. This branch used to be
        (-dx, -dy): a half-turn, which preserves handedness and so looked
        perfectly self-consistent — arrows agreed with positions — while
        putting north at the bottom against the sky."""
        dx, dy = ex - me["x"], ey - me["y"]
        if rot is not None:
            ca, sa = rot
            dx, dy = dx * sa - dy * ca, dx * ca + dy * sa
            return half + MINIMAP_MIRROR_X * dx * scale, half - dy * scale
        return half + dx * scale, half + dy * scale

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

        # The map backdrop goes down before anything else — canvas stacking is
        # creation order, so first drawn is bottom-most.
        self._draw_map_backdrop(c, half, size, me,
                                heading if rot is not None else None, body)

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
                r = style["r"] * self._icon_scale()
                # Nodes swap in their material's colour (and the rare halo)
                # while keeping the category's shape — rock stays rock.
                nst = (_node_style(e.get("g"))
                       if cat in ("ore", "herb") else None)
                if nst and nst.get("rare"):
                    r *= NODE_RARE_SCALE
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
                fill = self._marker_fill((nst or style)["fill"])
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
                                ring=self._marker_fill(
                                    (nst or style).get("ring")))
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

    def _draw_map_backdrop(self, c, half, size, me, heading, body):
        """Paint the world map under the markers, if there is one to paint.

        `heading` is None in fixed mode (the crop is already north-up, the
        same +x-right/+y-down frame as _minimap_px's fixed branch) and the
        camera azimuth in rotating mode. Every early-out below is the cheap
        kind — the expensive path only runs when a real image is drawn."""
        if not self._map_bg_on:
            return
        # Inside a rift the zone sig still names the world you left, but the
        # rift is not that world — the flat panel is the honest background.
        if self.ui_state.in_rift():
            return
        sig = self.ui_state.zone_sig()
        if sig != self._map_bg_key:
            # Resolve sig -> asset once per zone, not per tick: it globs disk.
            self._map_bg_key = sig
            self._map_bg_world = self._map_backdrop.available_world(sig)
        if self._map_bg_world is None:
            return
        im = self._map_backdrop.crop(self._map_bg_world,
                                     me.get("x", 0), me.get("y", 0),
                                     float(self._map_range), size,
                                     heading=heading)
        if im is None:
            return
        try:
            from PIL import Image, ImageTk
        except ImportError:
            return
        # Pulled toward the panel colour so markers keep winning the contrast
        # fight, and so every theme (parchment, dark, rift) tints its own map.
        rgb = tuple(int(body.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        im = Image.blend(im, Image.new("RGB", im.size, rgb), MINIMAP_BG_TINT)
        # One PhotoImage, pasted into — a fresh photo per tick is garbage the
        # collector has to chase four times a second.
        if self._map_photo is None or self._map_photo.width() != size:
            self._map_photo = ImageTk.PhotoImage(im)
        else:
            self._map_photo.paste(im)
        c.create_image(half, half, image=self._map_photo)

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
        # Same collapse as _minimap_px's fixed branch: the rotating form at
        # the north heading. World (cos a, sin a) maps to screen (cos a,
        # sin a) because the map now draws world offsets as (+dx, +dy).
        return (math.cos(world_angle), math.sin(world_angle))

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
        if cat in ("ore", "herb"):
            # "Copper Lode (Ore)", from the same CDB texts the game's own
            # widget shows. The placement id fallback is prettified for the
            # frame or two before the name resolves.
            nm = (name or "").strip() or _pretty_id(e.get("k") or "")
            tag = MINIMAP_LABELS[cat]
            return f"{self._elide(nm)} ({tag})" if nm else tag
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
        # The rare-node halo: a ring around the whole glyph, drawn first so
        # the shape sits on it. The dot keeps its own tighter ring below —
        # that one is the orb's second colour, not a rarity mark.
        if ring and shape in ("rock", "leaf"):
            rr = r * 1.5
            c.create_oval(x - rr, y - rr, x + rr, y + rr,
                          outline=ring, fill="",
                          width=max(1, int(round(self._scales["minimap"] * 1.5))))
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
        if shape == "rock":
            # The ore cluster, after the game's own icon: a tall central
            # crystal flanked by lower chunks. At marker size the cluster is
            # one blob, so what's drawn is what survives the shrinking — the
            # jagged skyline — plus a single lit facet down the tall crystal's
            # right side, which is what makes it read as stone rather than as
            # a grey splat. Same reduction the soulstone shard went through.
            def P(pts):
                out = []
                for px_, py_ in pts:
                    out.extend((x + px_ * r, y + py_ * r))
                return out
            c.create_polygon(*P(((-1.05, 0.75), (-1.25, 0.05), (-0.5, -0.7),
                                 (-0.15, -0.4), (0.3, -1.3), (0.9, -0.4),
                                 (1.2, 0.2), (0.85, 0.75))),
                             fill=fill, outline=edge or "", width=1)
            c.create_polygon(*P(((0.3, -1.3), (0.9, -0.4), (0.45, 0.75),
                                 (0.05, -0.25))),
                             fill=_lerp_hex(fill, "#FFFFFF", 0.4), outline="")
            return
        if shape == "leaf":
            # A pointed oval on the diagonal, with a vein and a stub of stem —
            # the two details that say "leaf" once the outline is four pixels
            # tall. Sides are quadratic beziers sampled into a plain polygon
            # rather than tk's smoothed splines: sampled points draw the same
            # on every renderer, and the tip and base stay sharp instead of
            # being rounded off with the rest of the corners.
            B, T = (-0.95, 0.95), (0.95, -0.95)
            def side(p0, p1, cx_, cy_):
                pts = []
                for i in range(1, 8):
                    t = i / 8.0
                    mt = 1.0 - t
                    pts.append((mt * mt * p0[0] + 2 * mt * t * cx_ + t * t * p1[0],
                                mt * mt * p0[1] + 2 * mt * t * cy_ + t * t * p1[1]))
                return pts
            outline_pts = [B] + side(B, T, 0.92, 0.92) + [T] \
                        + side(T, B, -0.92, -0.92)
            flat = []
            for px_, py_ in outline_pts:
                flat.extend((x + px_ * r, y + py_ * r))
            c.create_polygon(*flat, fill=fill, outline=edge or "", width=1)
            vein = _lerp_hex(fill, "#000000", 0.45)
            # Vein base-to-tip; the stem carries past the base on the same
            # line, so the two read as one stroke through the leaf.
            c.create_line(x + B[0] * 1.35 * r, y + B[1] * 1.35 * r,
                          x + T[0] * 0.72 * r, y + T[1] * 0.72 * r,
                          fill=vein, width=1)
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
        s = 6.0 * self._icon_scale()
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
        cream text on a cream background.

        The distance badges are the one part that isn't on this canvas. They
        want to be translucent — a box that softens the scenery without
        blacking it out — and a colorkey window can't blend: every pixel is
        solid or a hole. So the boxes are drawn on `badgewin`, a second
        colorkey window glued directly under this one at a lower whole-window
        opacity (the one kind of blending Windows does provide), and the
        numbers stay up here, crisp, on top of them. _glue_badgewin keeps the
        two windows coincident; _sync_badgewin keeps their opacities and
        mapped states married."""
        self.compass_canvas = tk.Canvas(
            self.compasswin, bg=TRANSPARENT_KEY,
            highlightthickness=0, bd=0, width=COMPASS_W, height=COMPASS_H)
        self.compass_canvas.pack()
        self.badge_canvas = tk.Canvas(
            self.badgewin, bg=TRANSPARENT_KEY,
            highlightthickness=0, bd=0, width=COMPASS_W, height=COMPASS_H)
        self.badge_canvas.pack()
        # A badge box is part of the compass as far as the hand cares, so
        # grabbing one drags the compass; the underlay follows via the glue.
        self._bind_drag(self.compasswin,
                        (self.compass_canvas, self.badge_canvas),
                        unlocked=self._mouse_available)
        self.compasswin.bind("<Configure>", self._glue_badgewin, add="+")

    def _glue_badgewin(self, _event=None):
        """Pin the badge underlay exactly under the compass window."""
        self.badgewin.geometry(
            f"+{self.compasswin.winfo_x()}+{self.compasswin.winfo_y()}")

    def _sync_badgewin(self):
        """Mirror the compass's opacity and mapped state onto the badge
        underlay.

        The underlay is deliberately NOT in _fade_win — it has no state of its
        own. Its opacity is always the compass's times COMPASS_DIST_BOX_ALPHA,
        which is what makes the boxes read as knocked-back while following
        every fade, the transparency slider and the boss-bar auto-hide with no
        handling of their own. Called from wherever the compass's alpha or
        mapped state changes: _step_fade, _want_visible, _set_transparency and
        once at startup."""
        try:
            self.badgewin.attributes(
                "-alpha", self._alpha["compass"] * COMPASS_DIST_BOX_ALPHA)
            if self.compasswin.state() == "normal":
                self._glue_badgewin()
                if self.badgewin.state() != "normal":
                    self.badgewin.deiconify()
                # Under the numbers, over the game — reasserted every sync,
                # because deiconify and the compass's own -topmost re-assert
                # both shuffle sibling order, and a badge that wins that race
                # is a black box sitting ON the numbers it exists to sit under.
                self.badgewin.lower(self.compasswin)
            else:
                self.badgewin.withdraw()
        except tk.TclError:
            pass

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
        cb = self.badge_canvas
        me, ents, _stamp = self.world.read()
        c.delete("all")
        cb.delete("all")
        scale = self._scales["compass"]
        w, h = int(COMPASS_W * scale), int(COMPASS_H * scale)
        if int(c["width"]) != w or int(c["height"]) != h:
            c.config(width=w, height=h)
        if int(cb["width"]) != w or int(cb["height"]) != h:
            cb.config(width=w, height=h)
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
            # Same material override as the map's, so a tungsten bearing looks
            # like the tungsten marker you'd walk to.
            nst = _node_style(e.get("g")) if cat in ("ore", "herb") else None
            r = style["r"] * scale * (NODE_RARE_SCALE
                                      if nst and nst.get("rare") else 1.0)
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
            self._map_glyph(c, x, my, r, style,
                            self._marker_fill((nst or style)["fill"]),
                            facing, edge,
                            ring=self._marker_fill((nst or style).get("ring")))
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
        # read. They also hang off the pill's lower edge onto the game — the
        # only text on the overlay that sits on scenery rather than on a
        # panel, and the scenery is any colour it likes — so each one gets a
        # box behind it. The box goes on the badge underlay window and the
        # text stays on this canvas above it; see _build_compass for why the
        # translucency needs a second window at all.
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
            self.badge_canvas.create_rectangle(
                x - half_tw - pad_x, y - line_h / 2.0 - pad_y,
                x + half_tw + pad_x, y + line_h / 2.0 + pad_y,
                fill=COMPASS_DIST_BOX, outline="")
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

        # "Do this every time" IS the General tab's setting, offered where the
        # question is being asked. Ticking it here and turning it on there are
        # the same act, so the two can't disagree — see _answer_rift for why it
        # only commits on Yes.
        self._prompt_every_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            body, text="Do this every time", variable=self._prompt_every_var,
            bg=RIFT_BODY, fg=RIFT_TITLE, activebackground=RIFT_BODY,
            activeforeground=RIFT_TIME, selectcolor=RIFT_GLOW,
            font=self.fonts["ui_10"], anchor="w",
            highlightthickness=0, bd=0, cursor="hand2").pack(fill="x")

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

    def _build_update_offer(self):
        """The "a new version is out — install it?" popup.

        Painted in the meter's own colours rather than the rift prompt's, and
        that is the whole point of it being a separate window: the rift palette
        means "the game did something", and this is the meter talking about
        itself. Sharing promptwin would also mean the two questions could
        collide, since a rift entry and an update check are unrelated events.
        """
        border = tk.Frame(self.updatewin, bg=BG_BORDER, padx=2, pady=2)
        border.pack(fill="both", expand=True)
        header = tk.Frame(border, bg=BG_HEADER_UNLOCKED)
        header.pack(fill="x")
        tk.Label(header, text="Farever+ update", bg=BG_HEADER_UNLOCKED,
                 fg=FG_HEADER, font=self.fonts["ui_b"], anchor="w",
                 padx=12, pady=6).pack(side="left")

        body = tk.Frame(border, bg=BG_BODY, padx=18, pady=14)
        body.pack(fill="both", expand=True)
        self.update_title = tk.Label(body, text="", bg=BG_BODY, fg=FG_VALUE,
                                     font=self.fonts["ui_lg_b"], anchor="w")
        self.update_title.pack(fill="x")
        self.update_body = tk.Label(body, text="", bg=BG_BODY, fg=FG_TEXT,
                                    font=self.fonts["ui_10"], anchor="w",
                                    justify="left", pady=8,
                                    wraplength=UPDATE_OFFER_WRAP)
        self.update_body.pack(fill="x")

        btns = tk.Frame(body, bg=BG_BODY)
        btns.pack(fill="x", pady=(10, 0))

        def offer_button(text, on, primary):
            b = tk.Button(btns, text=text, command=on,
                          font=self.fonts["ui_b"],
                          bg=BTN_ON_BG if primary else BG_BODY_SOFT,
                          fg=FG_HEADER if primary else FG_TEXT,
                          activebackground=BTN_ON_BG_ACTIVE if primary
                          else BG_BAR_TRACK,
                          activeforeground=FG_HEADER if primary else FG_VALUE,
                          relief="flat", bd=0, padx=26, pady=8,
                          highlightthickness=1, cursor="hand2",
                          highlightbackground=BG_BAR_TRACK)
            b.pack(side="left", expand=True, fill="x", padx=4)
            return b

        self.btn_update_yes = offer_button(
            "", self._enqueue(lambda: self._answer_update_offer(True)), True)
        offer_button("Later",
                     self._enqueue(lambda: self._answer_update_offer(False)),
                     False)
        self.updatewin.minsize(MIN_W["update"], 0)

    def _tick_update_offer(self):
        """Offer an automatically-found update, once, when it can be answered.

        The gate is the cursor, not a timer. The overlay is click-through while
        the game owns the mouse, so a popup during play would be a box you
        cannot press — and it would be covering a fight. Waiting for a free
        cursor (the escape menu, or L-ALT) means the offer arrives exactly when
        the player is already in UI mode, and never mid-pull.
        """
        if self._update_offer_done or self._update_offer_open:
            return
        # With the game gone the exit prompt is the only thing that should be
        # on screen, and an update offer would be a second dialog competing
        # with it — the same reasoning that keeps it away mid self-update.
        if self._game_gone:
            return
        if not UPDATE["prompt"] or not UPDATE["latest"]:
            return
        if self._updating or self._prompt_open or not self._focused:
            return
        if not self._cursor_free:
            return
        self._open_update_offer()

    def _open_update_offer(self):
        self._update_offer_open = True
        can = self._can_self_update()
        self.update_title.config(text=f"Farever+ {UPDATE['latest']} is available")
        self.update_body.config(
            text=(f"You're running {VERSION}. The update takes a few seconds — "
                  "the meter closes, installs and comes back on its own."
                  if can else
                  f"You're running {VERSION}. This release has no installer to "
                  "run from here, so this opens the download page."))
        self.btn_update_yes.config(text="Update now" if can else "Download")
        self.updatewin.update_idletasks()
        l, t, r, b = self._game_rect()
        w = max(self.updatewin.winfo_reqwidth(), MIN_W["update"])
        h = max(self.updatewin.winfo_reqheight(), 120)
        self.updatewin.geometry(
            f"+{l + ((r - l) - w) // 2}+{t + ((b - t) - h) // 2}")
        self._apply_clickthrough()      # the offer has to be clickable
        self._refresh_visibility()
        print(f"[meter] offering update {UPDATE['latest']} "
              f"({'self-update' if can else 'download'}).", file=sys.stderr)

    def _answer_update_offer(self, yes):
        """Either answer retires the offer for this session. "Later" is not
        "never": the menu's notice line stays, and the header's Check updates
        button still works — this only stops the popup asking again."""
        self._update_offer_open = False
        self._update_offer_done = True
        UPDATE["prompt"] = False
        self._refresh_visibility()
        print(f"[meter] update offer: {'accepted' if yes else 'dismissed'}.",
              file=sys.stderr)
        if yes:
            self._on_update_click()

    def _open_rift_prompt(self, kind):
        self._prompt_kind = kind
        # Opens unticked every time. The prompt only exists while the setting
        # is off, so a ticked box would be claiming a state that isn't real.
        self._prompt_every_var.set(False)
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

    def _apply_rift_view(self, kind):
        """Switch the view the way the rift wants it — to all-players on the
        way in, back to party-only on the way out.

        That resets the encounter, the same as the mode button: the two views
        can't share one encounter without the percentages lying. Saved too, so
        a restart comes back on the view you're actually looking at."""
        want = "all" if kind == "enter" else "party"
        if self.mode == want:
            return False
        self.mode = want
        self.focus_player = None
        self.session.reset()
        self._save_settings()
        return True

    def _answer_rift(self, yes):
        """Yes switches the view; No leaves it alone.

        "Do this every time" only commits on Yes, because it is shorthand for
        "that answer, standing" — and No is the answer that says the meter
        shouldn't be touching the view. Ticking it and then pressing No is a
        contradiction, so No wins and the box is discarded."""
        if yes:
            self._apply_rift_view(self._prompt_kind)
            if self._prompt_every_var.get() and not self._rift_auto_view:
                self._rift_auto_view = True
                self._save_settings()
                print("[meter] rift view switching is now automatic.",
                      file=sys.stderr)
        self._close_rift_prompt()
        print(f"[meter] rift prompt answered: {'yes' if yes else 'no'}",
              file=sys.stderr)

    def _toggle_rift_auto_view(self):
        """The General tab's half of the same setting. Turning it ON mid-rift
        doesn't retroactively switch the view — it's a rule for the next
        crossing, and silently binning the encounter you're in the middle of
        is not what pressing a settings toggle should do."""
        self._rift_auto_view = not self._rift_auto_view
        self._save_settings()
        # An open prompt is now answering a question that has a standing
        # answer. Honour it rather than leaving a stale box on screen.
        if self._rift_auto_view and self._prompt_open:
            self._apply_rift_view(self._prompt_kind)
            self._close_rift_prompt()

    def _tick_rift(self):
        """One prompt per rift entry, and it goes away by itself if the rift
        does — an unanswered box shouldn't outlive what it was asking about.

        With the standing answer on there is no box at all: the crossing is
        acted on directly, in both directions."""
        in_rift = self.ui_state.in_rift()
        if in_rift == self._rift_seen:
            return
        self._rift_seen = in_rift
        # Leaving is a question in its own right rather than a dismissal: the
        # all-players view you switched on for the rift is the wrong one to be
        # left holding once you're back outside.
        kind = "enter" if in_rift else "leave"
        if self._rift_auto_view:
            switched = self._apply_rift_view(kind)
            print(f"[meter] {'entered' if in_rift else 'left'} a rift — "
                  + ("switched the player view automatically." if switched
                     else "the player view was already right."),
                  file=sys.stderr)
            return
        self._open_rift_prompt(kind)

    # ---- end-of-rift report ----
    def _build_report(self):
        """The end-of-rift report card. Rift-styled like the prompts — it only
        ever exists because of one — and clickable whenever it's up, for the
        same reason the prompt is: close and copy are the whole point.

        The chrome is built once; the numbers are torn down and rebuilt by
        _render_report, which only runs when the card opens or a tab is
        clicked — never on the refresh tick."""
        glow = tk.Frame(self.reportwin, bg=RIFT_GLOW, padx=1, pady=1)
        glow.pack(fill="both", expand=True)
        border = tk.Frame(glow, bg=RIFT_EDGE, padx=2, pady=2)
        border.pack(fill="both", expand=True)
        header = tk.Frame(border, bg=RIFT_GLOW)
        header.pack(fill="x")
        # Stamped with the kill time on open — the card can be brought back
        # from the menu long after the rift, and an unstamped one reads as
        # current.
        self._report_title = tk.Label(header, text="RIFT REPORT",
                                      bg=RIFT_GLOW, fg=RIFT_TIME,
                                      font=self.fonts["ui_b"], anchor="w",
                                      padx=12, pady=6)
        title = self._report_title
        title.pack(side="left")
        tk.Button(header, text="✕", command=self._enqueue(self._close_report),
                  font=self.fonts["ui_b"], bg=RIFT_GLOW, fg=RIFT_TITLE,
                  activebackground=RIFT_EDGE, activeforeground="#2C0A1E",
                  relief="flat", bd=0, padx=10, cursor="hand2",
                  highlightthickness=0).pack(side="right", fill="y")
        # The header doubles as the drag handle, same as every other window —
        # a card parked over the loot can be moved rather than dismissed.
        self._bind_drag(self.reportwin, (header, title),
                        unlocked=self._mouse_available)

        body = tk.Frame(border, bg=RIFT_BODY, padx=16, pady=12)
        body.pack(fill="both", expand=True)

        # Both phases at once, side by side — the card exists to compare the
        # AoE clear against the boss burn, and a comparison you have to click
        # between isn't one.
        self._report_body = tk.Frame(body, bg=RIFT_BODY)
        self._report_body.pack(fill="both", expand=True)

        footer = tk.Frame(body, bg=RIFT_BODY)
        footer.pack(fill="x", pady=(10, 0))
        tk.Button(footer, text="Copy", command=self._enqueue(self._copy_report),
                  font=self.fonts["ui_b"], bg=RIFT_EDGE, fg="#2C0A1E",
                  activebackground=RIFT_TITLE, activeforeground="#2C0A1E",
                  relief="flat", bd=0, padx=24, pady=5, cursor="hand2",
                  highlightthickness=1,
                  highlightbackground=RIFT_EDGE).pack(side="left")
        # The copy feedback. Empty text rather than pack_forget when idle, so
        # the footer never changes height under the cursor.
        self._report_flash = tk.Label(footer, text="", bg=RIFT_BODY,
                                      fg=RIFT_TITLE, font=self.fonts["ui"],
                                      anchor="w", padx=10)
        self._report_flash.pack(side="left", fill="x", expand=True)
        # Same wording as the minimap's tip: the card takes clicks whenever
        # it's up, but there's only a cursor to click with once the game lets
        # go of it, which isn't something you'd guess.
        tk.Label(body, text="Press L-ALT or ESC to enable free mouse",
                 bg=RIFT_BODY, fg=RIFT_GLOW, font=self.fonts["ui_tiny_i"],
                 anchor="w").pack(fill="x", pady=(6, 0))

    @staticmethod
    def _mmss(secs):
        m, s = divmod(int(max(0, secs)), 60)
        return f"{m}:{s:02d}"

    @staticmethod
    def _elide_name(name, width=14):
        return name if len(name) <= width else name[:width - 1] + "…"

    def _render_report(self):
        """Rebuild the card: both phase columns, leaderboard-weighted.

        Rows are frames with the name packed left and the numbers packed
        right, not one mono string — the ranks wear different font sizes, and
        mono-space alignment dies the moment two sizes share a column."""
        data = self._report_data
        if not data:
            return
        for w in self._report_body.winfo_children():
            w.destroy()
        cols = tk.Frame(self._report_body, bg=RIFT_BODY)
        cols.pack(fill="both", expand=True)
        # Uniform grid columns, so the two phases stay the same width however
        # long the names run — a comparison wants its columns comparable.
        cols.grid_columnconfigure(0, weight=1, uniform="phase")
        cols.grid_columnconfigure(2, weight=1, uniform="phase")
        cols.grid_rowconfigure(0, weight=1)
        for i, ph in enumerate(data["phases"]):
            if i:
                tk.Frame(cols, bg=RIFT_GLOW, width=1).grid(
                    row=0, column=1, sticky="ns", padx=12)
            col = tk.Frame(cols, bg=RIFT_BODY)
            col.grid(row=0, column=i * 2, sticky="nsew")
            self._render_phase_column(col, ph)

    def _render_phase_column(self, col, ph):
        def line(text, font="ui_10", fg=RIFT_TIME, pady=0):
            tk.Label(col, text=text, bg=RIFT_BODY, fg=fg,
                     font=self.fonts[font], anchor="w",
                     pady=pady).pack(fill="x")

        def heading(text):
            row = tk.Frame(col, bg=RIFT_BODY)
            row.pack(fill="x", pady=(10, 3))
            tk.Label(row, text=text, bg=RIFT_BODY, fg=RIFT_TITLE,
                     font=self.fonts["ui_sm_b"], anchor="w").pack(side="left")
            tk.Frame(row, bg=RIFT_GLOW, height=1).pack(
                side="left", fill="x", expand=True, padx=(8, 0))

        def rank_row(i, p, amount, pct, medal=True):
            """One leaderboard entry. Ranks 1-3 wear medal colours and the
            bigger font; 4-5 are body text — the tiering IS the design.

            The class acronym is a label of its own in the dim colour, not part
            of the name string: it must not be eaten by the name's elision, and
            it is not part of who anyone is."""
            row = tk.Frame(col, bg=RIFT_BODY)
            row.pack(fill="x", pady=1)
            top3 = medal and i <= 3
            rank_fg = REPORT_MEDALS[i - 1] if top3 else RIFT_TITLE
            name_font = self.fonts["ui_rank_b" if top3 else "ui_10"]
            tk.Label(row, text=str(i), bg=RIFT_BODY, fg=rank_fg,
                     font=name_font, width=2, anchor="w").pack(side="left")
            tk.Label(row, text=self._elide_name(p.get("name") or "?"),
                     bg=RIFT_BODY, fg=RIFT_TIME if top3 else RIFT_TITLE,
                     font=name_font, anchor="w").pack(side="left")
            if p.get("cls"):
                tk.Label(row, text=p["cls"], bg=RIFT_BODY, fg=RIFT_TITLE,
                         font=self.fonts["ui_sm_b"], anchor="w").pack(
                    side="left", padx=(4, 0))
            tk.Label(row, text=f"{pct:4.0f}%", bg=RIFT_BODY, fg=RIFT_TITLE,
                     font=self.fonts["mono_sm"], anchor="e",
                     width=5).pack(side="right")
            tk.Label(row, text=f"{int(amount):,}", bg=RIFT_BODY,
                     fg=RIFT_TIME, font=self.fonts["mono_10"],
                     anchor="e").pack(side="right", padx=(0, 6))

        # The phase title is the column's headline; the totals sit under it.
        line(ph["label"].upper(), font="ui_b", fg=RIFT_PEAK)
        # Boost joins the totals line only when the run contained any — and
        # `.get`, because reports saved before the split have no such key.
        boost = ph.get("boost", 0.0)
        line(f"{self._mmss(ph['duration'])}   ·   "
             f"{int(ph['total']):,} dmg   ·   {int(ph['heal']):,} heal"
             + _overheal_note(ph)
             + (f"   ·   {int(boost):,} boost" if boost > 0.5 else ""),
             fg=RIFT_TITLE)

        players = ph["players"]
        if not players:
            line("nothing was recorded for this phase", font="ui_idle_i",
                 fg=RIFT_TITLE, pady=12)
            return

        # The MVP block: the phase's top damage, at headline size — this is
        # the line the card exists for. The top healer rides under it in their
        # own colour; sorted by damage already, so [0] is the damage MVP.
        heading("MVP")
        mvp = players[0]
        mvp_row = tk.Frame(col, bg=RIFT_BODY)
        mvp_row.pack(fill="x")
        tk.Label(mvp_row, text=f"★ {self._elide_name(mvp['name'])}",
                 bg=RIFT_BODY, fg=REPORT_MEDALS[0],
                 font=self.fonts["ui_mvp_b"], anchor="w").pack(side="left")
        if mvp.get("cls"):
            tk.Label(mvp_row, text=mvp["cls"], bg=RIFT_BODY, fg=RIFT_TITLE,
                     font=self.fonts["ui_sm_b"], anchor="s").pack(
                side="left", padx=(5, 0), pady=(0, 4))
        line(f"{int(mvp['total']):,} damage", fg=RIFT_TIME)
        healer = max(players, key=lambda p: p["heal"])
        if healer["heal"] > 0.5:
            tk.Label(col, text=f"✚ {self._elide_name(healer['name'])}"
                     + (f" ({healer['cls']})" if healer.get("cls") else "")
                     + f"   {int(healer['heal']):,} heal"
                     + _overheal_note(healer, "   {:.0f}% over"),
                     bg=RIFT_BODY, fg=REPORT_HEAL,
                     font=self.fonts["ui_rank_b"], anchor="w").pack(
                fill="x", pady=(2, 0))

        heading("DAMAGE — TOP 5")
        for i, p in enumerate(players[:5], 1):
            pct = p["total"] / ph["total"] * 100 if ph["total"] else 0.0
            rank_row(i, p, p["total"], pct)

        healers = sorted((p for p in players if p["heal"] > 0.5),
                         key=lambda p: -p["heal"])
        heading("HEALING — TOP 5")
        if not healers:
            line("no healing recorded", font="ui_idle_i", fg=RIFT_TITLE)
        for i, p in enumerate(healers[:5], 1):
            pct = p["heal"] / ph["heal"] * 100 if ph["heal"] else 0.0
            rank_row(i, p, p["heal"], pct)

        # Every type in its own colour — the bar and the name wear it, the
        # percentage stays quiet. Unknown affinities get a stable hash tint
        # from element_color, so a new patch element shows up coloured.
        heading("DAMAGE BY TYPE")
        top = ph["elements"][0][1] if ph["elements"] else 0.0
        for el, amt in ph["elements"][:8]:
            pct = amt / ph["total"] * 100 if ph["total"] else 0.0
            # The table's colours are tuned mid-tone; lifted toward white here
            # because they have to read on the card's near-black body.
            color = _lerp_hex(element_color(el), "#FFFFFF", 0.30)
            row = tk.Frame(col, bg=RIFT_BODY)
            row.pack(fill="x", pady=1)
            tk.Label(row, text="Other" if el == "?" else el, bg=RIFT_BODY,
                     fg=color, font=self.fonts["ui_sm_b"], width=9,
                     anchor="w").pack(side="left")
            bar = "▰" * max(1, round(amt / top * 12)) if top else ""
            tk.Label(row, text=bar, bg=RIFT_BODY, fg=color,
                     font=self.fonts["mono_sm"], anchor="w").pack(side="left")
            tk.Label(row, text=f"{pct:4.1f}%", bg=RIFT_BODY, fg=RIFT_TIME,
                     font=self.fonts["mono_sm"], anchor="e").pack(side="right")

    def show_rift_report(self, report):
        """A rift's boss died — freeze the card over the game. Called from the
        hook thread; everything real happens on the Tk one.

        The text version goes to disk the moment the report exists, before the
        card is even up — a card closed by reflex (it happened on day one)
        must not be the only copy of a run that can't be re-fought."""
        def open_():
            self._report_data = report
            self._save_rift_report(report)
            self._open_report_card()
        self._enqueue(open_)()

    def _open_report_card(self):
        """Open (or re-open) the card over whatever _report_data holds."""
        self._report_flash.config(text="")
        # A report can now outlive its session, so a bare clock isn't enough:
        # "21:03" on yesterday's run reads as tonight's. The date appears
        # exactly when it stops being obvious.
        lt = time.localtime(self._report_data["at"])
        fmt = ("%H:%M" if time.strftime("%Y%m%d", lt) == time.strftime("%Y%m%d")
               else "%b %d, %H:%M")
        self._report_title.config(
            text="RIFT REPORT — " + time.strftime(fmt, lt))
        self._render_report()
        self._report_open = True
        self.reportwin.update_idletasks()
        l, t, r, b = self._game_rect()
        w = max(self.reportwin.winfo_reqwidth(), 300)
        h = max(self.reportwin.winfo_reqheight(), 200)
        self.reportwin.geometry(
            f"+{l + ((r - l) - w) // 2}+{t + ((b - t) - h) // 2}")
        self._apply_clickthrough()   # the card has to be clickable
        self._refresh_visibility()

    def _reopen_report(self):
        """The menu's 'Last rift report' — the card back, exactly as it was.
        The data is already frozen plain data, so there's nothing to rebuild;
        a no-op until the first rift of the session produces one."""
        if self._report_data is not None:
            self._open_report_card()

    def _save_rift_report(self, report):
        """The report into parses/, three ways: .json is the full metrics —
        the file _load_last_rift_report reads back, which is what lets 'Last
        Rift Report' survive a meter restart; .txt is the chat-pasteable
        lines; .png is the shareable image. Same folder, same lifecycle as
        the parse screenshots. Never fatal, and each format fails alone: no
        Pillow costs the picture, not the data."""
        base = f"rift-{time.strftime('%Y%m%d-%H%M%S')}"
        try:
            PARSES_DIR.mkdir(parents=True, exist_ok=True)
            (PARSES_DIR / f"{base}.json").write_text(json.dumps(report),
                                                     encoding="utf-8")
            (PARSES_DIR / f"{base}.txt").write_text(self._report_text(report),
                                                    encoding="utf-8")
            print(f"[meter] rift report saved to {PARSES_DIR / base}.json/.txt",
                  file=sys.stderr)
        except Exception as e:
            print(f"[meter] couldn't save the rift report: {e}",
                  file=sys.stderr)
        try:
            render_rift_report_image(report, PARSES_DIR / f"{base}.png")
        except Exception as e:
            print(f"[meter] couldn't render the rift report image: {e}",
                  file=sys.stderr)

    @staticmethod
    def _load_last_rift_report():
        """The newest saved rift report, or None — how a fresh session still
        has a 'Last Rift Report'. Timestamped filenames sort lexicographically,
        so newest is just last. Validated for shape, not trusted: a truncated
        or hand-edited file costs the button, never the meter."""
        try:
            files = sorted(PARSES_DIR.glob("rift-*.json"))
            if not files:
                return None
            data = json.loads(files[-1].read_text(encoding="utf-8"))
            phases = data.get("phases")
            if (isinstance(data.get("at"), (int, float))
                    and isinstance(phases, list) and len(phases) == 2
                    and all(isinstance(ph, dict)
                            and isinstance(ph.get("players"), list)
                            and isinstance(ph.get("elements"), list)
                            for ph in phases)):
                return data
            print(f"[meter] ignoring malformed rift report {files[-1].name}",
                  file=sys.stderr)
        except Exception as e:
            print(f"[meter] couldn't load the last rift report: {e}",
                  file=sys.stderr)
        return None

    def _close_report(self):
        self._report_open = False
        self._refresh_visibility()

    def _copy_report(self):
        """Copy the report as an IMAGE — the leaderboard pastes into chat
        looking like the leaderboard, and one picture carries both phases.
        Rendered from the numbers (render_rift_report_image), never
        screenshotted, so the game behind the card can't bleed in. Falls back
        to the plaintext copy if Pillow or the clipboard declines — a Copy
        button that sometimes copies nothing is worse than one that
        occasionally copies text."""
        if not self._report_data:
            return
        flash = "Copied to clipboard"
        try:
            copy_image_to_clipboard(
                render_rift_report_image(self._report_data))
        except Exception as e:
            print(f"[meter] image copy failed ({e}) — copying text instead.",
                  file=sys.stderr)
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(
                    self._report_text(self._report_data))
            except tk.TclError as e2:
                print(f"[meter] couldn't copy the report: {e2}",
                      file=sys.stderr)
                return
            flash = "Copied as text"
        self._report_flash.config(text=flash)
        if self._report_flash_job is not None:
            try:
                self.root.after_cancel(self._report_flash_job)
            except tk.TclError:
                pass

        def clear():
            self._report_flash_job = None
            try:
                self._report_flash.config(text="")
            except tk.TclError:
                pass
        self._report_flash_job = self.root.after(1600, clear)

    def _report_text(self, data):
        """The plaintext version — chat-pasteable lines, no box drawing."""
        out = ["Farever+ Rift Report"]
        for ph in data["phases"]:
            out.append(f"== {ph['label']} — {self._mmss(ph['duration'])}, "
                       f"{int(ph['total']):,} dmg, {int(ph['heal']):,} heal"
                       + _overheal_note(ph, " ({:.0f}% overheal)")
                       + (f", {int(ph['boost']):,} boost"
                          if ph.get("boost", 0.0) > 0.5 else "")
                       + " ==")
            players = ph["players"]
            if not players:
                out.append("  (nothing recorded)")
                continue
            for i, p in enumerate(players[:5], 1):
                pct = p["total"] / ph["total"] * 100 if ph["total"] else 0.0
                out.append(f"  dmg {i}. {_report_name(p)} "
                           f"{int(p['total']):,} ({pct:.1f}%)")
            healers = sorted((p for p in players if p["heal"] > 0.5),
                             key=lambda p: -p["heal"])
            for i, p in enumerate(healers[:5], 1):
                pct = p["heal"] / ph["heal"] * 100 if ph["heal"] else 0.0
                out.append(f"  heal {i}. {_report_name(p)} "
                           f"{int(p['heal']):,} ({pct:.1f}%"
                           + _overheal_note(p, ", {:.0f}% over") + ")")
            # Who the game credited the buff damage to, by name: the number in
            # the header line is meaningless without knowing whose row it came
            # off. Usually exactly one player — see "Patch quirks".
            boosted = sorted((p for p in players if p.get("boost", 0.0) > 0.5),
                             key=lambda p: -p["boost"])
            for i, p in enumerate(boosted[:5], 1):
                out.append(f"  boost {i}. {_report_name(p)} "
                           f"{int(p['boost']):,}")
            if ph["elements"]:
                out.append("  types: " + " · ".join(
                    f"{'Other' if el == '?' else el} "
                    f"{amt / ph['total'] * 100 if ph['total'] else 0.0:.1f}%"
                    for el, amt in ph["elements"][:8]))
        return "\n".join(out)

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

    def _build_kill_toast(self):
        """The boss kill time — the same drop-shadowed floating text as the
        parse banner, on its own window because the two can be up at once
        (a parsed boss dying is the ordinary way a parse ends well)."""
        self._kill_font = self.fonts["ui_parse_b"]
        self._kill_canvas = tk.Canvas(self.killwin, bg=TRANSPARENT_KEY,
                                      highlightthickness=0, bd=0)
        self._kill_canvas.pack()
        self._kill_toast_job = None    # pending hide timer

    def _show_kill_toast(self, text, best):
        """Put the time on screen for KILL_TOAST_SECS. Gold when it set a
        record — the one moment the colour means something — body-coloured
        like the parse banner otherwise."""
        f, c = self._kill_font, self._kill_canvas
        pad, off = 8, 2
        w = f.measure(text) + pad * 2 + off
        h = f.metrics("linespace") + pad * 2 + off
        c.config(width=w, height=h)
        c.delete("all")
        c.create_text(pad + off, pad + off, text=text, font=f, fill=BG_BORDER,
                      anchor="nw")
        c.create_text(pad, pad, text=text, font=f,
                      fill=REPORT_MEDALS[0] if best else BG_BODY, anchor="nw")
        self.killwin.update_idletasks()
        l, t, r, _b = self._game_rect()
        self.killwin.geometry(f"+{l + ((r - l) - w) // 2}+{t + TOP_STRIP_KILL}")
        self.killwin.deiconify()
        self.killwin.attributes("-topmost", True)
        # A second kill inside the window restarts the clock rather than
        # letting the first one's timer take the new text down early.
        if self._kill_toast_job is not None:
            try:
                self.root.after_cancel(self._kill_toast_job)
            except Exception:
                pass
        self._kill_toast_job = self.root.after(int(KILL_TOAST_SECS * 1000),
                                               self._hide_kill_toast)

    def _hide_kill_toast(self):
        self._kill_toast_job = None
        self.killwin.withdraw()

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
            if fp.boost_total > 0.5:
                stats.append(f"{int(fp.boost_total)} boost")
            if fp.heal_total > 0.5:
                stats.append(f"{fp.overheal_pct:.0f}% overheal")
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
            # `boost` rides on every row and the renderer draws the column only
            # if some row has one — the image says what the overlay said.
            "rows": [{
                "name": p.name, "total": p.total, "heal": p.heal_total,
                "heal_self": p.heal_self, "overheal": p.overheal_pct,
                "boost": p.boost_total,
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
        the control menu ends by giving the game the foreground back.

        Except while a search box is being typed into — that is the one time
        the overlay is meant to hold the keyboard, and handing it back mid-word
        would eat the rest of what you were typing. Clicking a button on the
        menu therefore does NOT end typing (Tk doesn't move focus on a button
        click), which is what makes "type a name, hit the sort toggle, keep
        typing" work."""
        if self._typing:
            return
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

    def _set_typing(self, on):
        self._typing = bool(on)

    def _stop_typing(self):
        """Give the keyboard back to the game.

        Esc or Return in a search box, and every route by which the control
        menu leaves the screen. That second half is not optional: a _typing
        that outlived the window it belonged to would silently disable
        _refocus_game for the rest of the run. Cheap and idempotent, because
        _refresh_visibility calls it on every tick the menu is down."""
        if not self._typing:
            return
        self._typing = False
        try:
            self.menu.focus_set()   # off the Entry, before the game takes over
        except tk.TclError:
            pass
        self._refocus_game()

    def _on_row_click(self, name):
        # Same rule as the window's click-through, or the row would be
        # clickable-looking and inert while the mouse is free.
        if self._mouse_available() and name:
            self.focus_player = name

    def _set_win_clickthrough(self, win, enabled, activatable=False):
        if sys.platform != "win32":
            return
        hwnd = win.winfo_id()
        parent = ctypes.windll.user32.GetParent(hwnd)
        _set_clickthrough(parent or hwnd, enabled, activatable)

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
        # what asks DWM for the drop shadow that goes with it. Its badge
        # underlay is the same shape of window, so it sits this out too.
        if win in (self.riftwin, self.compasswin, self.badgewin):
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
        # The badge underlay carries the boxes' pixels, and a box is part of
        # the compass as far as the cursor cares — same signal, same answer.
        self._set_win_clickthrough(self.badgewin, pointable)
        # The hover box comes and goes with the same signal: it is only ever
        # useful when there's a cursor, and this is the one place both halves of
        # that answer (the escape menu and the freed mouse) are already known.
        self._sync_map_tip()
        # The control menu is always interactive (it is only ever shown while
        # the cursor is free); the floating hint and parse banner are text over
        # the game and must never take a click.
        #
        # It is also the one window allowed to ACTIVATE, because it is the one
        # window with a text field on it. Harmless here where it would not be
        # elsewhere: the menu is only ever on screen while the game's escape
        # menu holds the cursor, and _game_has_focus already counts our own
        # process as the game having focus, so taking the foreground does not
        # trip the alt-tab hiding rule. See _stop_typing for how it's handed
        # back.
        self._set_win_clickthrough(self.menu, False, activatable=True)
        self._set_win_clickthrough(self.hintwin, True)
        self._set_win_clickthrough(self.parsewin, True)
        self._set_win_clickthrough(self.killwin, True)
        # The prompt must take clicks whenever it's up, regardless of lock
        # state — it's the one overlay window that has to be answered.
        self._set_win_clickthrough(self.promptwin, False)
        # The rift report too: close and copy are its whole interface, and it
        # only ever appears the moment the fight (and the danger) is over.
        self._set_win_clickthrough(self.reportwin, False)

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
        # Bring the panel's contents up to date BEFORE it is shown. These used
        # to ride the 250 ms loop, which was invisible while the menu also
        # appeared on that loop — now that it opens promptly, a stale label
        # would be on screen for a moment and then change under the eye.
        # For the Social pages this is also one of only three load moments —
        # they don't poll; see _reload_social.
        if want:
            self._refresh_menu()
            self._reload_social()
        self._refresh_visibility()   # owns every window's target, menu included
        if want and not self._prompt_open:
            self._place_hint()       # after the map: it measures the window
        # Logged because it's the one state change with no keypress behind it —
        # if someone reports "the meter won't take my clicks", this line says
        # whether the game-menu signal is arriving at all.
        # The reaction time is on the line too. It is the overlay's own share
        # only — the stamp is taken when the hook's interceptor sees the game
        # open the window — so it says whether a menu that feels slow is us or
        # the game, which is otherwise pure guesswork.
        lag = ""
        if want:
            t = self.ui_state.take_unlock_stamp()
            if t is not None:
                lag = f" (reacted in {(time.monotonic() - t) * 1000:.0f} ms)"
        print(f"[meter] game menu {'open' if want else 'closed'} — overlay "
              f"{'unlocked' if not self._is_locked() else 'locked'}{lag}",
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
        # With an installer asset the notice does the whole job in place; a
        # tags-only release (or a from-source run, which has no installed
        # copy to replace) keeps the old open-the-browser behaviour.
        tail = ("Click here to update now." if self._can_self_update()
                else "Click here to download it.")
        self.warn_lbl.config(
            text=f"Farever+ {UPDATE['latest']} is available — you're running "
                 f"{VERSION}.  {tail}",
            fg=FG_WARN, cursor="hand2")
        self.warn_lbl.bind("<Button-1>", lambda _e: self._on_update_click())
        # The header button stops being a question at the same moment, however
        # the version was found — the automatic check never touched it before,
        # so it sat reading "Check updates" beside a notice line announcing the
        # answer. Skipped while a flash or a check owns the label.
        if self._upd_btn_after is None and not self._upd_checking:
            self.m_update.config(text=self._update_btn_label())

    def _check_updates_clicked(self):
        """The header's manual check. The result lands on the button itself,
        because "up to date" has nowhere else to appear — the notice line
        only exists for the other answer, and still takes over the moment a
        newer version is found, exactly as if the automatic check had won."""
        if self._upd_checking:
            return
        if os.environ.get("FAREVER_NO_UPDATE_CHECK"):
            # The env var means "never phone home"; a click doesn't outrank
            # it, but silence would read as a broken button.
            self._flash_update_btn("Checks disabled")
            return
        if UPDATE["latest"]:
            # Already found, so there is nothing left to ask GitHub — and the
            # button has stopped being a question. It now reads "Update to
            # X.Y.Z", so a click on it is the answer to that, not a request to
            # re-run a check whose result is already on the button.
            self._on_update_click()
            return
        self._upd_checking = True
        self._flash_update_btn("Checking ...", reset=False)

        def work():
            found = _latest_version()

            def finish():
                # On the Tk thread via the action queue, so UPDATE and the
                # button are only ever touched where the tick reads them.
                self._upd_checking = False
                if _record_newer(found):
                    self._flash_update_btn(f"{UPDATE['latest']} available")
                elif found:
                    print(f"[update] up to date (running {VERSION}).",
                          file=sys.stderr)
                    self._flash_update_btn("Up to date")
                else:
                    self._flash_update_btn("Check failed")

            self._enqueue(finish)()

        threading.Thread(target=work, daemon=True,
                         name="update-check-manual").start()

    def _flash_update_btn(self, msg, reset=True):
        """Show `msg` on the check button, returning to the resting label
        after a few seconds so the button stays a button."""
        self.m_update.config(text=msg)
        if self._upd_btn_after is not None:
            self.menu.after_cancel(self._upd_btn_after)
            self._upd_btn_after = None
        if reset:
            self._upd_btn_after = self.menu.after(
                UPDATE_BTN_FLASH_MS, self._reset_update_btn)

    def _reset_update_btn(self):
        """Back to the resting label — which is not always "Check updates".

        Once a version has been found the button stops being a question and
        becomes the action, so it says so. Leaving it reading "Check updates"
        meant the one control that already knew the answer still looked like
        the way to ask, and clicking it did nothing but repeat itself."""
        self._upd_btn_after = None
        if self._upd_checking:
            return
        self.m_update.config(text=self._update_btn_label())

    def _update_btn_label(self):
        if not UPDATE["latest"]:
            return "Check updates"
        # "Get" rather than "Update to" when we can't install it ourselves —
        # the click opens the download page, and the label should not promise
        # an update that is going to be a browser tab.
        verb = "Update to" if self._can_self_update() else "Get"
        return f"{verb} {UPDATE['latest']}"

    def _can_self_update(self):
        return IS_FROZEN and bool(UPDATE["asset"])

    def _open_url(self, url):
        import webbrowser
        try:
            webbrowser.open(url)
        except Exception as e:
            print(f"[meter] couldn't open {url}: {e}", file=sys.stderr)

    def _open_repo(self):
        self._open_url(REPO_URL)

    def _open_update(self):
        self._open_url(UPDATE["url"])

    def _on_update_click(self):
        if self._updating or self._upd_resolving:
            return
        # A version found from the TAG list carries no installer, and that says
        # nothing about whether one exists: /releases/latest 404s while a
        # release is being published, and fails outright on GitHub's
        # unauthenticated rate limit (~60/hour, and loading screens spend it).
        # Both land on the tag fallback. Sending an installed build to a
        # browser on that evidence is the wrong answer to "update me", so ask
        # once more before believing it.
        if IS_FROZEN and UPDATE["latest"] and not UPDATE["asset"]:
            self._resolve_asset_then_update()
            return
        if not self._can_self_update():
            self._open_update()
            return
        # One click, deliberately — unlike Quit, which still wants two.
        # Both end the meter, but they are not the same risk: Quit taken by
        # accident costs you the parse you were in the middle of, while this
        # comes back as the same meter a few seconds later. `_updating` is
        # what stops a double-click starting two downloads.
        self._start_self_update()

    def _resolve_asset_then_update(self):
        """Look the release up BY TAG, then update — or fall back honestly.

        Off the Tk thread: a click must not stall the overlay for the length of
        a network timeout. The button says what it is doing, because the gap
        between the click and the download starting is otherwise a click that
        appeared to do nothing.
        """
        self._upd_resolving = True
        self._flash_update_btn("Checking ...", reset=False)
        tag = UPDATE["latest"]

        def work():
            asset_url, size = None, 0
            try:
                rel = _fetch_json(UPDATE_API_RELEASE_TAG + str(tag))
                asset_url, size = _release_installer_asset(rel)
            except Exception as e:
                print(f"[update] no installer resolved for {tag}: {e}",
                      file=sys.stderr)

            def finish():
                self._upd_resolving = False
                if asset_url:
                    UPDATE["asset"], UPDATE["asset_size"] = asset_url, size
                    print(f"[update] installer found for {tag} on retry.",
                          file=sys.stderr)
                    self._start_self_update()
                    return
                # Genuinely nothing to install — this release has no Setup.exe
                # attached, or GitHub is still unreachable. The browser is the
                # only remaining answer.
                self._reset_update_btn()
                self._open_update()

            self._enqueue(finish)()

        threading.Thread(target=work, daemon=True,
                         name="update-asset-retry").start()

    def _start_self_update(self):
        """Download the new installer behind a small progress window, then ask
        whether to run it.

        The meter's own part deliberately ends at "downloaded and opened": a
        running exe can't be overwritten, so the install itself happens after
        this process is gone. Nothing waits around for that on our behalf —
        the installer is opened normally, we close, and Inno does the rest
        (including offering to start the meter again). See open_installer."""
        print(f"[update] self-update to {UPDATE['latest']} started.",
              file=sys.stderr)
        self._updating = True
        self._refresh_visibility()          # the whole overlay steps aside
        self._build_update_window()
        dest = UPDATE_DIR / f"FareverMeter-{UPDATE['latest']}-Setup.exe"
        self._dl = dl = {"done": 0, "total": int(UPDATE["asset_size"] or 0),
                         "err": None, "path": dest, "complete": False}
        url = UPDATE["asset"]

        def work():
            import urllib.request
            tmp = dest.with_suffix(".part")
            try:
                UPDATE_DIR.mkdir(parents=True, exist_ok=True)
                req = urllib.request.Request(url, headers={
                    "User-Agent": f"FareverMeter/{VERSION}"})
                with urllib.request.urlopen(req, timeout=30.0) as r:
                    total = int(r.headers.get("Content-Length") or 0)
                    if total:
                        dl["total"] = total
                    with open(tmp, "wb") as f:
                        while True:
                            chunk = r.read(256 * 1024)
                            if not chunk:
                                break
                            f.write(chunk)
                            dl["done"] += len(chunk)
                # A short download would hand the helper a broken installer;
                # better to find out here, where the browser fallback exists.
                expect = int(UPDATE["asset_size"] or 0)
                if expect and dl["done"] != expect:
                    raise OSError(f"download truncated ({dl['done']} of "
                                  f"{expect} bytes)")
                os.replace(tmp, dest)
                dl["complete"] = True
            except Exception as e:
                dl["err"] = str(e)
                try:
                    tmp.unlink()
                except OSError:
                    pass

        threading.Thread(target=work, daemon=True,
                         name="update-download").start()
        self._update_dl_tick()

    def _build_update_window(self):
        """A small centred panel of its own — every overlay window is hidden
        while the update runs, so this one belongs to none of their rules."""
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        outer = tk.Frame(win, bg=BG_BORDER, padx=2, pady=2)
        outer.pack(fill="both", expand=True)
        body = tk.Frame(outer, bg=BG_BODY, padx=18, pady=14)
        body.pack(fill="both", expand=True)
        tk.Label(body, text=f"Updating Farever+ to {UPDATE['latest']}",
                 bg=BG_BODY, fg=FG_VALUE,
                 font=("Segoe UI", 11, "bold")).pack(anchor="w")
        self._upd_lbl = tk.Label(body, text="Starting download ...",
                                 bg=BG_BODY, fg=FG_TEXT, justify="left",
                                 font=("Segoe UI", 9))
        self._upd_lbl.pack(anchor="w", pady=(6, 8))
        self._upd_bar = tk.Canvas(body, width=320, height=10,
                                  bg=BG_BAR_TRACK, highlightthickness=0)
        self._upd_bar.pack(fill="x")
        self._upd_fill = self._upd_bar.create_rectangle(
            0, 0, 0, 12, fill=BG_HEADER, width=0)
        # Kept so the download can turn this panel into the "ready to install?"
        # question without building a second window on top of it.
        self._upd_body = body
        self._upd_btns = None
        win.update_idletasks()
        win.geometry(f"+{(win.winfo_screenwidth() - win.winfo_width()) // 2}"
                     f"+{(win.winfo_screenheight() - win.winfo_height()) // 3}")
        self._updwin = win

    def _update_dl_tick(self):
        dl = self._dl
        if dl is None or self._updwin is None:
            return
        if dl["err"] is not None:
            self._update_failed(dl["err"])
            return
        if dl["complete"]:
            self._offer_installer(dl["path"])
            return
        done, total = dl["done"], dl["total"]
        if total:
            w = int(self._upd_bar.winfo_width() * min(1.0, done / total))
            self._upd_bar.coords(self._upd_fill, 0, 0, w, 12)
            self._upd_lbl.config(text=f"Downloading ... {done / 1048576:.1f} "
                                      f"of {total / 1048576:.1f} MB")
        else:
            self._upd_lbl.config(text=f"Downloading ... "
                                      f"{done / 1048576:.1f} MB")
        self.root.after(100, self._update_dl_tick)

    def _offer_installer(self, installer):
        """The download is done — ask before running it.

        The old flow went straight from "downloaded" to a silent install with
        nothing shown and nothing to agree to. Downloading is reversible;
        executing an installer that replaces the program you are running is
        not, so that is where the question belongs. Saying no keeps the file.
        """
        if self._upd_btns is not None:
            return                      # already asked
        self._upd_lbl.config(
            text=f"Farever+ {UPDATE['latest']} is downloaded.\n"
                 "The installer will open and the meter will close, so it can "
                 "let go of Farever cleanly first.")
        self._upd_bar.pack_forget()
        row = self._upd_btns = tk.Frame(self._upd_body, bg=BG_BODY)
        row.pack(fill="x", pady=(10, 0))

        def go():
            # Breadcrumb for the build that replaces this one: it has no other
            # way to know it arrived via the update button rather than a normal
            # launch, and only the former earns a "what's new" window. Written
            # before the handoff, because after it this process is on its way
            # out — but only once the user has actually said yes, or it would
            # greet the wrong version after a decline.
            try:
                UPDATED_MARKER.write_text(
                    json.dumps({"version": UPDATE["latest"],
                                "from": VERSION}), encoding="utf-8")
            except OSError as e:
                print(f"[update] couldn't leave the what's-new marker: {e}",
                      file=sys.stderr)
            try:
                open_installer(installer)
            except Exception as e:
                self._update_failed(f"couldn't open the installer: {e}")
                return
            # Quit AFTER launching: a process on its way out cannot reliably
            # start another one. The installer sits on its first wizard page
            # while we shut down, and if the user is quicker than we are, the
            # installer's own check asks them to stop the meter.
            print("[update] installer opened — closing the meter so it can "
                  "install.", file=sys.stderr)
            self._quit()

        def later():
            print(f"[update] install declined; installer kept at {installer}",
                  file=sys.stderr)
            if self._updwin is not None:
                self._updwin.destroy()
                self._updwin = None
            self._dl = None
            self._updating = False
            self._refresh_visibility()
            self.warn_lbl.config(
                text=f"Farever+ {UPDATE['latest']} is downloaded and waiting "
                     f"at {installer} — run it whenever you like.",
                fg=FG_WARN)

        tk.Button(row, text="Open the installer", command=go,
                  bg=BTN_ON_BG, fg=FG_HEADER, activebackground=BTN_ON_BG_ACTIVE,
                  activeforeground=FG_HEADER, relief="flat", bd=0,
                  padx=14, pady=6, cursor="hand2",
                  font=("Segoe UI", 9, "bold")).pack(side="left")
        tk.Button(row, text="Not now", command=later,
                  bg=BG_BODY_SOFT, fg=FG_TEXT, activebackground=BG_BAR_TRACK,
                  activeforeground=FG_VALUE, relief="flat", bd=0,
                  padx=14, pady=6, cursor="hand2",
                  font=("Segoe UI", 9)).pack(side="left", padx=(8, 0))

    def _update_failed(self, why):
        """Put the overlay back and point the notice at the manual path. The
        browser is NOT opened here — the common failure is being offline,
        where a browser tab helps nobody."""
        print(f"[update] self-update failed: {why}", file=sys.stderr)
        if self._updwin is not None:
            self._updwin.destroy()
            self._updwin = None
        self._dl = None
        self._updating = False
        self._refresh_visibility()
        self.warn_lbl.config(
            text=f"The automatic update didn't work ({why}) — click here to "
                 "get it from the releases page instead.")
        self.warn_lbl.bind("<Button-1>", lambda _e: self._open_update())

    def on_game_exit(self, reason=""):
        """The game process went away. Safe from any thread — the frida
        detached signal arrives on frida's own.

        This used to assume the overlay would have vanished by itself, on the
        grounds that a dead game can't hold the foreground. It doesn't: the
        focus rule treats our own windows as the game's, so raising the prompt
        below put the overlay straight back on screen, over the prompt. The
        overlay is now stood down explicitly, before the prompt exists.
        """
        self._enqueue(lambda: self._on_game_exit(reason))()

    def _on_game_exit(self, reason=""):
        self._game_gone = True
        # The hook died with the game, so the close events for whatever was
        # open are never coming — ui_state would keep reporting the escape
        # menu as open, and with it the control menu as unlocked, forever.
        self.ui_state.clear()
        self._menu_unlock = False
        # A parse whose data source just died is not a sample of anything, and
        # its banner maps itself directly rather than through the fade system —
        # so it would climb back over the prompt on the next tick. Ending it is
        # both the honest answer and the one that gets it off the screen.
        if self._parse_state is not None:
            self._stop_parse()
        self._refresh_visibility()      # stand everything down FIRST
        self._show_game_exit_prompt(reason)

    def _show_game_exit_prompt(self, reason=""):
        if self._game_exit_win is not None:
            return
        if self._updating:
            # Mid self-update the meter is about to exit and restart anyway;
            # a second dialog about the game would only compete with it.
            return
        win = tk.Toplevel(self.root)
        win.title("Farever+ Meter")
        win.attributes("-topmost", True)
        win.resizable(False, False)
        body = tk.Frame(win, bg=BG_BODY, padx=16, pady=14)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="Farever has stopped.",
                 bg=BG_BODY, fg=FG_VALUE,
                 font=("Segoe UI", 11, "bold")).pack(anchor="w")
        tk.Label(body,
                 text="The game has closed, so the meter has nothing left to "
                      "read.\nWould you like to exit the meter?",
                 bg=BG_BODY, fg=FG_TEXT, justify="left",
                 font=("Segoe UI", 9)).pack(anchor="w", pady=(6, 12))
        row = tk.Frame(body, bg=BG_BODY)
        row.pack(fill="x")

        def close(quit_now):
            self._game_exit_win = None
            try:
                win.destroy()
            except tk.TclError:
                pass
            if quit_now:
                self._quit()

        # No second-click arming here, unlike the Quit button: with the game
        # gone there is no encounter left for a misclick to destroy.
        tk.Button(row, text="Exit the meter", command=lambda: close(True),
                  bg=FG_WARN, fg=FG_HEADER, activebackground=FG_WARN,
                  activeforeground=FG_HEADER, relief="flat", bd=0,
                  padx=12, pady=6, cursor="hand2",
                  font=("Segoe UI", 9, "bold")).pack(side="left")
        tk.Button(row, text="Keep it running", command=lambda: close(False),
                  bg=BG_BODY_SOFT, fg=FG_TEXT, activebackground=BG_BAR_TRACK,
                  activeforeground=FG_VALUE, relief="flat", bd=0,
                  padx=12, pady=6, cursor="hand2",
                  font=("Segoe UI", 9)).pack(side="left", padx=(8, 0))
        win.protocol("WM_DELETE_WINDOW", lambda: close(False))
        win.update_idletasks()
        win.geometry(f"+{(win.winfo_screenwidth() - win.winfo_width()) // 2}"
                     f"+{(win.winfo_screenheight() - win.winfo_height()) // 3}")
        self._game_exit_win = win

    def check_whats_new(self):
        """If this build arrived through the update button, show its notes once.

        The marker is consumed no matter what happens next: one that outlived
        its update would greet every launch from here on, and a window nobody
        asked for is worse than no window."""
        try:
            marker = json.loads(UPDATED_MARKER.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return                      # a normal launch, which is most of them
        except Exception as e:
            print(f"[update] unreadable what's-new marker ({e}); ignoring.",
                  file=sys.stderr)
            marker = {}
        try:
            UPDATED_MARKER.unlink()
        except OSError:
            pass
        if marker.get("version") != VERSION:
            # The update didn't land, or something else replaced the build.
            # Either way these notes would describe the wrong version.
            return
        print(f"[update] updated from {marker.get('from')} to {VERSION} — "
              "fetching the release notes.", file=sys.stderr)

        def work():
            body = _fetch_release_notes(VERSION)
            if body:
                self._enqueue(lambda: self._show_whats_new(body))()
            else:
                print("[update] no release notes to show.", file=sys.stderr)

        threading.Thread(target=work, daemon=True, name="whats-new").start()

    def _show_whats_new(self, body):
        """A dismissable panel of its own, like the game-exit prompt — it has
        to be readable with the game in any state, so it follows none of the
        overlay's visibility rules."""
        if self._whats_new_win is not None:
            return
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        outer = tk.Frame(win, bg=BG_BORDER, padx=2, pady=2)
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer, bg=BG_HEADER_UNLOCKED)
        header.pack(fill="x")
        title = tk.Label(header, text=f"Farever+ is now {VERSION}",
                         bg=BG_HEADER_UNLOCKED, fg=FG_HEADER,
                         font=self.fonts_m["ui_b"], anchor="w", padx=10, pady=5)
        title.pack(side="left")
        self._bind_drag(win, (header, title))

        body_fr = tk.Frame(outer, bg=BG_BODY, padx=14, pady=12)
        body_fr.pack(fill="both", expand=True)

        text_fr = tk.Frame(body_fr, bg=BG_BODY)
        text_fr.pack(fill="both", expand=True)
        scroll = tk.Scrollbar(text_fr, orient="vertical")
        scroll.pack(side="right", fill="y")
        txt = tk.Text(text_fr, wrap="word", width=64, height=22,
                      bg=BG_BODY, fg=FG_TEXT, relief="flat", bd=0,
                      padx=4, pady=2, font=("Segoe UI", 9),
                      yscrollcommand=scroll.set, cursor="arrow")
        txt.pack(side="left", fill="both", expand=True)
        scroll.config(command=txt.yview)
        txt.insert("1.0", _markdown_to_text(body))
        # Read-only, but still selectable and scrollable — `state="disabled"`
        # is the usual trick and it kills the mouse wheel too.
        txt.bind("<Key>", lambda _e: "break")

        row = tk.Frame(body_fr, bg=BG_BODY)
        row.pack(fill="x", pady=(10, 0))

        def close():
            self._whats_new_win = None
            try:
                win.destroy()
            except tk.TclError:
                pass

        tk.Button(row, text="Got it", command=close,
                  bg=BG_HEADER_UNLOCKED, fg=FG_HEADER,
                  activebackground=BG_HEADER_UNLOCKED,
                  activeforeground=FG_HEADER, relief="flat", bd=0,
                  padx=14, pady=6, cursor="hand2",
                  font=("Segoe UI", 9, "bold")).pack(side="right")
        win.protocol("WM_DELETE_WINDOW", close)
        win.bind("<Escape>", lambda _e: close())
        win.update_idletasks()
        win.geometry(f"+{(win.winfo_screenwidth() - win.winfo_width()) // 2}"
                     f"+{(win.winfo_screenheight() - win.winfo_height()) // 4}")
        self._whats_new_win = win

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

    def _rebuild_mounts(self, kinds):
        """The favorites checklist, rebuilt whenever the hook's unlock list
        changes (including from empty, at attach). Three columns keep ~30
        mounts to ~10 rows, so the Mounts tab stays in the same size class as
        the other pages."""
        for w in self.mounts_box.winfo_children():
            w.destroy()
        self._mount_vars = {}
        if not kinds:
            tk.Label(self.mounts_box,
                     text=("Waiting for the game — the list fills in once "
                           "the meter is attached and your hero has loaded."),
                     bg=BG_BODY, fg=FG_DIM, font=self.fonts_m["ui_tiny_i"],
                     anchor="w", justify="left",
                     wraplength=WARN_WRAP).grid(row=0, column=0, sticky="w")
            return
        # Two columns, ordered by what the labels SAY: real display names are
        # longer than the old ids (three columns overflowed the panel), and a
        # list sorted by backend id looks shuffled once the labels don't
        # start with the same words.
        cols = 2
        kinds = sorted(kinds, key=lambda k: _mount_label(k).lower())
        for i, kind in enumerate(kinds):
            var = tk.BooleanVar(value=kind in self._mount_favs)
            cb = tk.Checkbutton(
                self.mounts_box, text=_mount_label(kind), variable=var,
                command=self._enqueue(lambda k=kind: self._toggle_mount_fav(k)),
                bg=BG_BODY, fg=FG_TEXT, activebackground=BG_BODY,
                activeforeground=FG_VALUE, selectcolor=BG_BODY_SOFT,
                font=self.fonts_m["ui"], anchor="w",
                highlightthickness=0, bd=0, cursor="hand2")
            cb.grid(row=i // cols, column=i % cols, sticky="w", padx=(0, 10))
            self._mount_vars[kind] = var

    def _toggle_mount_random(self):
        self._mount_random = not self._mount_random
        self._save_settings()
        self._push_mount_cfg()

    def _on_mount_mode_pick(self, value):
        # Queued like every other dropdown: it mutates state the refresh
        # loop reads, and Tk isn't thread-safe.
        self._enqueue(lambda: self._set_mount_mode(value))()

    def _set_mount_mode(self, value):
        if value not in MOUNT_MODES:
            return
        self._mount_mode = value
        self._save_settings()
        self._push_mount_cfg()

    def _toggle_mount_fav(self, kind):
        # The Checkbutton's var has already flipped by the time this runs on
        # the queue — sync the set from it rather than toggling blind.
        var = self._mount_vars.get(kind)
        if var is None:
            return
        if var.get():
            self._mount_favs.add(kind)
        else:
            self._mount_favs.discard(kind)
        self._save_settings()
        self._push_mount_cfg()

    def _push_mount_cfg(self):
        """Hand the standing mount config to the hook. Also called once at
        startup (main), because the agent boots with the feature off."""
        self._configure(mounts={"enabled": bool(self._mount_random),
                                "mode": self._mount_mode.lower(),
                                "favorites": sorted(self._mount_favs)})

    def _rebuild_gliders(self, kinds):
        """The glider favorites checklist — same layout rules as
        _rebuild_mounts (two columns, sorted by display name)."""
        for w in self.gliders_box.winfo_children():
            w.destroy()
        self._glider_vars = {}
        if not kinds:
            tk.Label(self.gliders_box,
                     text=("Waiting for the game — the list fills in once "
                           "the meter is attached and your hero has loaded."),
                     bg=BG_BODY, fg=FG_DIM, font=self.fonts_m["ui_tiny_i"],
                     anchor="w", justify="left",
                     wraplength=WARN_WRAP).grid(row=0, column=0, sticky="w")
            return
        cols = 2
        kinds = sorted(kinds, key=lambda k: _glider_label(k).lower())
        for i, kind in enumerate(kinds):
            var = tk.BooleanVar(value=kind in self._glider_favs)
            cb = tk.Checkbutton(
                self.gliders_box, text=_glider_label(kind), variable=var,
                command=self._enqueue(
                    lambda k=kind: self._toggle_glider_fav(k)),
                bg=BG_BODY, fg=FG_TEXT, activebackground=BG_BODY,
                activeforeground=FG_VALUE, selectcolor=BG_BODY_SOFT,
                font=self.fonts_m["ui"], anchor="w",
                highlightthickness=0, bd=0, cursor="hand2")
            cb.grid(row=i // cols, column=i % cols, sticky="w", padx=(0, 10))
            self._glider_vars[kind] = var

    def _toggle_glider_random(self):
        self._glider_random = not self._glider_random
        self._save_settings()
        self._push_glider_cfg()

    def _on_glider_mode_pick(self, value):
        # Queued like every other dropdown: it mutates state the refresh
        # loop reads, and Tk isn't thread-safe.
        self._enqueue(lambda: self._set_glider_mode(value))()

    def _set_glider_mode(self, value):
        if value not in MOUNT_MODES:
            return
        self._glider_mode = value
        self._save_settings()
        self._push_glider_cfg()

    def _toggle_glider_fav(self, kind):
        # The Checkbutton's var has already flipped by the time this runs on
        # the queue — sync the set from it rather than toggling blind.
        var = self._glider_vars.get(kind)
        if var is None:
            return
        if var.get():
            self._glider_favs.add(kind)
        else:
            self._glider_favs.discard(kind)
        self._save_settings()
        self._push_glider_cfg()

    def _push_glider_cfg(self):
        """Hand the standing glider config to the hook. Also called once at
        startup (main), because the agent boots with the feature off."""
        self._configure(gliders={"enabled": bool(self._glider_random),
                                 "mode": self._glider_mode.lower(),
                                 "favorites": sorted(self._glider_favs)})

    def _sort_btn_text(self):
        return "▼ Healing" if self._sort_heal else "▼ Damage"

    def _toggle_sort(self):
        self._sort_heal = not self._sort_heal
        self.sort_btn.config(text=self._sort_btn_text())
        self._save_settings()

    def _toggle_heal(self):
        self._show_heal = not self._show_heal
        # The sort toggle lives and dies with the healing columns: turning
        # them off while heal-sorted snaps the order back to damage, or the
        # rows would sit in an order nothing on screen explains.
        if self._show_heal:
            self.sort_btn.pack(side="right")
        else:
            if self._sort_heal:
                self._sort_heal = False
                self.sort_btn.config(text=self._sort_btn_text())
            self.sort_btn.pack_forget()
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
        # The tab strip and the Social viewport are fixed pixel sizes with
        # propagation off, so nothing else would ever resize them — at 150% the
        # navbar would keep clipping its own labels.
        self.menu_nav.config(width=int(MENU_NAV_W * self._scales["menu"]))
        for c in (self.social_canvas, self.session_canvas):
            c.config(height=int(SOCIAL_LIST_H * self._scales["menu"]))
        self.social_note.config(
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

    def _toggle_map_bg(self):
        self._map_bg_on = not self._map_bg_on
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

    def auto_reset_boss(self) -> bool:
        """Read by the hook thread, so it stays a plain attribute read."""
        return bool(self._auto_reset_boss)

    def on_boss_pull(self):
        """A boss (not an elite) raised its healthbar and the meter was reset.
        Called from the hook thread — everything real happens on the Tk one."""
        self._enqueue(lambda: self.sounds.play("pull"))()

    def on_boss_kill(self):
        """A boss died: its bar went down with its last health reading at 0."""
        self._enqueue(lambda: self.sounds.play("victory"))()

    def on_boss_timed_kill(self, kinds, secs):
        """The LAST boss bar went down killed — the fight is formally over and
        its clock has a reading. Called from the hook thread alongside the
        victory cue; the record and the toast belong to the Tk one.

        Not opt-in, deliberately: a record you had to switch on beforehand is
        a record you don't have when you finally want it."""
        self._enqueue(lambda: self._record_boss_kill(tuple(kinds), secs))()

    def _record_boss_kill(self, kinds, secs):
        """Compare the kill against the stored best and say so on screen.

        The key is the PULL's boss kinds, sorted and joined — stable for a
        council pulled together (whichever member dies last), and for the
        Nightqueen it is her alone, because her copies never fire a second
        pull edge. The killed bar's kind would be neither."""
        if not kinds:
            # A bar with no kind can't key a record, but the time is still
            # worth saying — it just can't be compared to anything.
            self._show_kill_toast(f"BOSS DOWN  {self._mmss(secs)}", best=False)
            return
        # The record keys on the internal kind — stable across localization
        # and any rename the cdb ships — but the toast speaks the game's
        # language: the kind is routinely not the name on the bar (the first
        # live kill said "CLEODORA" for a boss the game calls Honeyzabeth).
        key = "+".join(kinds)
        name = " + ".join(_boss_label(k) for k in kinds).upper()
        prev = self._best_times.get(key)
        if prev is None:
            self._best_times[key] = secs
            self._save_best_times()
            text = f"{name} DOWN  {self._mmss(secs)} — FIRST RECORDED KILL"
            best = True
        elif secs < prev:
            self._best_times[key] = secs
            self._save_best_times()
            text = (f"{name} DOWN  {self._mmss(secs)} — NEW BEST "
                    f"(was {self._mmss(prev)})")
            best = True
        else:
            text = f"{name} DOWN  {self._mmss(secs)} — BEST {self._mmss(prev)}"
            best = False
        print(f"[meter] boss kill timed: {key} {secs:.1f}s"
              + (f" (best {self._best_times[key]:.1f}s)"), file=sys.stderr)
        self._show_kill_toast(text, best)

    @staticmethod
    def _load_best_times():
        """The record book, tolerantly: a missing file is an empty one, and a
        hand-edited or corrupt entry drops rather than crashing the launch."""
        try:
            d = json.loads(BEST_TIMES_CACHE.read_text())
            return {str(k): float(v) for k, v in d.items()
                    if isinstance(v, (int, float)) and v > 0}
        except Exception:
            return {}

    def _save_best_times(self):
        try:
            BEST_TIMES_CACHE.parent.mkdir(parents=True, exist_ok=True)
            BEST_TIMES_CACHE.write_text(json.dumps(self._best_times, indent=1))
        except OSError as e:
            print(f"[meter] couldn't save best times: {e}", file=sys.stderr)

    def on_legendary_pickup(self):
        """A legendary weapon appeared in the loadout that wasn't there before.
        Called from the hook thread — the cue belongs to the Tk one."""
        self._enqueue(lambda: self.sounds.play("legendary"))()

    def _toggle_sounds(self):
        self._sounds_on = not self._sounds_on
        self.sounds.set_enabled(self._sounds_on)
        self._save_settings()
        # Play the pull cue as confirmation when switching them ON, so the
        # checkbox proves the audio path works instead of leaving you to pull a
        # boss to find out. Nothing on the way off, for obvious reasons.
        if self._sounds_on:
            self.sounds.play("pull")

    def _toggle_auto_reset_boss(self):
        self._auto_reset_boss = not self._auto_reset_boss
        self._save_settings()

    def _apply_map_zoom(self):
        """Turn the zoom percentage into the range the draw pass reads.

        Zooming IN means seeing less ground, so range is inversely proportional
        to zoom. Clamped to the documented floor/ceiling rather than trusted:
        the value comes from a saved settings file that a user can edit.
        """
        self._map_range = max(MINIMAP_RANGE_MIN,
                              min(MINIMAP_RANGE_MAX,
                                  MINIMAP_RANGE * 100.0 / max(1, self._map_zoom)))

    def _icon_scale(self):
        """The multiplier every marker radius is drawn through.

        MINIMAP_ICON_SCALE is the shipped baseline, the user's percentage rides
        on top of it, and the minimap's own window scale is the last term. One
        product used everywhere means the style table's RATIOS survive all
        three — which is the point: markers are tuned against each other, not
        against the panel.
        """
        return (MINIMAP_ICON_SCALE * (self._map_icons / 100.0)
                * self._scales["minimap"])

    def _on_map_zoom_pick(self):
        self._enqueue(lambda: self._set_map_zoom(self._map_zoom_var.get()))()

    def _set_map_zoom(self, pct):
        pct = max(MINIMAP_ZOOM_MIN, min(MINIMAP_ZOOM_MAX, int(pct)))
        if pct == self._map_zoom:
            return
        self._map_zoom = pct
        self._apply_map_zoom()
        # Nothing to redraw by hand: the map is repainted wholesale on the next
        # tick and reads _map_range as it goes.
        self._save_settings()

    def _on_map_icons_pick(self):
        self._enqueue(lambda: self._set_map_icons(self._map_icons_var.get()))()

    def _set_map_icons(self, pct):
        pct = max(MINIMAP_ICONS_MIN, min(MINIMAP_ICONS_MAX, int(pct)))
        if pct == self._map_icons:
            return
        self._map_icons = pct
        self._save_settings()

    def _on_volume_pick(self):
        self._enqueue(lambda: self._set_volume(self._volume_var.get()))()

    def _set_volume(self, pct):
        pct = max(0, min(SOUND_VOLUME_MAX, int(pct)))
        if pct == self._sound_volume:
            return
        self._sound_volume = pct
        self.sounds.set_volume(pct)
        self._save_settings()

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
        # Once the game is gone, every window here is a readout of something
        # that no longer exists, and the exit prompt is the only thing left
        # with anything to say. Hide the lot — ahead of every other rule,
        # because two of them actively argue for showing things:
        #
        #   * _game_has_focus() counts OUR OWN process as the game having
        #     focus (Tk's dropdowns are separate windows and would otherwise
        #     hide the menu you opened them from). The exit prompt is one of
        #     our windows, so raising it made the overlay think the game was
        #     back in the foreground and un-hid everything.
        #   * ui_state never learns the game's windows closed — the hook died
        #     with the process, so no close events arrive and the escape menu
        #     stays "open" forever, holding the control menu unlocked.
        #
        # Both were reported as the menu rendering over the exit prompt.
        if self._game_gone:
            self._stop_typing()
            changed = False
            for key in self._fade_win:
                changed |= self._want_visible(key, False)
            if changed:
                self._start_fade()
            # Not a faded window, so it has to be told separately.
            try:
                self.parsewin.withdraw()
            except tk.TclError:
                pass
            return
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
        # ...and while the self-update runs, everything yields to its progress
        # window — the overlay is about to be replaced, not consulted.
        blanket = (menu_hidden or self._prompt_open or not self._focused
                   or self._updating)
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
            # The game puts its boss/elite healthbar across the top of the
            # screen, which is exactly where the compass sits. Unconditional,
            # like the rift rule above: the two are fighting for the same
            # pixels, and during a boss pull the game's bar is the one you
            # want. The escape menu still brings it back, so neither is ever
            # unreachable while a long fight is running.
            #
            # The minimap goes with it. Not because it collides with anything —
            # it sits in a corner — but because a boss pull is the one time you
            # are looking at the fight and not at where to go next, and the two
            # navigation panels leaving together reads as one deliberate "get
            # out of the way" rather than half the overlay flickering off.
            if key in BOSS_HIDDEN and self.ui_state.boss_bar_up() \
                    and not self._menu_unlock:
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
                        and self._focused and not menu_hidden
                        and not self._updating)
        # Whatever route the menu leaves by — Esc, alt-tab, the game opening
        # something over it — its dropdowns leave with it, and so does the
        # keyboard if a search box was holding it.
        if not menu_visible:
            self._unpost_menus()
            self._stop_typing()
        changed |= self._want_visible("menu", menu_visible)
        changed |= self._want_visible("hint", menu_visible)
        changed |= self._want_visible("prompt", self._prompt_open)
        # The offer lives and dies with the free cursor that makes it
        # answerable: press Escape again and it steps aside with everything
        # else, then comes back with the cursor. It is still "open" throughout
        # — only answering it retires it.
        changed |= self._want_visible(
            "update",
            self._update_offer_open and self._cursor_free and self._focused
            and not self._updating and not self._prompt_open)
        # The report follows the blanket rules (alt-tab, the game's own
        # screens, the modal prompt) but not the out-of-combat one — the boss
        # just died, so out-of-combat is precisely when it exists. It stays up
        # until its ✕ is clicked; a card that vanished on its own before you
        # could read the numbers would be worse than no card.
        changed |= self._want_visible("report",
                                      self._report_open and not blanket)
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
        win = self._fade_win[key]
        # Zero fade means this window is driven by a keypress and has to be on
        # screen in the same frame. Handled here rather than as a one-step fade
        # because the driver only wakes every FADE_STEP_MS — going through it
        # would put a tick of nothing between the key and the panel, which is
        # the delay this exists to avoid.
        if self._fade_secs[key] <= 0:
            self._alpha[key] = self._alpha_for(key) if visible else 0.0
            win.attributes("-alpha", self._alpha[key])
            if visible:
                win.deiconify()
                win.attributes("-topmost", True)
            else:
                win.withdraw()
            if key == "compass":
                self._sync_badgewin()
            return True
        if visible:
            win.attributes("-alpha", self._alpha[key])
            win.deiconify()
            win.attributes("-topmost", True)   # re-assert over the game's UI
        if key == "compass":
            self._sync_badgewin()
        return True

    def _start_fade(self):
        if self._fade_job is None:
            self._fade_job = self.root.after(FADE_STEP_MS, self._step_fade)

    # ---- rebinding the reset key ----
    def _begin_bind_capture(self):
        """Listen for the next keypress and make it the reset bind.

        POLLED, not bound. The obvious version — focus the button and take a
        Tk <KeyPress> — silently never fires unless something has deliberately
        claimed the keyboard first: the overlay windows are overrideredirect,
        and every one of them except the control menu carries WS_EX_NOACTIVATE
        precisely so that clicking it can't steal focus from the game, which
        also means it never receives a keystroke. The menu is the exception
        only while a Social search box has focus (see _stop_typing) — and
        _set_menu_tab ends that on the way to this tab, so by the time you are
        looking at this button the keyboard belongs to Farever again.

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
        self._sync_badgewin()
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
            secs = self._fade_secs[key]
            if secs <= 0:
                # A no-fade window is normally settled by _want_visible and
                # never reaches here. If anything else moves its target, snap —
                # never divide by zero working out a step size.
                a = target
            else:
                step = OVERLAY_ALPHA * FADE_STEP_MS / (secs * 1000)
                a = (min(target, a + step) if target > a
                     else max(target, a - step))
            self._alpha[key] = a
            win.attributes("-alpha", a)
            if a <= 0.0:
                win.withdraw()
            else:
                fading = True
            if key == "compass":
                self._sync_badgewin()
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
        """Merge a skill-id table by display name (e.g. all weapons' base
        "Attack" share one row) and return [(label, total, hits, crits, self)]
        sorted by total desc.

        Damage rows carry [hits, total, crits] and healing rows carry a fourth
        column (the self-healed share). Both go through here, so the fourth is
        read optionally and damage simply reports 0 — a damage bar has nothing
        to split."""
        merged: dict[str, list] = defaultdict(lambda: [0, 0.0, 0, 0.0])
        for sid, vals in table.items():
            label = self.session.skill_names.get(sid) or _pretty_id(sid)
            m = merged[label]
            m[0] += vals[0]; m[1] += vals[1]; m[2] += vals[2]
            m[3] += vals[3] if len(vals) > 3 else 0.0
        out = [(label, v[1], v[0], v[2], v[3]) for label, v in merged.items()]
        out.sort(key=lambda t: -t[1])
        return out[:MAX_SKILL_ROWS]

    def _apply_heal_columns(self):
        """Show/hide every optional piece of meter chrome in one go: the
        healing columns (the meter's HEAL header, and the breakdown's HEALING
        list with its divider) and the patch-quirk BOOST header. Guarded on the
        last applied state — re-packing widgets on every 250 ms tick would
        flicker."""
        state = (self._show_heal, self._show_boost)
        if state == self._cols_shown:
            return
        # The header line carries both; only the healing half owns widgets, so
        # a boost-only change redraws the header and stops there.
        heal_changed = self._cols_shown[0] != self._show_heal
        self._cols_shown = state
        self.cols_lbl.config(text=self._meter_cols_text())
        if not heal_changed:
            return
        if self._show_heal:
            self.col_sep.pack(side="left", fill="y", padx=6)
            self.heal_col.f.pack(side="left", anchor="n")
        else:
            self.col_sep.pack_forget()
            self.heal_col.f.pack_forget()

    # ---- Social tab ----------------------------------------------------
    def _reload_social(self):
        """Load both Social pages, now, once.

        This used to ride the refresh tick, gated on the menu being mapped,
        with the signature checks as the churn guard. The guard was the wrong
        one: on a busy shard the roster genuinely changes every few seconds
        and the session page's "seen" column ticks over on its own, so the
        signatures kept missing and the menu spent its time destroying and
        rebuilding row widgets — reported as the whole window going slow
        whenever the Social tab was open. The roster is a page you READ, not
        a feed. It now loads at the three moments a page should: the menu
        opening, the Social tab being raised, and the Refresh button. The
        world data underneath accumulates regardless — a reload is a read of
        what's already there, not a query.

        Both pages, not just the visible one: the session log grows whether
        or not you are looking at it, and reloading only on page switch would
        show a stale count the moment you arrived. Both stay signature-gated,
        so a reload that changes nothing redraws nothing.
        """
        self._rebuild_social()
        self._rebuild_session()

    def _refresh_social_clicked(self):
        """The Refresh button. The reload is silent when nothing changed —
        which after a click reads as a dead button — so the note answers
        every press, whether or not the list moved."""
        self._reload_social()
        self._social_note("Refreshed.", transient=True)

    def _set_social_page(self, key):
        """Raise one of the Social sub-pages and paint its tab."""
        if key not in self._social_page_frames:
            return
        # Each page has its own search box, and the one being left keeps Tk's
        # focus through the raise — same trap as _set_menu_tab.
        self._stop_typing()
        self._social_page = key
        self._social_page_frames[key].tkraise()
        for k, b in self._social_page_btns.items():
            self._paint_tab_btn(b, k == key)
        # The note is shared, so it has to re-answer for the page now on top.
        self._social_note(self._social_idle_note())

    def _sorted_roster(self):
        """The roster in the order the Social tab should show it.

        Name is a directory: you go to the top, then everyone alphabetically.
        Level is a ranking, highest first, and pins nobody — putting yourself
        above a level 25 because you happen to be you would make the column
        say something untrue. Name is the tie-break in both, so equal levels
        keep a stable order instead of shuffling on every rebuild.
        """
        rows = self.world.roster()
        if self._social_sort == "level":
            # Missing level (a player on the layer whose entity has not been
            # built yet) sorts last rather than as level 0 — it is unknown, not
            # low, and floating them to the bottom keeps the ranking readable.
            rows.sort(key=lambda r: (r.get("lvl") is None,
                                     -(r.get("lvl") or 0),
                                     (r.get("n") or "").lower()))
        else:
            rows.sort(key=lambda r: (not r.get("me"),
                                     (r.get("n") or "").lower()))
        return rows

    def _toggle_social_sort(self):
        i = SOCIAL_SORTS.index(self._social_sort)
        self._social_sort = SOCIAL_SORTS[(i + 1) % len(SOCIAL_SORTS)]
        self.btn_social_sort.config(
            text=SOCIAL_SORT_LABEL[self._social_sort])
        self._save_settings()
        self._rebuild_social()

    def _toggle_session_sort(self):
        i = SESSION_SORTS.index(self._session_sort)
        self._session_sort = SESSION_SORTS[(i + 1) % len(SESSION_SORTS)]
        self.btn_session_sort.config(
            text=SESSION_SORT_LABEL[self._session_sort])
        self._save_settings()
        self._rebuild_session()

    def _social_note(self, text, transient=False):
        """The line under the list: empty-state explanation, or a confirmation.

        A copy is silent by nature — nothing on screen changes when the
        clipboard does — so the confirmation is the only feedback that the
        click did anything."""
        # A confirmation holds the line for its full two and a half seconds.
        # Without this a roster change — which happens every time anyone zones,
        # and rebuilds both pages — would call back in here with the idle text
        # and blank the "Copied ..." the user is still reading.
        if self._social_note_transient and not transient:
            return
        if self._social_note_job is not None:
            try:
                self.root.after_cancel(self._social_note_job)
            except Exception:
                pass
            self._social_note_job = None
        self._social_note_transient = transient
        self.social_note.config(text=text,
                                fg=ACCENT if transient else FG_DIM)
        if transient:
            def restore():
                self._social_note_transient = False
                self._social_note(self._social_idle_note())
            self._social_note_job = self.root.after(2500, restore)

    def _social_idle_note(self):
        if self._social_page == "session":
            if not self.world.seen_players():
                return ("Nobody logged yet — players are added here as they "
                        "appear on your shard, and the list lasts until the "
                        "meter is closed.")
            return ""
        if not self.world.roster():
            return ("Waiting for the roster — it arrives a second or two after "
                    "the hook attaches and you are loaded into a zone.")
        return ""

    def _rebuild_social(self, *_a):
        """Redraw the roster rows, but only when something actually changed.

        Rows are destroyed and rebuilt wholesale rather than diffed. At shard
        size (tens, not thousands) that is far simpler than reconciling a list
        whose members appear and vanish as people zone, and it runs only on a
        genuine change — so the cost is paid when someone joins, not per tick.
        """
        rows = self._sorted_roster()
        q = self._social_query.get().strip().lower()
        sig = (q, self._social_sort,
               tuple((r.get("n"), r.get("k"), r.get("lvl"), r.get("uid"),
                      r.get("me")) for r in rows))
        if sig == self._social_sig:
            return
        self._social_sig = sig

        for w in self._social_row_widgets:
            w.destroy()
        self._social_row_widgets = []

        # Search matches name OR class, so "mage" filters to a class and "bru"
        # to a person without needing two boxes.
        shown = [r for r in rows
                 if not q
                 or q in (r.get("n") or "").lower()
                 or q in (r.get("k") or "").lower()]
        for r in shown:
            self._social_row_widgets.append(
                self._build_social_row(self.social_list, r, detail=True))

        total = len(rows)
        self.social_count.config(
            text=(f"{len(shown)} / {total}" if q
                  else f"{total} player{'' if total == 1 else 's'}"))
        if rows and not shown and self._social_page == "shard":
            self._social_note("Nobody here matches that.")
        else:
            self._social_note(self._social_idle_note())
        self._resize_scroll(self.social_canvas, self.social_list)

    def _rebuild_session(self, *_a):
        """Redraw the session log. Same signature-gating as the shard page.

        No class or level column: those come off a live ent.Hero, and a player
        who has left your shard no longer has one — so the honest thing to show
        is a name and an id, not a snapshot of what they were an hour ago.
        """
        rows = self.world.seen_players()
        now = time.monotonic()
        # Resolved once, here, so the sort and the column can never disagree
        # about how long ago something was.
        for r in rows:
            r["ago"] = _seen_ago(max(0.0, now - r.get("last", now)))
        if self._session_sort == "name":
            rows.sort(key=lambda r: (r.get("n") or "").lower())
        else:
            # Most recent first. Name breaks ties, and everyone still on the
            # shard shares one timestamp, so the present block is alphabetical
            # and holds still instead of churning every sweep.
            rows.sort(key=lambda r: (-r.get("last", 0.0),
                                     (r.get("n") or "").lower()))
        q = self._session_query.get().strip().lower()
        # `ago` is in the signature: when a row ticks from "now" to "1m" the
        # list has genuinely changed and must be redrawn.
        sig = (q, self._session_sort,
               tuple((r.get("n"), r.get("uid"), r["ago"]) for r in rows))
        if sig == self._session_sig:
            return
        self._session_sig = sig

        for w in self._session_row_widgets:
            w.destroy()
        self._session_row_widgets = []

        shown = [r for r in rows if not q or q in (r.get("n") or "").lower()]
        for r in shown:
            self._session_row_widgets.append(
                self._build_social_row(self.session_list, r, detail=False,
                                       seen_text=r["ago"]))

        total = len(rows)
        self.session_count.config(
            text=(f"{len(shown)} / {total}" if q
                  else f"{total} seen"))
        if rows and not shown and self._social_page == "session":
            self._social_note("Nobody here matches that.")
        else:
            self._social_note(self._social_idle_note())
        self._resize_scroll(self.session_canvas, self.session_list)

    @staticmethod
    def _resize_scroll(canvas, inner):
        """A rebuild changes the stack's height; without this the scrollregion
        keeps the old one and the last rows are unreachable."""
        inner.update_idletasks()
        canvas.configure(scrollregion=canvas.bbox("all"))

    def _build_social_row(self, parent, r, detail=True, seen_text=None):
        """One roster line.

        `detail` adds the class and level columns, which only the live shard
        page has honest values for. `seen_text` adds the session log's
        last-seen column in their place."""
        name = r.get("n") or "?"
        cls = r.get("k")
        lvl = r.get("lvl")
        me = bool(r.get("me"))
        steam64 = steam64_from_uid(r.get("uid"))

        row = tk.Frame(parent, bg=BG_BODY)
        row.pack(fill="x", pady=1)
        # Your own row is marked the way the meter marks it, for the same
        # reason: it is the row you use to check the list is about who you think.
        tk.Label(row, text="*" if me else " ", bg=BG_BODY,
                 fg=ACCENT, font=self.fonts_m["mono"], width=1).pack(side="left")
        tk.Label(row, text=name[:SOCIAL_NAME_CELLS],
                 bg=BG_BODY, fg=FG_VALUE if me else FG_TEXT,
                 font=self.fonts_m["mono"], width=SOCIAL_NAME_CELLS,
                 anchor="w").pack(side="left")
        if detail:
            tk.Label(row, text=(cls or "-"), bg=BG_BODY,
                     fg=CLASS_COLORS.get(cls, FG_TEXT),
                     font=self.fonts_m["mono"], width=SOCIAL_CLASS_CELLS,
                     anchor="w").pack(side="left")
            tk.Label(row, text=("" if lvl is None else f"lv{lvl}"), bg=BG_BODY,
                     fg=FG_DIM, font=self.fonts_m["mono"], width=5,
                     anchor="w").pack(side="left")
        elif seen_text is not None:
            # Still here reads as present, not as a stale timestamp — so it
            # gets the body colour while everything older stays dimmed.
            tk.Label(row, text=seen_text, bg=BG_BODY,
                     fg=FG_TEXT if seen_text == "now" else FG_DIM,
                     font=self.fonts_m["mono"], width=SOCIAL_SEEN_CELLS,
                     anchor="w").pack(side="left")

        def mini(text, cmd, enabled):
            b = tk.Button(row, text=text, command=cmd,
                          font=self.fonts_m["ui"], bg=BG_BODY_SOFT,
                          fg=FG_TEXT if enabled else FG_DIM,
                          activebackground=BG_BAR_TRACK,
                          activeforeground=FG_VALUE, relief="flat", bd=0,
                          padx=8, pady=1, highlightthickness=1,
                          highlightbackground=BG_BAR_TRACK,
                          cursor="hand2" if enabled else "arrow")
            if not enabled:
                b.config(state="disabled", disabledforeground=FG_DIM)
            b.pack(side="right", padx=(4, 0))
            return b

        # Packed right-to-left, so Copy ends up left of Profile.
        ok = steam64 is not None
        mini("Profile", self._enqueue(
            lambda s=steam64: self._open_url(STEAM_PROFILE_URL.format(s))), ok)
        mini("Copy ID", self._enqueue(
            lambda s=steam64, n=name: self._copy_steamid(n, s)), ok)
        # Mouse wheel over a row must scroll the list it is IN, not stall on
        # the child — and not scroll the other page's list either.
        canvas = (self.session_canvas if parent is self.session_list
                  else self.social_canvas)
        for w in (row,) + tuple(row.winfo_children()):
            if not isinstance(w, tk.Button):
                w.bind("<MouseWheel>",
                       lambda e, c=canvas: c.event_generate(
                           "<MouseWheel>", delta=e.delta))
        return row

    def _copy_steamid(self, name, steam64):
        if steam64 is None:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(str(steam64))
            # Windows serves the clipboard from the owning app, so the value
            # has to be on the wire before focus goes back to the game.
            self.root.update_idletasks()
        except Exception as e:
            print(f"[meter] clipboard failed: {e}", file=sys.stderr)
            self._social_note("Couldn't reach the clipboard.", transient=True)
            return
        self._social_note(f"Copied {name}'s SteamID.", transient=True)

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
        self.btn_map_bg.config(
            text=("☑  World map background" if self._map_bg_on
                  else "☐  World map background"))
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
        self.btn_sounds.config(
            text=("☑  Enable sounds" if self._sounds_on
                  else "☐  Enable sounds"))
        self.btn_mount_random.config(
            text=("☑  Random favorite mount" if self._mount_random
                  else "☐  Random favorite mount"))
        mounts = self.ui_state.mounts()
        if mounts != self._mounts_shown:
            self._mounts_shown = mounts
            self._rebuild_mounts(mounts)
        self.btn_glider_random.config(
            text=("☑  Random favorite glider" if self._glider_random
                  else "☐  Random favorite glider"))
        gliders = self.ui_state.gliders()
        if gliders != self._gliders_shown:
            self._gliders_shown = gliders
            self._rebuild_gliders(gliders)
        self.btn_auto_reset.config(
            text=("☑  Auto reset on boss pull" if self._auto_reset_boss
                  else "☐  Auto reset on boss pull"))
        # Reads state, not action — and it's the same setting the rift prompt's
        # "Do this every time" ticks, so a player who opted in from the prompt
        # finds it already on here.
        self.btn_rift_auto_view.config(
            text=("☑  Auto 'View All Players' in rifts" if self._rift_auto_view
                  else "☐  Auto 'View All Players' in rifts"))
        # Labelled with the action, but tinted by the *state*: green while
        # all-players is the live mode, so it's obvious at a glance that the
        # meter is showing more than the group. Switching either way calls
        # session.reset(), so the label warns about that up front rather than
        # silently binning the encounter mid-fight.
        # Tinted while a parse is live for the same reason the mode button is:
        # it's a state you can forget you're in, and the meter looks normal.
        # Greyed rather than hidden before the first rift: a button that
        # appears out of nowhere mid-session is a button nobody knew to look
        # for. The label says why it does nothing yet.
        have_report = self._report_data is not None
        self.btn_rift_report.config(
            text=("Last Rift Report" if have_report
                  else "Last Rift Report   (no rift yet)"),
            fg=FG_TEXT if have_report else FG_DIM)
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

    def _pump_input(self):
        """Everything that answers to the player rather than to the fight.

        Split out of _refresh and run on its own fast timer: the aggregation
        loop ticks every 250 ms because that is often enough to redraw damage
        numbers, but it was also what decided when the control menu appeared.
        Pressing Escape therefore cost up to a full tick before the fade even
        started, which is a quarter second of nothing happening — long enough
        to feel broken rather than smooth.

        Deliberately only the cheap checks: two user32 calls, a queue drain and
        a comparison, none of which touch the session aggregation the main loop
        owns. Same division the minimap's own loop already uses.
        """
        self._drain()
        # Cheap, and only acted on when it changes, so the click-through style
        # isn't rewritten on every one of these ticks.
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
        self._sync_game_ui()

    def _refresh(self):
        self._apply_update_notice()
        # Polled here rather than pushed by the checker: the check runs on its
        # own thread and Tk is not thread-safe, the same reason the notice line
        # is polled. Cheap — it returns on its first line once answered.
        self._tick_update_offer()
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
        # _sync_game_ui now rides the fast input pump instead — see
        # _pump_input. It stays idempotent, so nothing here depends on which
        # loop got to it first.
        self._refresh_menu()
        # The Social pages are deliberately NOT refreshed here: they load on
        # the menu opening, the tab being raised, and the Refresh button, and
        # hold still in between — see _reload_social for what polling cost.
        _, _, rows = self.session.snapshot()
        rows = self._apply_mode(rows)
        # Decided from the rows that are about to be drawn, so the column
        # follows what you can actually see: a boosted player filtered out by
        # party mode takes their column with them. It costs one pass over at
        # most eight rows.
        self._show_boost = any(p.boost_total > 0.5 for p in rows)
        self._apply_heal_columns()
        if self._sort_heal:
            rows.sort(key=lambda p: -p.heal_total)
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
            self.sort_btn.config(bg=meter_bg, activebackground=meter_bg)
            for w in (self.d_header, self.d_title, self.d_tip):
                w.config(bg=detail_bg)
            for w in (self.title_lbl, self.timer_lbl, self.d_title):
                w.config(fg=theme["fg_header"])
            self.sort_btn.config(fg=theme["fg_header"],
                                 activeforeground=theme["fg_header"])
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
        # Bars scale against the biggest number of their own kind on screen.
        # Neither leader is positional: the sort toggle means rows[0] can be
        # the top healer, and the top of either column can be anyone.
        top_dmg = max((p.total for p in rows), default=0.0) or 1.0
        top_heal = max((p.heal_total for p in rows), default=0.0) or 1.0
        for i, row in enumerate(self.player_rows):
            if i < len(rows):
                p = rows[i]
                dps = p.total / duration if duration > 0 else 0.0
                pct = (p.total / party_total * 100) if party_total else 0.0
                row.show(i + 1, p, dps, pct, focused=(focus == p.name),
                         dmg_frac=p.total / top_dmg,
                         heal_frac=p.heal_total / top_heal,
                         heal_self_frac=p.heal_self / top_heal,
                         show_heal=self._show_heal,
                         show_boost=self._show_boost,
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
            # Named in the breakdown rather than left as a bare number in the
            # column: this is where there is room to say what it was.
            if fp.boost_total > 0.5:
                stats.append(f"{int(fp.boost_total)} boost "
                             f"({fp.boost_hits} hits)")
            if self._show_heal:
                stats.append(f"{int(fp.heal_total)} heal")
                if fp.heal_total > 0.5:
                    stats.append(f"{fp.overheal_pct:.0f}% overheal")
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

        # The input pump. Its own timer, like the minimap's, because what makes
        # the overlay feel responsive and what makes the numbers correct run at
        # completely different speeds — and the slower of the two was setting
        # the pace for both.
        def input_loop():
            try:
                self._pump_input()
            except tk.TclError:
                return              # window went away; stop rescheduling
            self.root.after(UI_TICK_MS, input_loop)
        input_loop()

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
        # Deferred rather than called here: it wants a live Tk loop to schedule
        # the window onto, and the fetch behind it is a network round trip that
        # has no business delaying the overlay coming up.
        self.root.after(1200, self.check_whats_new)
        self.root.mainloop()


_ITEM_NAMES = None


def _item_names():
    """id -> display name, from analysis_out/item_names.json — the game's own
    data.cdb rows, extracted by emit_offsets.py on the same self-heal cycle as
    the offsets. Loaded once; {} when the file is absent (an old analysis_out
    before its regeneration, or the extraction failed and logged why)."""
    global _ITEM_NAMES
    if _ITEM_NAMES is None:
        try:
            _ITEM_NAMES = json.loads(
                (ANALYSIS / "item_names.json").read_text(encoding="utf-8"))
        except Exception:
            _ITEM_NAMES = {}
    return _ITEM_NAMES


_UNIT_NAMES = None


def _unit_names():
    """Same as _item_names, for the cdb's unit sheet. Names the boss kill
    toast: a bar's unit kind is routinely NOT the name the game shows on it
    (measured: 'Cleodora' displays as 'Queen Honeyzabeth', 'Phrixes' as
    'High Inquisitor Chakram' — the kind often names the LAIR, not the boss)."""
    global _UNIT_NAMES
    if _UNIT_NAMES is None:
        try:
            _UNIT_NAMES = json.loads(
                (ANALYSIS / "unit_names.json").read_text(encoding="utf-8"))
        except Exception:
            _UNIT_NAMES = {}
    return _UNIT_NAMES


_HEAL_SPECS = None


def _heal_specs():
    """skill id -> {step: [heal effect spec]}, from analysis_out/heal_specs.json.

    The game's own cdb, extracted on the same self-heal cycle as the offsets.
    Without it every heal on a full-health target is unsizeable and healing
    collapses back to "health actually restored" — so its absence is logged
    rather than swallowed."""
    global _HEAL_SPECS
    if _HEAL_SPECS is None:
        try:
            _HEAL_SPECS = json.loads(
                (ANALYSIS / "heal_specs.json").read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[meter] heal_specs.json unavailable ({e}) — healing falls "
                  "back to what each skill has been seen to restore",
                  file=sys.stderr)
            _HEAL_SPECS = {}
    return _HEAL_SPECS


def _boss_label(kind):
    """The boss's real display name, falling back to the prettified kind for
    anything the unit sheet doesn't carry."""
    return _unit_names().get(kind) or _pretty_id(kind)


def _summon_label(kind):
    """A summon's real display name ('Summon_Imp' -> 'Nightling Terror',
    'Rabbit_EarlyAccess_Spark' -> 'Sparktail'), falling back to the prettified
    kind for anything the unit sheet doesn't carry.

    Same sheet and the same reason as _boss_label: a unit's kind is a backend
    id, not what the game puts on its nameplate. Stripping the `Summon_`/
    `Totem_` prefix off the kind instead looks like it works — `Summon_Imp`
    reduces to a plausible "Imp" — but it is a guess that happens to read well,
    and it degenerates to a raw id on every summon not named that way."""
    return _unit_names().get(kind) or _pretty_id(kind)


def _mount_label(kind):
    """The item's real display name ('Mount_Aries_05' -> 'Aegis'), falling
    back to the prettified id for anything the name table doesn't carry —
    a kind a patch added is still tickable, just under its backend name."""
    nm = _item_names().get(kind)
    if nm:
        return nm
    base = kind[6:] if kind.startswith("Mount_") else kind
    return base.replace("_", " ")


def _glider_label(kind):
    """Same as _mount_label for gliders ('Glider_FlyingFish_Demon' ->
    'Niflelian Wingfish')."""
    nm = _item_names().get(kind)
    if nm:
        return nm
    base = kind[7:] if kind.startswith("Glider_") else kind
    return base.replace("_", " ")


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
        # Self-healing, green-teal, always from the left edge — so the split
        # sits at the same place on every row and the column can be read
        # down. Placed
        # after (and therefore over) the green bar, which draws the full
        # amount: only one of the two widths has to be exactly right, and no
        # rounding gap can open between them.
        self.heal_self_bar = tk.Frame(self.heal_track, bg=SELF_HEAL_BAR,
                                      height=5)
        self.heal_self_bar.place(relwidth=0.0, relheight=1.0)
        self._packed = False
        self._heal_packed = True
        self._name = None
        self._is_me = False
        self._hover = False
        # Every visible piece of the row — bars and tracks included — clicks,
        # carries the hand cursor, and lifts the row on hover: the row IS the
        # click target (it focuses the breakdown), and a target that only
        # answers on its text is a target most of the cursor misses. Enter and
        # Leave both land before Tk repaints, so crossing between the row's
        # own widgets never flickers the lift.
        for w in (self.f, self.top, self.line, self.cls, self.nums,
                  self.dmg_track, self.dmg_bar,
                  self.heal_track, self.heal_bar, self.heal_self_bar):
            w.bind("<Button-1>", lambda e: on_click(self._name))
            w.bind("<Enter>", lambda e: self._set_hover(True))
            w.bind("<Leave>", lambda e: self._set_hover(False))
            w.config(cursor="hand2")

    def _set_hover(self, on):
        if on == self._hover:
            return
        self._hover = on
        bg = self.theme["soft"] if on else self.theme["body"]
        for w in (self.f, self.top, self.line, self.cls, self.nums):
            w.config(bg=bg)

    def set_theme(self, t):
        self.theme = t
        bg = t["soft"] if self._hover else t["body"]
        self.f.config(bg=bg)
        self.top.config(bg=bg)
        ink = t["fg_value"] if self._is_me else t["fg_text"]
        for w in (self.line, self.cls, self.nums):
            w.config(bg=bg, fg=ink)
        self.dmg_track.config(bg=t["track"])
        self.dmg_bar.config(bg=t["dmg"])
        self.heal_track.config(bg=t["track"])
        self.heal_bar.config(bg=t["heal"])
        self.heal_self_bar.config(bg=t["heal_self"])

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
             heal_self_frac=0.0, show_heal=True, show_boost=False, cls_tag=""):
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
        if show_boost:
            # Blank, not "0", for everyone the buff isn't crediting: a column
            # of zeroes down a party of eight says nothing, and the one row
            # that has a number is the whole point of the column.
            nums += (f"{int(p.boost_total):>9}" if p.boost_total > 0.5
                     else " " * 9)
        if show_heal:
            nums += f"{int(p.heal_total):>9}"
            # A row that never healed has no overheal share to report, and a
            # column of "0%" down every damage dealer is noise rather than
            # information — so those cells stay blank.
            nums += (f"{p.overheal_pct:>5.0f}%" if p.heal_total > 0.5
                     else " " * 6)
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
            self.heal_self_bar.place_configure(
                relwidth=_clamp01(min(heal_self_frac, heal_frac)))

    def hide(self):
        if self._packed:
            self.f.pack_forget()
            self._packed = False
            # A row that vanishes mid-hover gets no Leave event; without this
            # it would come back lifted for whoever fills the slot next.
            self._set_hover(False)


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
            # The self-healed segment, always drawn from the left edge so the
            # split lines up down the column. Placed AFTER the main bar so it
            # sits on top of it, which means only one width has to be right:
            # the main bar draws the whole amount and this covers the self
            # share of it, and no rounding gap can open between the two.
            self_bar = tk.Frame(track, bg=SELF_HEAL_BAR, height=4)
            self_bar.place(relwidth=0.0, relheight=1.0)
            self.rows.append((rf, lbl, bar, track, self_bar))
        self._shown = 0

    def set_theme(self, t):
        self.f.config(bg=t["body"])
        self.title_lbl.config(bg=t["body"], fg=t["accent"])
        for rf, lbl, bar, track, self_bar in self.rows:
            rf.config(bg=t["body"])
            lbl.config(bg=t["body"], fg=t["fg_text"])
            track.config(bg=t["track"])
            bar.config(bg=t[self.bar_key])
            self_bar.config(bg=t["heal_self"])

    def show(self, entries, denom):
        """entries: [(label, total, hits, crits, self_total)] sorted desc;
        denom is the player's overall total for the % column."""
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
            label, tot, hits, _crits, slf = entries[i]
            pct = (tot / denom * 100) if denom else 0.0
            _rf, lbl, bar, _track, self_bar = self.rows[i]
            lbl.config(text=f"{label[:16]:<16}{int(tot):>8} {pct:>3.0f}% {hits:>3}h")
            frac = _clamp01(tot / top)
            bar.place_configure(relwidth=frac)
            # The self share of THIS row, in the same scale as the row's bar —
            # so a skill cast only on yourself reads as a bar that is entirely
            # parchment, however small the skill is next to the column's biggest.
            self_bar.place_configure(
                relwidth=frac * _clamp01(slf / tot) if tot > 0 else 0.0)


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
REQUIRED_RESOLVER_KEYS = ("anchors", "boss_fns", "boss_targets", "cam_targets",
                          "count_targets", "funcs", "glider_targets",
                          "mount_targets", "ui_targets")
# The minimap's half of the offsets. The combat half (DamageResult, HitData,
# ...) is deliberately not listed wholesale: it has been there since the first
# release, so it can't be what an upgrade is missing, and a list that mentions
# everything is a list nobody maintains.
#
# "Key.subkey" entries check INSIDE a group. DamageResult has existed forever,
# so its presence proves nothing — but 2.3.3 started reading `blocker` out of
# it to drop damage against an invulnerable target, and a 2.3.2 file has the
# group without that field. The hook degrades quietly when it's absent (no
# gate, immune damage counted again), which is precisely the silent-upgrade
# failure the rest of this comment is about.
REQUIRED_OFFSET_KEYS = ("Activity", "ArrayObj", "BossInfo", "BossesInfo",
                        "Camera", "DamageResult.blocker", "DamageResult.effect",
                        "Element", "Entity", "Foe", "GameLayer", "Hero",
                        "Interactible", "State", "String", "Unit", "Unit.attr",
                        # Healing on a full-health target. Without these the
                        # hook sends no dynVal/attributes and every overheal is
                        # sized 0 — the exact bug the feature exists to fix.
                        "BaseSkill.dynVal1", "HitData.step", "SkillStep",
                        "UnitAttributes.faith",
                        # The legendary pickup cue. Hero has existed forever,
                        # so its presence proves nothing — the subkeys are what
                        # a pre-3.1 file is missing, and sweepInventory() bails
                        # silently without them.
                        "Hero.loadout", "Hero.weaponInHand", "Inventory",
                        "Item", "Item.uid", "Loadout", "Weapon", "Weapon.rarity",
                        # The zone signal / map backdrop identity. GameLayer
                        # has existed forever; these subkeys are what a
                        # pre-3.0.4 file is missing — without them the hook
                        # falls back to no zone identity at all (getMapId is a
                        # hostname, see TESTING.md) and the backdrop never
                        # draws.
                        "GameLayer.world", "World.level",
                        # The random-favorite mount. Player has existed since
                        # the party meter; the collection walk is what a
                        # pre-3.2 file is missing, and hookMountSwap refuses
                        # to arm without it.
                        "Player.accountProgress", "AccountProgress",
                        "Collection", "ArrayProxyData", "ArrayDyn",
                        # Ore/herb nodes. A pre-3.2.1 file lacks the group and
                        # sweepArray quietly draws no nodes at all.
                        "Gatherable",
                        # Summon and pet damage. A pre-3.3.4 file has no foe
                        # class list, and without it the hook cannot safely
                        # read summonOwner off a dealer — so it doesn't, and
                        # every pet's damage silently vanishes from the parse
                        # exactly as it did before the feature existed.
                        "foeClasses",
                        # The random-favorite glider. Collection has existed
                        # since 3.2; the gliders list is what a pre-3.2.2 file
                        # is missing, and hookGliderEquip refuses to arm
                        # without it.
                        "Collection.gliders",
                        # The Social tab's shard roster. Player, Hero and
                        # GameLayer have all existed for releases, so their
                        # presence proves nothing — these five subkeys are what
                        # a pre-3.2.2 file lacks. readShard() returns an empty
                        # list on its first line without them, so the tab would
                        # sit there looking like an empty shard rather than
                        # like a stale data directory: exactly the silent
                        # upgrade failure this list exists to prevent.
                        "Player.uid", "Player.hero", "GameLayer.players",
                        "Hero.kind", "Hero.level")


def _data_is_current():
    """True if both generated files carry everything the hook reads.

    Missing keys are named rather than just counted: this runs before the
    overlay exists, so the log is the only place anyone can see why a
    multi-second regenerate just happened.

    A key may be "Group.field", which checks for the field inside the group —
    a group that has existed for releases can still be missing a field this
    build depends on."""
    def present(d, key):
        group, _, field = key.partition(".")
        got = d.get(group)
        if not got:
            return False
        # `is not None` rather than truthiness: an offset of 0 is a real
        # offset, and `not 0` would condemn a perfectly good file.
        return True if not field else (isinstance(got, dict)
                                       and got.get(field) is not None)

    for name, required in (("resolver_data.json", REQUIRED_RESOLVER_KEYS),
                           ("meter_offsets.json", REQUIRED_OFFSET_KEYS)):
        try:
            d = json.loads((ANALYSIS / name).read_text())
        except Exception:
            return False
        missing = [k for k in required if not present(d, k)]
        if missing:
            print(f"[meter] {name} predates this build — missing "
                  f"{', '.join(missing)}; regenerating.", file=sys.stderr)
            return False
    # item_names.json arrived with the Mounts tab (3.2). The generators only
    # re-run when this returns False, so an upgrade over an older analysis_out
    # has to fail here once or the tab shows backend ids until the next game
    # patch. Existence only — its content is cosmetic and self-describing.
    if not (ANALYSIS / "item_names.json").exists():
        print("[meter] item_names.json absent — regenerating for the mount "
              "labels.", file=sys.stderr)
        return False
    # unit_names.json arrived with the boss kill timer (3.4) — same upgrade
    # trap, same fix.
    if not (ANALYSIS / "unit_names.json").exists():
        print("[meter] unit_names.json absent — regenerating for the boss "
              "names.", file=sys.stderr)
        return False
    # heal_specs.json arrived when healing started counting overheal — without
    # it a heal that restores nothing cannot be sized, which is the whole
    # feature. Same upgrade trap as the two above.
    if not (ANALYSIS / "heal_specs.json").exists():
        print("[meter] heal_specs.json absent — regenerating so healing can "
              "be counted on full-health targets.", file=sys.stderr)
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
                # Say WHY when the skip doesn't happen. Regenerating costs two
                # subprocess parses of a 14 MB bytecode file, right as the game
                # is loading, and without this the log shows the cost with no
                # reason attached — which is exactly the state that made a
                # stale stamp take an hour to spot. `_data_is_current` prints
                # its own reason, so only the stamp arm needs one here.
                on_disk = json.loads(DATA_STAMP.read_text())
                if on_disk != stamp:
                    print(f"[meter] hlboot.dat has changed since the last "
                          f"regenerate (stamp {on_disk.get('size')} bytes, "
                          f"now {stamp['size']}); regenerating.",
                          file=sys.stderr)
                elif (not (ANALYSIS / "resolver_data.json").is_file()
                        or not (ANALYSIS / "meter_offsets.json").is_file()):
                    print("[meter] a generated file is missing; regenerating.",
                          file=sys.stderr)
                elif _data_is_current():
                    print("[meter] data already matches this build "
                          "(hlboot.dat unchanged).", file=sys.stderr)
                    return True
            except FileNotFoundError:
                print("[meter] no data stamp yet; regenerating.",
                      file=sys.stderr)
            except Exception as e:
                print(f"[meter] couldn't read the data stamp ({e}); "
                      "regenerating.", file=sys.stderr)
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
        # Written AND read back. A stamp that silently fails to land costs a
        # full regenerate on every single launch — the data stays correct, so
        # nothing looks wrong except several seconds of startup, and the old
        # `except OSError: pass` made that invisible. Whatever goes wrong here,
        # the log now says so once per launch instead of never.
        try:
            DATA_STAMP.write_text(json.dumps(stamp), encoding="utf-8")
            back = json.loads(DATA_STAMP.read_text())
            if back != stamp:
                print("[meter] the data stamp did not take — every launch will "
                      f"regenerate. Wrote {stamp['size']} bytes, read back "
                      f"{back.get('size')}. Check {DATA_STAMP}.",
                      file=sys.stderr)
        except Exception as e:
            print(f"[meter] couldn't write the data stamp ({e}) — data is "
                  "correct, but every launch will regenerate it. "
                  f"Check {DATA_STAMP}.", file=sys.stderr)
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

    def bar(x, y, w, frac, colour, thick, self_frac=0.0):
        """One bar, optionally split: `self_frac` of the SAME scale as `frac`
        is drawn from the left edge in the self-heal parchment, over the top of
        full-length bar — same trick as the live overlay, so the two can't
        disagree about where the join is."""
        d.rectangle((x, y, x + w, y + thick - 1), fill=BG_BAR_TRACK)
        filled = int(w * min(1.0, max(0.0, frac)))
        if filled > 0:
            d.rectangle((x, y, x + filled, y + thick - 1), fill=colour)
        selfw = int(w * min(min(1.0, max(0.0, self_frac)),
                            min(1.0, max(0.0, frac))))
        if selfw > 0:
            d.rectangle((x, y, x + selfw, y + thick - 1), fill=SELF_HEAL_BAR)

    d.text((x0, y), f"{data['mode']}   ({len(rows)})", font=ui_small, fill=ACCENT)
    y += 18
    # Same rule as the overlay's BOOST column: drawn only when the parse
    # actually contains boosted damage. Older snapshots have no such key.
    show_boost = any(r.get("boost", 0.0) > 0.5 for r in rows)
    d.text((x0, y),
           f"  #  {'NAME':<12}{'DMG':>9} {'DPS':>6} {'%':>4}"
           + (f"{'BOOST':>9}" if show_boost else "")
           + f"{'HEAL':>9}{'OVER':>6}",
           font=mono, fill=FG_DIM)
    y += line_h

    top_dmg = max((r["total"] for r in rows), default=0.0) or 1.0
    top_heal = max((r["heal"] for r in rows), default=0.0) or 1.0
    for i, r in enumerate(rows, 1):
        me = "*" if r["is_me"] else " "
        over = (f"{r.get('overheal', 0.0):>5.0f}%" if r["heal"] > 0.5
                else " " * 6)
        boost = ""
        if show_boost:
            boost = (f"{int(r['boost']):>9}" if r.get("boost", 0.0) > 0.5
                     else " " * 9)
        d.text((x0, y), f"  {i}.{me}{r['name'][:12]:<12}{int(r['total']):>9} "
                        f"{r['dps']:>6.0f} {r['pct']:>3.0f}%"
                        f"{boost}{int(r['heal']):>9}{over}",
               font=mono, fill=FG_VALUE if r["is_me"] else FG_TEXT)
        y += line_h
        bar(x0, y, x1 - x0, r["total"] / top_dmg, DMG_BAR, bar_h)
        y += bar_h
        bar(x0, y, x1 - x0, r["heal"] / top_heal, HEAL_BAR, bar_h,
            self_frac=r.get("heal_self", 0.0) / top_heal)
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
            for label, amount, hits, _crits, slf in entries:
                pct = (amount / denom * 100) if denom else 0.0
                d.text((cx, cy),
                       f"{label[:16]:<16}{int(amount):>8} {pct:>3.0f}% {hits:>3}h",
                       font=mono_small, fill=FG_TEXT)
                cy += line_h
                frac = amount / scale
                bar(cx, cy, colw, frac, colour, 4,
                    self_frac=frac * (slf / amount) if amount > 0 else 0.0)
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


# The rift report as an image — same reasoning as the parse image: drawn from
# the numbers rather than screenshotted from the card, so it's pixel-clean at
# any window opacity and works with the card closed. Same two-column layout,
# same tiering, same palette, so a paste reads as the card it came from.
RIFT_IMG_COL_W = 350
RIFT_IMG_PAD = 20
RIFT_IMG_GAP = 26


def render_rift_report_image(data, path=None):
    """Draw an end-of-rift report dict as a PIL image. Returns the image;
    also writes a PNG when `path` is given."""
    from PIL import Image, ImageDraw

    ui_mvp = _parse_font(PARSE_FONT_UI, 22)
    ui = _parse_font(PARSE_FONT_UI, 15)
    ui_rank = _parse_font(PARSE_FONT_UI, 14)
    ui_small = _parse_font(PARSE_FONT_UI, 11)
    mono = _parse_font(PARSE_FONT_MONO, 13)
    mono_small = _parse_font(PARSE_FONT_MONO, 11)

    W = RIFT_IMG_PAD * 2 + RIFT_IMG_COL_W * 2 + RIFT_IMG_GAP
    img = Image.new("RGB", (W, 1600), RIFT_BODY)
    d = ImageDraw.Draw(img)

    d.rectangle((0, 0, W - 1, 39), fill=RIFT_GLOW)
    d.text((RIFT_IMG_PAD, 10), "RIFT REPORT", font=ui, fill=RIFT_TIME)
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(data["at"]))
    d.text((W - RIFT_IMG_PAD - d.textlength(stamp, font=mono_small), 14),
           stamp, font=mono_small, fill=RIFT_PEAK)

    def heading(cx, y, text):
        d.text((cx, y), text, font=ui_small, fill=RIFT_TITLE)
        tw = d.textlength(text, font=ui_small)
        d.line((cx + tw + 8, y + 7, cx + RIFT_IMG_COL_W, y + 7), fill=RIFT_GLOW)
        return y + 22

    # The card's ★ and ✚ are DRAWN here rather than typed: PIL does no font
    # fallback, so glyphs Segoe UI Bold doesn't carry come out as tofu boxes —
    # measured on the first render. Shapes can't be missing from a font.
    def star(x, y, r, fill):
        pts = []
        for i in range(10):
            a = -math.pi / 2 + i * math.pi / 5
            rad = r if i % 2 == 0 else r * 0.42
            pts.append((x + rad * math.cos(a), y + rad * math.sin(a)))
        d.polygon(pts, fill=fill)

    def plus(x, y, r, fill):
        t = max(2, int(r * 0.55))
        d.rectangle((x - t // 2, y - r, x + t // 2, y + r), fill=fill)
        d.rectangle((x - r, y - t // 2, x + r, y + t // 2), fill=fill)

    def column(cx, ph):
        y = 52
        d.text((cx, y), ph["label"].upper(), font=ui, fill=RIFT_PEAK)
        y += 24
        d.text((cx, y), f"{Overlay._mmss(ph['duration'])}  ·  "
               f"{int(ph['total']):,} dmg  ·  {int(ph['heal']):,} heal"
               + _overheal_note(ph),
               font=ui_small, fill=RIFT_TITLE)
        y += 24
        players = ph["players"]
        if not players:
            d.text((cx, y), "nothing was recorded for this phase",
                   font=ui_small, fill=RIFT_TITLE)
            return y + 20

        y = heading(cx, y, "MVP")
        mvp = players[0]
        star(cx + 11, y + 14, 11, REPORT_MEDALS[0])
        d.text((cx + 28, y), Overlay._elide_name(mvp["name"]),
               font=ui_mvp, fill=REPORT_MEDALS[0])
        if mvp.get("cls"):
            d.text((cx + 32 + d.textlength(Overlay._elide_name(mvp["name"]),
                                           font=ui_mvp), y + 12),
                   mvp["cls"], font=ui_small, fill=RIFT_TITLE)
        y += 30
        d.text((cx + 28, y), f"{int(mvp['total']):,} damage",
               font=ui_small, fill=RIFT_TIME)
        y += 18
        healer = max(players, key=lambda p: p["heal"])
        if healer["heal"] > 0.5:
            plus(cx + 8, y + 9, 7, REPORT_HEAL)
            d.text((cx + 22, y), f"{Overlay._elide_name(healer['name'])}"
                   + (f" ({healer['cls']})" if healer.get("cls") else "")
                   + f"   {int(healer['heal']):,} heal"
                   + _overheal_note(healer, "   {:.0f}% over"),
                   font=ui_rank, fill=REPORT_HEAL)
            y += 22

        def rank_rows(y, entries, total, key):
            for i, p in enumerate(entries[:5], 1):
                top3 = i <= 3
                fg = REPORT_MEDALS[i - 1] if top3 else RIFT_TITLE
                nfont = ui_rank if top3 else ui_small
                d.text((cx, y), str(i), font=nfont, fill=fg)
                nm = Overlay._elide_name(p["name"])
                d.text((cx + 18, y), nm,
                       font=nfont, fill=RIFT_TIME if top3 else RIFT_TITLE)
                # Drawn separately, in the dim colour, so a long name's
                # elision can never eat the acronym.
                if p.get("cls"):
                    # Sat on the name's baseline, not its top: the acronym is
                    # 11px against a 14px (or 11px) name, and hanging it from
                    # the same y reads as a superscript.
                    d.text((cx + 22 + d.textlength(nm, font=nfont),
                            y + (5 if top3 else 2)),
                           p["cls"], font=mono_small, fill=RIFT_TITLE)
                amt = f"{int(p[key]):,}"
                pct = p[key] / total * 100 if total else 0.0
                d.text((cx + RIFT_IMG_COL_W - 44
                        - d.textlength(amt, font=mono), y + 2),
                       amt, font=mono, fill=RIFT_TIME)
                d.text((cx + RIFT_IMG_COL_W
                        - d.textlength(f"{pct:.0f}%", font=mono_small), y + 3),
                       f"{pct:.0f}%", font=mono_small, fill=RIFT_TITLE)
                y += 22 if top3 else 19
            return y

        y = heading(cx, y + 6, "DAMAGE — TOP 5")
        y = rank_rows(y, players, ph["total"], "total")
        healers = sorted((p for p in players if p["heal"] > 0.5),
                         key=lambda p: -p["heal"])
        y = heading(cx, y + 4, "HEALING — TOP 5")
        if healers:
            y = rank_rows(y, healers, ph["heal"], "heal")
        else:
            d.text((cx, y), "no healing recorded", font=ui_small,
                   fill=RIFT_TITLE)
            y += 19

        y = heading(cx, y + 4, "DAMAGE BY TYPE")
        top = ph["elements"][0][1] if ph["elements"] else 0.0
        for el, amt in ph["elements"][:8]:
            colour = _lerp_hex(element_color(el), "#FFFFFF", 0.30)
            d.text((cx, y), "Other" if el == "?" else str(el),
                   font=ui_small, fill=colour)
            frac = amt / top if top else 0.0
            bar_x = cx + 78
            bar_w = RIFT_IMG_COL_W - 78 - 44
            d.rectangle((bar_x, y + 4, bar_x + int(bar_w * frac), y + 11),
                        fill=colour)
            pct = amt / ph["total"] * 100 if ph["total"] else 0.0
            d.text((cx + RIFT_IMG_COL_W
                    - d.textlength(f"{pct:.1f}%", font=mono_small), y + 2),
                   f"{pct:.1f}%", font=mono_small, fill=RIFT_TIME)
            y += 18
        return y

    bottoms = [column(RIFT_IMG_PAD, data["phases"][0]),
               column(RIFT_IMG_PAD + RIFT_IMG_COL_W + RIFT_IMG_GAP,
                      data["phases"][1])]
    mid = RIFT_IMG_PAD + RIFT_IMG_COL_W + RIFT_IMG_GAP // 2
    h = max(bottoms) + RIFT_IMG_PAD
    d.line((mid, 52, mid, h - RIFT_IMG_PAD), fill=RIFT_GLOW)
    img = img.crop((0, 0, W, h))
    ImageDraw.Draw(img).rectangle((0, 0, W - 1, img.height - 1),
                                  outline=RIFT_EDGE, width=2)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        img.save(path)
    return img


def copy_image_to_clipboard(img):
    """Put a PIL image on the Windows clipboard as CF_DIB — the format every
    paste target understands. A BMP file is a 14-byte header ahead of a DIB,
    so the conversion is a save and a slice, no encoder gymnastics."""
    import io
    if sys.platform != "win32":
        raise OSError("image clipboard is Windows-only")
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "BMP")
    dib = buf.getvalue()[14:]

    k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
    k32.GlobalAlloc.restype = ctypes.c_void_p
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = (ctypes.c_void_p,)
    k32.GlobalUnlock.argtypes = (ctypes.c_void_p,)
    k32.GlobalFree.argtypes = (ctypes.c_void_p,)
    u32.SetClipboardData.restype = ctypes.c_void_p
    u32.SetClipboardData.argtypes = (wintypes.UINT, ctypes.c_void_p)

    GMEM_MOVEABLE = 0x0002
    h = k32.GlobalAlloc(GMEM_MOVEABLE, len(dib))
    if not h:
        raise OSError("GlobalAlloc failed")
    p = k32.GlobalLock(h)
    ctypes.memmove(p, dib, len(dib))
    k32.GlobalUnlock(h)
    # The clipboard is one shared lock; whoever synced it last (clipboard
    # managers love to) can hold it for a beat. Brief retries beat failing.
    for attempt in range(5):
        if u32.OpenClipboard(0):
            break
        time.sleep(0.05)
    else:
        k32.GlobalFree(h)
        raise OSError("clipboard is held by another window")
    try:
        u32.EmptyClipboard()
        if not u32.SetClipboardData(8, ctypes.c_void_p(h)):    # CF_DIB
            k32.GlobalFree(h)
            raise OSError("SetClipboardData failed")
        # Ownership of `h` passed to the system on success — no free here.
    finally:
        u32.CloseClipboard()


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
    # background; the notice lands when it lands. Re-checked on loading screens
    # too — see the zone handler.
    check_for_update(announce=True)
    claim_single_instance()
    # Only after claiming: before it, the flag on disk may still be the one
    # aimed at the instance we just displaced.
    watch_for_quit_request()
    session = PartySession()
    ui_state = GameUIState()
    world = WorldSnapshot()
    rift_rec = RiftRecorder()
    # Outlives every encounter on purpose: a skill's heal size is a property of
    # the build, not of the pull, and resetting it each fight would throw away
    # exactly the observations that make the first heals of the next one
    # countable.
    heal_sizer = HealSizeEstimator(_heal_specs())

    # Up before anything that can block. Attaching waits for the game to launch
    # and the hook's memory scan can run for minutes on a slow machine — with no
    # console, an icon that only appeared afterwards would leave the user
    # staring at nothing, with Task Manager as their only way to change their
    # mind. Its quit callback works throughout, overlay or not.
    tray = TrayIcon(request_stop)
    tray.start()
    try:
        return _run(tray, session, ui_state, world, rift_rec, heal_sizer)
    finally:
        tray.stop()
        # Here as well as on the paths inside _run, which miss the early
        # returns — stopping while still waiting for the game would otherwise
        # leave our pid sitting in the lock file. Unlinking twice is harmless.
        release_instance_lock()


def _run(tray, session, ui_state, world, rift_rec, heal_sizer):
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

    def on_detached(*args):
        """The frida session died — in practice, the game closed or crashed.
        Fires on frida's own thread. On a normal quit this fires too (we're
        the ones detaching), but by then the finally below has already cleared
        _OVERLAY, which is what keeps the prompt out of that path."""
        reason = str(args[0]) if args else ""
        print(f"[meter] game session detached ({reason or 'unknown'}).",
              file=sys.stderr)
        ov = _OVERLAY["ref"]
        if ov is not None:
            ov.on_game_exit(reason)
    fsession.on("detached", on_detached)

    ready = {"ok": None}
    ready_evt = threading.Event()
    liveness = {"t": time.monotonic(), "printed": 0.0}
    hero_id = {"name": None}           # last local hero, to keep the log quiet
    shard_seen = {"n": False}          # the roster is announced once, not per sweep
    nullified: dict = {}               # mitigated-hit shapes seen, see below
    nullified_at = [0.0]               # last time they were reported
    boss_fight_on = [False]            # a boss fight is under way, see below
    # The fight clock: armed on the pull edge, read when the last bar goes
    # down killed. It inherits the pull edge's known cost — walk away and
    # re-pull without a loading screen and the clock keeps the first pull's
    # start — because a fight that never formally ended was never re-timed
    # either. `kinds` is what keys the record; see _record_boss_kill.
    boss_clock = {"t0": None, "kinds": ()}
    # (skill id, display name) pairs already reported for boosted damage. The
    # quirk is matched on the display name because the id was never measured —
    # this is what measures it, once per id, so BOOST_SKILL_IDS can be filled
    # in from a log rather than from a guess. See "Patch quirks".
    boost_seen: set = set()
    # (pet, owner) pairs already reported. Summon damage merges silently into
    # the owner's row, which means a regression here is invisible — the number
    # is just quietly ~13% low, exactly as it was before 3.3.4. This is the
    # only place that says out loud that pet attribution is working, and on
    # which summons. See "Summon and pet damage".
    pet_seen: set = set()

    heal_log_at = [0.0]

    def on_message(message, data):
        liveness["t"] = time.monotonic()   # any agent traffic counts as alive
        if message["type"] == "error":
            print("[JS]", message.get("description"), file=sys.stderr)
            return
        p = message.get("payload") or {}
        k = p.get("kind")
        if k == "hit":
            # Nullified-hit diagnostic. The hook only attaches these when one of
            # them is set, so an ordinary hit never reaches this branch. Damage
            # against a boss in an immunity phase is currently counted in full;
            # this is here to establish WHICH field marks it before the meter
            # starts discarding hits, because gating on the wrong one would
            # silently drop real damage instead of fake damage.
            # Tallied and reported once every 30s, not per hit: a boss with a
            # long immune phase would otherwise write thousands of lines.
            dropped = False
            if "blocker" in p:
                who = p.get("blocker") or ""
                dropped = who in NULLIFIED_BLOCKERS
                sig = (who, p.get("effect"), (p.get("block") or 0) > 0,
                       (p.get("amount") or 0) > 0, dropped)
                nullified[sig] = nullified.get(sig, 0) + 1
                now_m = time.monotonic()
                if now_m - nullified_at[0] > 30.0:
                    nullified_at[0] = now_m
                    for s, n in sorted(nullified.items(), key=lambda kv: -kv[1]):
                        print(f"[meter] mitigated-hit x{n}: blocker={s[0]!r} "
                              f"effect={s[1]} block>0={s[2]} amount>0={s[3]} "
                              f"{'DROPPED' if s[4] else 'counted'}",
                              file=sys.stderr)
                    nullified.clear()
            # The target never took this, so it is not damage. Dropped before
            # record() rather than subtracted after, so it can't start an
            # encounter or extend one either — whaling on an immune boss is not
            # combat as far as the parse is concerned.
            # Classified here, once, and stamped onto the event: both
            # aggregators then bucket it the same way without either of them
            # knowing which skill the current patch is broken on.
            if is_boost_hit(p):
                p["boost"] = 1
                sig = (p.get("skill") or "?", p.get("name") or "")
                if sig not in boost_seen:
                    boost_seen.add(sig)
                    print(f"[meter] boosted damage: skill={sig[0]!r} "
                          f"name={sig[1]!r} — counted as Boost, not damage",
                          file=sys.stderr)
            if p.get("pet"):
                sig = (p["pet"], p.get("player") or "?")
                if sig not in pet_seen:
                    pet_seen.add(sig)
                    print(f"[meter] summon damage: pet={sig[0]!r} "
                          f"credited to {sig[1]!r}", file=sys.stderr)
            if not dropped:
                session.record(p)
                rift_rec.record("hit", p)
        elif k == "heal":
            # The hook reports what LANDED (0 for a heal on a full-health
            # target); this fills in how big the heal itself was, before both
            # aggregators see it, so the two can never disagree.
            heal_sizer.stamp(p)
            session.record_heal(p)
            rift_rec.record("heal", p)
            # The formula is checked against ordinary play, not asserted: this
            # says how many heals the cdb table could size and names any skill
            # whose computed size came out below what it measurably restored.
            now_h = time.monotonic()
            if now_h - heal_log_at[0] > 30.0:
                heal_log_at[0] = now_h
                line = heal_sizer.drain_report()
                if line:
                    print(line, file=sys.stderr)
        elif k == "combat":
            session.set_combat(p.get("state") or {})
        elif k == "mounts":
            # The unlocked-mount list for the Mounts tab. Sent once the hook
            # can walk the collection, then only when the set changes.
            kinds = p.get("list") or []
            ui_state.set_mounts(kinds)
            print(f"[meter] mount collection: {len(kinds)} unlocked",
                  file=sys.stderr)
        elif k == "gliders":
            # Same for the Gliders tab.
            kinds = p.get("list") or []
            ui_state.set_gliders(kinds)
            print(f"[meter] glider collection: {len(kinds)} unlocked",
                  file=sys.stderr)
        elif k == "world":
            world.update(p)
        elif k == "rift":
            state = bool(p.get("state"))
            ui_state.set_rift(state)
            rift_rec.set_rift(state)
            print(f"[meter] rift: {state}", file=sys.stderr)
        elif k == "window":
            name, is_open = p.get("name"), bool(p.get("open"))
            ui_state.set_window(name, is_open)
            # Logged because every class the game opens now hides the overlay
            # (MENU_IGNORE_WINDOWS aside) — if the meter goes missing and stays
            # missing, these lines name the window that's holding it down.
            print(f"[meter] game window {name} {'open' if is_open else 'closed'}",
                  file=sys.stderr)
        elif k == "bossbar":
            # The game's own boss/elite healthbar went up or down. `n` drives
            # the compass auto-hide; the up/down lists drive the boss-only
            # rules, which is why the hook classifies each unit — an elite
            # raises the same bar and must NOT reset the meter or play a cue.
            ui_state.set_boss_bar(p.get("n") or 0)
            ov = _OVERLAY["ref"]
            # The reset fires on the edge INTO a boss fight, once, and the
            # fight only ends the two ways it ends for the player: the boss
            # died (the same signal the victory cue rides on, below), or a
            # loading screen took them out of the instance (the zone handler).
            #
            # What decidedly does NOT end it is "no boss bar is up right now".
            # That was the previous rule and the Nightqueen breaks it: she
            # replaces herself with copies, and between the old bar dropping
            # and the new ones rising there is at least one poll seeing zero
            # boss bars. That read as the fight ending, so the copies' bars
            # read as a fresh pull and wiped the meter repeatedly through a
            # single fight.
            #
            # The cost of the stricter rule: walking away from a boss and
            # re-pulling it, without dying and without a loading screen, will
            # not reset again — the fight never formally ended. A wipe usually
            # brings a loading screen with it, which does re-arm.
            now_boss = bool(p.get("boss"))
            if now_boss and not boss_fight_on[0]:
                boss_fight_on[0] = True
                kinds = [b.get("kind") for b in (p.get("up") or [])
                         if b.get("boss")]
                boss_clock["t0"] = time.monotonic()
                boss_clock["kinds"] = tuple(sorted(k for k in kinds if k))
                print(f"[meter] boss fight started: "
                      f"{', '.join(k for k in kinds if k) or '?'}",
                      file=sys.stderr)
                # The rift report's phase boundary — the same edge, whether or
                # not the auto-reset setting below is on. A no-op outside a
                # rift, or on a second pull edge in one.
                rift_rec.on_boss_pull()
                if ov is not None and ov.auto_reset_boss():
                    # Not a plain reset: the bar is refreshed on a timer, so
                    # this arrives after the opening burst has already landed.
                    # Carry the last few seconds forward or the reset eats it.
                    kept = session.reset_keeping_recent()
                    print(f"[meter] meter reset for the pull "
                          f"(kept {kept} event{'' if kept == 1 else 's'} from "
                          f"the last {BOSS_PULL_BACKLAG_SECS:.0f}s)",
                          file=sys.stderr)
                    # Queued rather than called: this is the hook's thread, and
                    # both the reset banner and the cue belong to the Tk thread.
                    ov.on_boss_pull()
            for b in (p.get("down") or []):
                # `killed` is decided in the hook from the last health seen
                # while the bar was up — a bar that drops because the player
                # walked away and the boss reset is not a kill, and measured
                # it is the common case.
                if b.get("boss") and b.get("killed"):
                    print(f"[meter] boss killed: {b.get('kind')}",
                          file=sys.stderr)
                    # A kill ends the fight and re-arms the reset — but ONLY
                    # if it took the last boss bar with it. The hook keys bars
                    # by unit pointer and caches the boss/elite classification
                    # by KIND, so every copy the Nightqueen spawns is its own
                    # bar carrying boss=true, and a copy dying reports exactly
                    # the same {boss, killed} as she does. Re-arming on that
                    # would unlatch mid-fight and let the surviving copies'
                    # bars read as a fresh pull on the very next poll — the
                    # reported bug, straight back through a different door.
                    # `boss` is computed over the bar set as it stands AFTER
                    # this removal, so "no boss left" is exactly what it says.
                    if not now_boss:
                        boss_fight_on[0] = False
                        print("[meter] last boss bar down — pull reset "
                              "re-armed", file=sys.stderr)
                        # ...and the fight clock has its reading. Keyed by the
                        # pull's kinds; the killed bar's kind is the fallback
                        # for a pull whose bars arrived nameless.
                        t0 = boss_clock["t0"]
                        boss_clock["t0"] = None
                        if t0 is not None and ov is not None:
                            fkinds = boss_clock["kinds"] or (
                                (b.get("kind"),) if b.get("kind") else ())
                            ov.on_boss_timed_kill(fkinds,
                                                  time.monotonic() - t0)
                        # The fight is formally over — if a rift was recording,
                        # this is its ending, and the report goes up on the
                        # same signal the victory cue rides. Guarded by the
                        # recorder itself: None outside a rift.
                        report = rift_rec.on_boss_kill()
                        # Classes are stamped in HERE rather than looked up
                        # when the card draws: a report outlives its session
                        # (it is saved to disk and re-opened days later, when
                        # the world sweep no longer knows who these people
                        # were), so the acronym has to be frozen with the rest
                        # of the numbers.
                        if report is not None:
                            _stamp_report_classes(report, world)
                        if report is not None and ov is not None:
                            print("[meter] rift complete — showing the "
                                  "end-of-rift report", file=sys.stderr)
                            ov.show_rift_report(report)
                    if ov is not None:
                        ov.on_boss_kill()
        elif k == "pickup":
            # The hook counts items by `kind` across inventory AND equipment,
            # so this only fires when the hero genuinely gained one — moving a
            # weapon between the two (which mints a new hxbit uid every time)
            # leaves the count alone. Anything can be reported; only legendary
            # WEAPONS make a noise, because rarity is a field that exists only
            # on st.item.Weapon.
            rarity = p.get("rarity")
            print(f"[meter] picked up: {p.get('item')} "
                  f"({p.get('cls')}{', ' + rarity if rarity else ''})"
                  + (f" x{p.get('count')}" if (p.get("count") or 1) > 1 else ""),
                  file=sys.stderr)
            if rarity == LEGENDARY_RARITY:
                # No switch of its own: it rides the master Sounds setting,
                # which SoundPlayer.play() already gates on.
                ov = _OVERLAY["ref"]
                if ov is not None:
                    ov.on_legendary_pickup()
        elif k == "zone":
            ui_state.set_zone(p.get("sig"))
            # The first report after attach says where we already are — it
            # keys the map background but is not a loading screen, so nothing
            # resets on it.
            # The sidecar fields ride along so their real meaning accumulates
            # from normal play — `name`, `branchName` and `_isWorldMap` were
            # emitted unmeasured, and this line is how they get measured.
            extra = ", ".join(f"{k}={p.get(k)!r}"
                              for k in ("name", "branch", "world_map")
                              if p.get(k) is not None)
            if p.get("initial"):
                print(f"[meter] zone identified ({p.get('sig')!r}"
                      + (f"; {extra}" if extra else "") + ")",
                      file=sys.stderr)
                return
            ui_state.clear()      # the UI is rebuilt across a loading screen
            session.reset()
            # A loading screen mid-rift is a wipe or a walk-out, not a finished
            # run — the recording is abandoned, not reported.
            rift_rec.on_zone()
            # The other way a boss fight ends. Nothing else re-arms the pull
            # reset now that a dropped bar doesn't, so leaving an instance
            # mid-fight has to — otherwise a fight abandoned rather than won
            # would suppress the reset for the rest of the session.
            boss_fight_on[0] = False
            boss_clock["t0"] = None    # an abandoned fight is not a time
            print(f"[meter] zone change ({p.get('sig')!r}"
                  + (f"; {extra}" if extra else "") + ") — meter reset",
                  file=sys.stderr)
            # A loading screen is the one moment the player is demonstrably
            # not mid-fight, which makes it the right time to notice a new
            # release. Throttled and self-silencing inside — this costs
            # nothing on the zone changes it declines to act on.
            check_for_update()
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
        elif k == "shard":
            # The hook only sends this when the roster actually changed, so
            # there is no throttle here — an arriving message IS the change.
            rows = p.get("list") or []
            world.set_shard(rows)
            # Once, like the local-hero line. An empty Social tab has two very
            # different causes — the sweep never ran, or it ran and the layer
            # is genuinely just you — and without this they look identical.
            if rows and not shard_seen["n"]:
                shard_seen["n"] = True
                named = sum(1 for r in rows if r.get("k"))
                print(f"[meter] shard roster: {len(rows)} players "
                      f"({named} with a class).", file=sys.stderr)
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
    # Same for the mount/glider configs: the agent boots with both features
    # off, and a saved "on" that never reached it would be a checkbox that
    # lies.
    overlay._push_mount_cfg()
    overlay._push_glider_cfg()
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
            overlay.sounds.close()      # release the MCI devices we opened
        except Exception:
            pass
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
