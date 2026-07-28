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
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import frida

ROOT = Path(__file__).resolve().parent.parent
ANALYSIS = ROOT / "analysis_out"
FRIDA_DIR = ROOT / "frida"
POSITION_CACHE = ROOT / ".meter_position.json"
PARSES_DIR = ROOT / "parses"        # finished-parse images land here (gitignored)
TARGET_PROCESS = "Farever.exe"

# One meter at a time. The lock deliberately lives OUTSIDE the project folder:
# copies of this script run from different directories have to find each other,
# and that's the case that actually bites — an old copy left running while a
# newer one is launched from somewhere else, with only the old overlay on screen
# to show for it. A per-project lock would miss exactly that.
LOCK_DIR = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "FareverMeter"
LOCK_FILE = LOCK_DIR / "instance.json"
# How long to let a running instance shut itself down before forcing it. It has
# to unload the hook and detach, which is the whole point of asking nicely.
QUIT_WAIT_SECS = 12.0

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
)

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
ACCENT = "#3D7C7C"
DMG_BAR = "#5279B5"       # blue — damage bars
HEAL_BAR = "#5E9C4A"      # green — healing bars
TRANSPARENT_KEY = "#010101"

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

SHUTDOWN_WARNING = ("ALWAYS CLOSE FAREVER+ BY CTRL+C "
                    "NOT BY CLOSING THE COMMAND WINDOW")


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
# Overlay
# ---------------------------------------------------------------------------
class Overlay:
    def __init__(self, session: PartySession, target_pid, ui_state=None):
        self.session = session
        self.target_pid = target_pid
        self.ui_state = ui_state if ui_state is not None else GameUIState()
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
        self._action_q = []
        self._q_lock = threading.Lock()
        self._last_epoch = session.epoch
        # Parse mode: None | "countdown" (pre-roll) | "parsing" | "done" (the
        # finished sample, frozen on screen until it's cleared).
        self._parse_state = None
        self._parse_until = 0.0
        # Rift prompt: modal, and the only overlay window on screen while it's
        # up. _rift_seen is the edge detector — one prompt per rift entry.
        self._prompt_open = False
        self._rift_seen = False

        pos = self._load_positions()
        self.root = tk.Tk()
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
        for win in (self.root, self.detail, self.menu, self.hintwin,
                    self.parsewin, self.promptwin):
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
        for win in (self.root, self.detail, self.menu):
            win.attributes("-alpha", OVERLAY_ALPHA)
        # Keys must match TOGGLEABLE_ELEMENTS.
        self._element_win = {"meter": self.root, "detail": self.detail}
        # Every window that fades: the two toggleable ones, plus the control
        # menu and its hint, which follow the game's escape menu.
        self._fade_win = dict(self._element_win, menu=self.menu,
                              hint=self.hintwin, prompt=self.promptwin)
        self._shown["menu"] = self._shown["hint"] = False
        self._shown["prompt"] = False
        # Live opacity of each faded window, driven by _step_fade. The menu pair
        # starts at zero: they're withdrawn until the escape menu opens.
        self._alpha = {k: OVERLAY_ALPHA for k in self._fade_win}
        self._alpha["menu"] = self._alpha["hint"] = 0.0
        self._alpha["prompt"] = 0.0
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
        for key in ("meter", "detail", "menu"):
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
        self.hintwin.geometry(f"+{l + ((r - l) - w) // 2}+{t + 24}")

    def _save_pos(self):
        try:
            POSITION_CACHE.write_text(json.dumps({
                "meter": {"x": self.root.winfo_x(), "y": self.root.winfo_y()},
                "detail": {"x": self.detail.winfo_x(),
                           "y": self.detail.winfo_y()},
                "menu": {"x": self.menu.winfo_x(), "y": self.menu.winfo_y()},
            }))
        except OSError:
            pass

    # ---- UI ----
    def _build_meter(self):
        border = tk.Frame(self.root, bg=BG_BORDER, padx=2, pady=2)
        border.pack(fill="both", expand=True)

        self.header = tk.Frame(border, bg=BG_HEADER)
        self.header.pack(fill="x")
        self.title_lbl = tk.Label(self.header, text="Farever+ Party Meter",
                                  bg=BG_HEADER, fg=FG_HEADER,
                                  font=("Segoe UI", 10, "bold"), anchor="w",
                                  padx=8, pady=4)
        self.title_lbl.pack(side="left")
        self.timer_lbl = tk.Label(self.header, text="", bg=BG_HEADER, fg=FG_HEADER,
                                  font=("Consolas", 9), padx=8)
        self.timer_lbl.pack(side="right")
        self._bind_drag(self.root, (self.header, self.title_lbl))

        body = tk.Frame(border, bg=BG_BODY, padx=8, pady=6)
        body.pack(fill="both", expand=True)

        self.overview_title = tk.Label(body, text="PARTY", bg=BG_BODY,
                                       fg=ACCENT, font=("Segoe UI", 8, "bold"),
                                       anchor="w")
        self.overview_title.pack(fill="x")
        # Same font/size as the rows so the monospace columns line up exactly.
        self.cols_lbl = tk.Label(
            body, text=self._meter_cols_text(),
            bg=BG_BODY, fg=FG_DIM, font=("Consolas", 10), anchor="w")
        self.cols_lbl.pack(fill="x", pady=(2, 0))
        rows_box = tk.Frame(body, bg=BG_BODY)
        rows_box.pack(fill="x", pady=(1, 2))
        # pack stops managing a container the moment its last slave is forgotten
        # — it keeps whatever size it last asked for. Without this 1 px keeper
        # the meter would stay as tall as the biggest party it ever showed once
        # a reset empties the rows. Packed to the bottom so row order is
        # untouched.
        tk.Frame(rows_box, bg=BG_BODY, height=1, width=1).pack(side="bottom")
        self.player_rows = [PlayerRow(rows_box, self._on_row_click)
                            for _ in range(MAX_PLAYER_ROWS)]

        self.root.minsize(360, 0)

    def _meter_cols_text(self):
        head = f"  #  {'NAME':<12}{'DMG':>9} {'DPS':>6} {'%':>4}"
        return head + (f"{'HEAL':>9}" if self._show["healing"] else "")

    def _build_detail(self):
        border = tk.Frame(self.detail, bg=BG_BORDER, padx=2, pady=2)
        border.pack(fill="both", expand=True)

        self.d_header = tk.Frame(border, bg=BG_HEADER)
        self.d_header.pack(fill="x")
        self.d_title = tk.Label(self.d_header, text="Breakdown",
                                bg=BG_HEADER, fg=FG_HEADER,
                                font=("Segoe UI", 10, "bold"), anchor="w",
                                padx=8, pady=4)
        self.d_title.pack(side="left")
        # Sits in the header rather than the body so it reads as a caption on
        # the window instead of another data row. It tints with the header.
        self.d_tip = tk.Label(self.d_header,
                              text="Click a player in the meter to view details",
                              bg=BG_HEADER, fg=FG_HEADER_DIM,
                              font=("Segoe UI", 7, "italic"), anchor="e", padx=8)
        self.d_tip.pack(side="right")
        self._bind_drag(self.detail, (self.d_header, self.d_title, self.d_tip))

        body = tk.Frame(border, bg=BG_BODY, padx=8, pady=6)
        body.pack(fill="both", expand=True)
        self.stats_lbl = tk.Label(body, text="", bg=BG_BODY, fg=FG_TEXT,
                                  font=("Consolas", 9), anchor="w")
        self.stats_lbl.pack(fill="x")

        cols = tk.Frame(body, bg=BG_BODY)
        cols.pack(fill="x", pady=(3, 2))
        self.dmg_col = SkillColumn(cols, "DAMAGE", DMG_BAR)
        self.dmg_col.f.pack(side="left", anchor="n")
        # Kept as attributes so the healing toggle can unpack them; re-packing
        # in this order puts them back to the right of the damage column.
        self.col_sep = tk.Frame(cols, bg=BG_BODY_SOFT, width=1)
        self.col_sep.pack(side="left", fill="y", padx=6)
        self.heal_col = SkillColumn(cols, "HEALING", HEAL_BAR)
        self.heal_col.f.pack(side="left", anchor="n")

        self.elem_lbl = tk.Label(body, text="", bg=BG_BODY, fg=FG_DIM,
                                 font=("Consolas", 8), anchor="w", justify="left")
        self.elem_lbl.pack(fill="x", pady=(3, 0))
        self.detail.minsize(320, 0)

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
                                font=("Segoe UI", 10, "bold"), anchor="w",
                                padx=8, pady=4)
        self.m_title.pack(side="left")
        self._bind_drag(self.menu, (self.m_header, self.m_title))

        body = tk.Frame(border, bg=BG_BODY, padx=8, pady=8)
        body.pack(fill="both", expand=True)

        # Top of the menu, above everything: closing the console window kills the
        # process before it can unload the hook and detach, which is what
        # destabilises the game across relaunches. Worth shouting about — it's
        # the one way to break things that looks like a normal way to quit.
        tk.Label(body, text=SHUTDOWN_WARNING, bg=BG_BODY, fg=FG_WARN,
                 font=("Segoe UI", 8, "bold"), anchor="w", justify="left",
                 wraplength=250).pack(fill="x", pady=(0, 8))
        tk.Frame(body, bg=BG_BAR_TRACK, height=1).pack(fill="x", pady=(0, 2))

        def section(text, first=False):
            tk.Label(body, text=text, bg=BG_BODY, fg=ACCENT,
                     font=("Segoe UI", 8, "bold"), anchor="w").pack(
                         fill="x", pady=(0 if first else 9, 3))

        def button(cmd):
            b = tk.Button(body, text="", command=cmd, anchor="w",
                          font=("Segoe UI", 9), bg=BG_BODY_SOFT, fg=FG_TEXT,
                          activebackground=BG_BAR_TRACK, activeforeground=FG_VALUE,
                          relief="flat", bd=0, padx=10, pady=5,
                          highlightthickness=1, highlightbackground=BG_BAR_TRACK,
                          cursor="hand2")
            b.pack(fill="x", pady=2)
            return b

        # Commands are queued rather than run inline: they mutate overlay state
        # the refresh loop also touches, and _drain runs them on the Tk thread.
        section("OPTIONS", first=True)
        self.btn_mode = button(self._enqueue(self._toggle_mode))
        # Exactly what the hotkey fires, so the two can't diverge. Labelled with
        # the keybind because the hotkey is the one that's useful mid-fight,
        # when the escape menu (and so this button) isn't an option.
        self.btn_reset_data = button(self._enqueue(self.session.reset))
        self.btn_reset_data.config(
            text=f"Reset encounter data   ({RESET_HOTKEY_KEYS})")
        self.btn_reset_pos = button(self._enqueue(self._reset_pos))
        self.btn_reset_pos.config(text="Reset window positions")

        section("SHOW / HIDE")
        self.element_btns = {
            key: button(self._enqueue(lambda k=key: self._toggle_element(k)))
            for key, _label in TOGGLEABLE_ELEMENTS
        }
        # Last in this section rather than under OPTIONS: it hides the same
        # windows the checkboxes above do, just on a condition instead of a
        # click.
        self.btn_hide_ooc = button(self._enqueue(self._toggle_hide_ooc))

        section("ACTIONS")
        self.btn_parse = button(self._enqueue(self._toggle_parse))
        self.btn_parses = button(self._enqueue(self._open_parses))
        self.btn_parses.config(text="Parse Screenshots")
        self.menu.minsize(230, 0)

    def _build_hint(self):
        """The one remaining keybind, as free-floating text over the game — no
        panel, no border, just a drop-shadowed line so it stays readable on
        whatever is behind it."""
        from tkinter import font as tkfont
        f = tkfont.Font(family="Segoe UI", size=11, weight="bold")
        pad, off = 6, 2
        w = f.measure(RESET_HOTKEY_TEXT) + pad * 2 + off
        h = f.metrics("linespace") + pad * 2 + off
        c = tk.Canvas(self.hintwin, width=w, height=h, bg=TRANSPARENT_KEY,
                      highlightthickness=0, bd=0)
        c.pack()
        c.create_text(pad + off, pad + off, text=RESET_HOTKEY_TEXT, font=f,
                      fill=BG_BORDER, anchor="nw")
        c.create_text(pad, pad, text=RESET_HOTKEY_TEXT, font=f,
                      fill=BG_BODY, anchor="nw")

    def _build_prompt(self):
        """The rift prompt: a modal box in the middle of the game window. It is
        the only overlay window on screen while it's up, so there's nothing to
        read or click except the question."""
        border = tk.Frame(self.promptwin, bg=BG_BORDER, padx=2, pady=2)
        border.pack(fill="both", expand=True)
        header = tk.Frame(border, bg=BG_HEADER_UNLOCKED)
        header.pack(fill="x")
        tk.Label(header, text="Farever+", bg=BG_HEADER_UNLOCKED, fg=FG_HEADER,
                 font=("Segoe UI", 10, "bold"), anchor="w",
                 padx=10, pady=5).pack(side="left")

        body = tk.Frame(border, bg=BG_BODY, padx=16, pady=14)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="You have entered a rift", bg=BG_BODY, fg=FG_VALUE,
                 font=("Segoe UI", 12, "bold"), anchor="w").pack(fill="x")
        tk.Label(body, text="Enable 'View All Players'?", bg=BG_BODY,
                 fg=FG_TEXT, font=("Segoe UI", 10), anchor="w",
                 pady=6).pack(fill="x")

        btns = tk.Frame(body, bg=BG_BODY)
        btns.pack(fill="x", pady=(8, 0))

        def answer_button(text, on, yes):
            b = tk.Button(btns, text=text, command=on, font=("Segoe UI", 10, "bold"),
                          bg=BTN_ON_BG if yes else BG_BODY_SOFT,
                          fg=FG_HEADER if yes else FG_TEXT,
                          activebackground=BTN_ON_BG_ACTIVE if yes else BG_BAR_TRACK,
                          activeforeground=FG_HEADER if yes else FG_VALUE,
                          relief="flat", bd=0, padx=26, pady=7,
                          highlightthickness=1, cursor="hand2",
                          highlightbackground=BTN_ON_BG_ACTIVE if yes
                          else BG_BAR_TRACK)
            b.pack(side="left", expand=True, fill="x", padx=3)
            return b

        answer_button("Yes", self._enqueue(lambda: self._answer_rift(True)), True)
        answer_button("No", self._enqueue(lambda: self._answer_rift(False)), False)
        self.promptwin.minsize(300, 0)

    def _open_rift_prompt(self):
        self._prompt_open = True
        self.promptwin.update_idletasks()
        l, t, r, b = self._game_rect()
        w = max(self.promptwin.winfo_reqwidth(), 300)
        h = max(self.promptwin.winfo_reqheight(), 120)
        self.promptwin.geometry(f"+{l + ((r - l) - w) // 2}+{t + ((b - t) - h) // 2}")
        self._apply_clickthrough()      # the prompt has to be clickable
        self._refresh_visibility()
        print("[meter] entered a rift — asking about View All Players.",
              file=sys.stderr)

    def _close_rift_prompt(self):
        self._prompt_open = False
        self._refresh_visibility()

    def _answer_rift(self, yes):
        """Yes switches to all-players. That resets the encounter the same way
        the mode button does — party-only and all-players numbers can't share
        one encounter without the percentages lying."""
        if yes and self.mode != "all":
            self.mode = "all"
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
            self._open_rift_prompt()
        elif not in_rift:
            self._rift_seen = False
            if self._prompt_open:
                self._close_rift_prompt()

    def _build_parse(self):
        """The parse banner — the same drop-shadowed floating text as the
        keybind hint, but its content changes every second, so the canvas and
        the window are re-measured on each update instead of sized once."""
        from tkinter import font as tkfont
        self._parse_font = tkfont.Font(family="Segoe UI", size=15, weight="bold")
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
        # Below the keybind hint, which owns the strip at t+24 whenever the
        # escape menu is open — and it is, for the first few seconds of a parse.
        self.parsewin.geometry(f"+{l + ((r - l) - w) // 2}+{t + 64}")

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
        if isinstance(win, (tk.Tk, tk.Toplevel)):
            self._round_win_corners(win)

    def _apply_clickthrough(self):
        locked = self._is_locked()
        for win in (self.root, self.detail):
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
        hidden = ooc_hidden or menu_hidden or self._prompt_open
        changed = False
        for key in self._element_win:
            changed |= self._want_visible(
                key, (self._show[key] or self._menu_unlock) and not hidden)
        menu_visible = self._menu_unlock and not self._prompt_open
        changed |= self._want_visible("menu", menu_visible)
        changed |= self._want_visible("hint", menu_visible)
        changed |= self._want_visible("prompt", self._prompt_open)
        if changed:
            self._start_fade()

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
        # Before the epoch check below: starting a parse resets the session
        # itself, and syncs _last_epoch so that isn't mistaken for the player
        # resetting back out of parse mode.
        self._tick_rift()
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
        unlocked = not self._is_locked()
        want_bg = (BG_HEADER_UNLOCKED if unlocked
                   else BG_HEADER_COMBAT if in_combat else BG_HEADER)
        if want_bg != self._header_bg:
            for w in (self.header, self.title_lbl, self.timer_lbl,
                      self.d_header, self.d_title, self.d_tip):
                w.config(bg=want_bg)
            self._header_bg = want_bg

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


def _clamp01(v):
    return min(1.0, max(0.0, v))


class PlayerRow:
    """One meter line (rank, name, damage, dps, %, healing) over a stacked
    bar pair: damage (blue, top) and healing (green, bottom)."""

    def __init__(self, parent, on_click):
        self.f = tk.Frame(parent, bg=BG_BODY)
        self.line = tk.Label(self.f, text="", bg=BG_BODY, fg=FG_TEXT,
                             font=("Consolas", 10), anchor="w")
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
        self.line.config(text=line, fg=FG_VALUE if p.is_me else FG_TEXT)
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

    def __init__(self, parent, title, bar_color):
        self.f = tk.Frame(parent, bg=BG_BODY)
        tk.Label(self.f, text=title, bg=BG_BODY, fg=ACCENT,
                 font=("Segoe UI", 8, "bold"), anchor="w").pack(fill="x")
        self.rows = []
        for _ in range(MAX_SKILL_ROWS):
            rf = tk.Frame(self.f, bg=BG_BODY)
            lbl = tk.Label(rf, text="", bg=BG_BODY, fg=FG_TEXT,
                           font=("Consolas", 9), anchor="w")
            lbl.pack(fill="x")
            track = tk.Frame(rf, bg=BG_BAR_TRACK, height=4)
            track.pack(fill="x", pady=(0, 1))
            bar = tk.Frame(track, bg=bar_color, height=4)
            bar.place(relwidth=0.0, relheight=1.0)
            self.rows.append((rf, lbl, bar))
        self._shown = 0

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
            _rf, lbl, bar = self.rows[i]
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
    for t in tools:
        print(f"[meter] regenerating {t.name} for this build ...", file=sys.stderr)
        cmd = [sys.executable, str(t)]
        if hlboot is not None:
            cmd.append(str(hlboot))
        r = subprocess.run(cmd, capture_output=True, text=True)
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
              "(launch the game; Ctrl+C to quit) ...", file=sys.stderr)
        while not procs:
            time.sleep(1.5)
            procs = matches()
        print(f"[*] {TARGET_PROCESS} is up.", file=sys.stderr)
    if len(procs) == 1:
        return procs[0]
    infos = [(p, _exe_path_of_pid(p.pid)) for p in procs]
    print(f"[*] {len(infos)} {TARGET_PROCESS} processes found:", file=sys.stderr)
    for i, (p, path) in enumerate(infos, 1):
        print(f"      {i}. pid {p.pid:>6}  {path or '(path unavailable)'}",
              file=sys.stderr)
    while True:
        try:
            ans = input(f"    Which one is your game? [1-{len(infos)}] "
                        "(Enter = 1): ").strip()
        except EOFError:
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

    try:
        other = json.loads(LOCK_FILE.read_text())
    except Exception:
        other = {}
    pid = int(other.get("pid") or 0)
    if pid and pid != os.getpid() and _process_alive(pid):
        image = _process_image(pid)
        if "python" in Path(image).name.lower():
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
    while True:
        try:
            d = input("    Where is Farever installed? (folder containing "
                      "hlboot.dat, Enter to skip): ").strip().strip('"')
        except EOFError:
            return None
        if not d:
            return None
        p = Path(d)
        cand = p if p.is_file() else p / "hlboot.dat"
        if cand.is_file():
            return cand
        print(f"    [!] no hlboot.dat at {p}", file=sys.stderr)


def main():
    claim_single_instance()
    session = PartySession()
    ui_state = GameUIState()

    device = frida.get_local_device()
    proc = find_game_process(device)
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

    if script is None or ready["ok"] is not True:
        print("[meter] could not initialise the hook after 3 attempts.\n"
              "        Fully close Farever and reopen it, then relaunch the meter.\n"
              "        If it keeps happening, send the full log above to whoever\n"
              "        gave you the meter — the [hook] lines say where it stopped.\n"
              "        (Avoid repeatedly relaunching against a stuck session — "
              "that can crash the game.)", file=sys.stderr)

    print("[*] overlay starting. Open the game's escape menu for the control "
          "menu (and to drag the windows / click a row to inspect). Only "
          "hotkey: Shift+\\ resets the encounter.", file=sys.stderr)

    overlay = Overlay(session, pid, ui_state)
    try:
        overlay.run()
    finally:
        try:
            script.unload()
            fsession.detach()
        except Exception:
            pass
        release_instance_lock()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # Ctrl+C is the documented way to stop the meter, so it shouldn't look
        # like a crash: main()'s finally has already unloaded the hook and
        # detached by the time this runs. Printing (and exiting 0) also lets a
        # launcher tell a normal stop from a real failure.
        print("[meter] stopped.", file=sys.stderr)
