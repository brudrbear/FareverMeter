"""
live_meter_simple.py — damage-only variant of the Farever combat meter.

Stripped-down version of live_meter.py:
  - Tracks outgoing damage only (no healing, absorb, damage-taken).
  - No per-target breakdown / entity name resolution.
  - No mob database, no questlog.gg lookups — fully offline.

Keeps the same Frida + ssl.hdll hook plumbing and the TX-based player-ID
detection (without that, source/target classification breaks).

Usage:
  python live_meter_simple.py
"""
from __future__ import annotations

import ctypes
import json
import math
import os
import queue
import re
import struct
import sys
import threading
import time
import tkinter as tk
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import frida

from event_parser import scan_events, get_entity_id_from_tx, COMBAT_EVENT_NAMES

VERSION = "1.3"

TARGET_PROCESS = "Farever.exe"
HOOK_SCRIPT_PATH = Path(__file__).with_name("hook_ssl.js")
# Persists the meter's top-left corner across restarts. Written on drag-end,
# read on startup, deleted on Shift+/ (reset position).
POSITION_CACHE_PATH = Path(__file__).with_name(".meter_position.json")
# 30s Parse Mode saves a styled PNG of each completed parse here. Folder is
# created on first save; the meter never touches it otherwise.
PARSE_SCREENSHOTS_DIR = Path(__file__).with_name("parse_screenshots")


def _load_position() -> tuple[int, int] | None:
    """Return the cached (x, y) if a valid file exists, else None."""
    try:
        raw = POSITION_CACHE_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        x = int(data["x"])
        y = int(data["y"])
        return (x, y)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"[meter] ignoring bad position cache: {e}", file=sys.stderr)
        return None


def _save_position(x: int, y: int) -> None:
    try:
        POSITION_CACHE_PATH.write_text(
            json.dumps({"x": int(x), "y": int(y)}), encoding="utf-8")
    except OSError as e:
        print(f"[meter] failed to save position: {e}", file=sys.stderr)


def _clear_position_cache() -> None:
    try:
        POSITION_CACHE_PATH.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"[meter] failed to clear position cache: {e}", file=sys.stderr)

# ---- Player-detection tunables ----
PLAYER_BLOCK_TX_RADIUS = 1000
# Tight radius — sub-entities (projectiles, talent procs) sit very close to the
# player main entity ID. The original 5000 caught ambient NPCs in populated
# areas and leaked their damage into the meter. Widen only if real damage is
# being missed (enable FAREVER_METER_DEBUG=1 to see what's credited).
PLAYER_SUBENTITY_RADIUS = 50
SESSION_RESET_IDLE_SECS = 20.0
TX_WINDOW_SECS = 30.0
PLAYER_ID_JUMP_THRESHOLD = 100_000

# Set FAREVER_METER_DEBUG=1 in the environment to print every credited damage
# event to stderr. Useful for finding leaks (events being attributed to the
# player that shouldn't be) or losses (real damage being filtered out).
DEBUG = os.environ.get("FAREVER_METER_DEBUG") == "1"

# ---- Combat-session tunables ----
# Seconds of no combat events before the current encounter is considered fully
# closed. The next damage event after this gap wipes the current view and
# starts a fresh encounter. Set high enough to survive boss intermissions
# (knockback phases, untargetable casts, etc.) — otherwise a long intermission
# resets the meter mid-fight.
COMBAT_TIMEOUT_SECS = 30.0
ACTIVE_PROXIMITY_SECS = 2.0

# ---- 30s Parse Mode tunables ----
# When parse mode is toggled on (Ctrl+\), the meter records at most this many
# seconds of combat (timer starts on first damage event) and then freezes.
# Subsequent damage events are dropped until Shift+\ hard-resets for another
# parse, or Ctrl+\ toggles parse mode off. Useful for target-dummy damage
# checks where you want a clean 30-second window every time.
PARSE_MODE_DURATION_SECS = 30.0

# ---- Overlay tunables ----
REFRESH_MS = 250
MAX_ROWS = 10
MARGIN_FROM_TOP = 10
MARGIN_FROM_RIGHT = 10

# Farever-style palette
BG_BORDER    = "#2C1A0E"
BG_BODY      = "#F2E1CB"
BG_BODY_SOFT = "#E8D5B8"
BG_HEADER    = "#54A4A9"
BG_HEADER_COMBAT = "#C9612A"   # warm red-orange — header turns this in combat
BG_BAR_TRACK = "#D9C09A"
FG_HEADER    = "#FFFFFF"
FG_TEXT      = "#3D2817"
FG_VALUE     = "#1F1208"
FG_DIM       = "#7B5A3A"
ACCENT       = "#3D7C7C"
# Magic color used by Tk's `-transparentcolor` attribute on Windows. Any pixel
# of this exact color in the window becomes fully transparent. Near-black is
# chosen so that white-text anti-aliasing blends toward dark instead of toward
# magenta — without this, AA fringe shows up as a pink outline around the
# floating hint text. None of our palette colors are exactly #010101 so this
# can't accidentally punch holes elsewhere.
TRANSPARENT_KEY = "#010101"

TYPE_COLORS = {
    "Physical": "#B68A4E",
    "Magic":    "#5279B5",
    "Fire":     "#C9612A",
    "Spark":    "#D9B43C",
    "Earth":    "#7C5A2E",
    "Water":    "#4B8FB5",
    "Faith":    "#C8B280",
    "Light":    "#E5C95A",
    "Raw":      "#8A6A4A",
    "Cheese":   "#D8C25E",
}

# Windows click-through
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000


def _set_window_clickthrough(hwnd: int, enabled: bool) -> None:
    """Toggle click-through on a Windows top-level window.

    When `enabled` is True, mouse events pass through the window to whatever
    is behind it. When False, the window receives clicks normally (so it can
    be dragged). NOACTIVATE stays on either way so clicks don't steal focus
    from the game.
    """
    if sys.platform != "win32":
        return
    user32 = ctypes.windll.user32
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        get_long = user32.GetWindowLongPtrW
        set_long = user32.SetWindowLongPtrW
    else:
        get_long = user32.GetWindowLongW
        set_long = user32.SetWindowLongW
    extended = get_long(hwnd, GWL_EXSTYLE)
    extended |= WS_EX_LAYERED | WS_EX_NOACTIVATE
    if enabled:
        extended |= WS_EX_TRANSPARENT
    else:
        extended &= ~WS_EX_TRANSPARENT
    set_long(hwnd, GWL_EXSTYLE, extended)


def _make_window_clickthrough(hwnd: int) -> None:
    _set_window_clickthrough(hwnd, True)


# ---- Global hotkey support (Windows only) ----
# Click-through windows never receive keyboard focus, so tk bindings can't see
# keypresses. We prefer a WH_KEYBOARD_LL hook so hotkeys only fire when the
# target process (Farever) owns the foreground window — that way, typing in
# our own popup (Toplevel) lets `\` and `/` through to the text fields
# instead of being eaten as toggle/lock commands. Falls back to RegisterHotKey
# (which fires globally regardless of focus) if the LL hook can't install.
WM_HOTKEY = 0x0312
MOD_SHIFT = 0x0004
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
VK_OEM_5 = 0xDC  # the '\|' key on a US keyboard layout
VK_OEM_2 = 0xBF  # the '/?' key on a US keyboard layout
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt

# Low-level hook constants
WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

HOTKEY_TOGGLE = 1
HOTKEY_RESET = 2
HOTKEY_LOCK = 3
HOTKEY_RESET_POS = 4
HOTKEY_PARSE = 5


def _start_global_hotkeys(callbacks: dict[int, callable],
                          target_pid: int | None = None) -> None:
    """Spawn a daemon thread that listens for our global hotkeys.

    When `target_pid` is provided AND we can install a WH_KEYBOARD_LL hook,
    hotkeys only fire when that PID owns the foreground window — typing into
    our own popup window passes through cleanly. Falls back to RegisterHotKey
    on hook install failure or when no PID is supplied; in that mode, hotkeys
    fire globally and will eat `\\` keystrokes from any focused app.

    `callbacks` maps a HOTKEY_* id to a no-arg function (called on the hook
    thread — callers must marshal back to the tk main thread if needed).

    No-op on non-Windows platforms.
    """
    if sys.platform != "win32":
        print("[meter] global hotkeys unsupported on this platform.",
              file=sys.stderr)
        return

    def _pump():
        from ctypes import wintypes
        user32 = ctypes.windll.user32

        # ---- Try focus-conditional WH_KEYBOARD_LL hook first ----
        if target_pid is not None:
            if _run_focused_keyboard_hook(user32, callbacks, target_pid):
                return  # message loop exited cleanly
            # else: install failed; fall through to RegisterHotKey

        # ---- RegisterHotKey fallback (focus-blind, global) ----
        if not user32.RegisterHotKey(None, HOTKEY_TOGGLE,
                                     MOD_NOREPEAT, VK_OEM_5):
            print("[meter] failed to register '\\' hotkey "
                  "(another app may own it).", file=sys.stderr)
        if not user32.RegisterHotKey(None, HOTKEY_RESET,
                                     MOD_SHIFT | MOD_NOREPEAT, VK_OEM_5):
            print("[meter] failed to register 'Shift+\\' hotkey "
                  "(another app may own it).", file=sys.stderr)
        if not user32.RegisterHotKey(None, HOTKEY_LOCK,
                                     MOD_NOREPEAT, VK_OEM_2):
            print("[meter] failed to register '/' hotkey "
                  "(another app may own it).", file=sys.stderr)
        if not user32.RegisterHotKey(None, HOTKEY_RESET_POS,
                                     MOD_SHIFT | MOD_NOREPEAT, VK_OEM_2):
            print("[meter] failed to register 'Shift+/' hotkey "
                  "(another app may own it).", file=sys.stderr)
        if not user32.RegisterHotKey(None, HOTKEY_PARSE,
                                     MOD_CONTROL | MOD_NOREPEAT, VK_OEM_5):
            print("[meter] failed to register 'Ctrl+\\' hotkey "
                  "(another app may own it).", file=sys.stderr)

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                cb = callbacks.get(msg.wParam)
                if cb is not None:
                    try:
                        cb()
                    except Exception as e:
                        print(f"[!] hotkey handler error: {e}",
                              file=sys.stderr)
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    threading.Thread(target=_pump, daemon=True,
                     name="hotkey-pump").start()


def _run_focused_keyboard_hook(user32, callbacks: dict, target_pid: int) -> bool:
    """Install WH_KEYBOARD_LL and run a Windows message loop until exit.

    Returns False if the hook can't install (caller falls back to RegisterHotKey).
    Returns True after the message loop exits cleanly (shutdown).

    The hook function inspects every keydown system-wide but only acts on our
    five binds AND only when GetForegroundWindow's PID matches `target_pid`.
    Everything else passes through unchanged so unrelated apps (and our own
    popup window when it has focus) keep working normally.
    """
    from ctypes import wintypes

    # Proper Win64 signatures — without these, LRESULT returns truncate to
    # 32 bits and pointers come back garbled.
    LRESULT = ctypes.c_ssize_t
    HHOOK = ctypes.c_void_p

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode", wintypes.DWORD),
            ("scanCode", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_void_p),
        ]

    HOOKPROC = ctypes.WINFUNCTYPE(
        LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

    user32.SetWindowsHookExW.restype = HHOOK
    user32.SetWindowsHookExW.argtypes = [
        ctypes.c_int, HOOKPROC, ctypes.c_void_p, wintypes.DWORD]
    user32.CallNextHookEx.restype = LRESULT
    user32.CallNextHookEx.argtypes = [
        HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    user32.UnhookWindowsHookEx.restype = ctypes.c_int
    user32.UnhookWindowsHookEx.argtypes = [HHOOK]
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]

    keys_down: set[int] = set()

    def _is_pressed(vk: int) -> bool:
        return bool(user32.GetAsyncKeyState(vk) & 0x8000)

    def _foreground_pid() -> int:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return 0
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    def hook_proc(nCode, wParam, lParam):
        if nCode != HC_ACTION:
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        kbd = ctypes.cast(
            lParam, ctypes.POINTER(KBDLLHOOKSTRUCT))[0]
        vk = kbd.vkCode

        # Track key state so auto-repeat (still keydown after first press)
        # doesn't re-fire our binds.
        if wParam in (WM_KEYUP, WM_SYSKEYUP):
            keys_down.discard(vk)
            return user32.CallNextHookEx(None, nCode, wParam, lParam)
        if wParam not in (WM_KEYDOWN, WM_SYSKEYDOWN):
            return user32.CallNextHookEx(None, nCode, wParam, lParam)
        if vk in keys_down:
            return user32.CallNextHookEx(None, nCode, wParam, lParam)
        keys_down.add(vk)

        # Bail fast for keys that aren't one of ours.
        if vk not in (VK_OEM_5, VK_OEM_2):
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        # The whole point: only fire when Farever has the foreground.
        if _foreground_pid() != target_pid:
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        shift = _is_pressed(VK_SHIFT)
        ctrl = _is_pressed(VK_CONTROL)
        alt = _is_pressed(VK_MENU)
        # Alt+anything isn't a bind we care about — let it pass.
        if alt:
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        action = None
        if vk == VK_OEM_5:  # backslash family
            if ctrl and not shift:
                action = HOTKEY_PARSE
            elif shift and not ctrl:
                action = HOTKEY_RESET
            elif not shift and not ctrl:
                action = HOTKEY_TOGGLE
        elif vk == VK_OEM_2:  # forward-slash family
            if shift and not ctrl:
                action = HOTKEY_RESET_POS
            elif not shift and not ctrl:
                action = HOTKEY_LOCK

        if action is not None:
            cb = callbacks.get(action)
            if cb is not None:
                try:
                    cb()
                except Exception as e:
                    print(f"[!] hotkey handler error: {e}",
                          file=sys.stderr)
            return 1  # consume so Farever doesn't also see the key

        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    # Bind the WINFUNCTYPE wrapper to a local to keep it alive for the
    # lifetime of the hook (GC'd callback = crash on next key event).
    c_hook_proc = HOOKPROC(hook_proc)

    kernel32 = ctypes.windll.kernel32
    hMod = kernel32.GetModuleHandleW(None)
    hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, c_hook_proc, hMod, 0)
    if not hook:
        err = ctypes.get_last_error()
        print(f"[meter] LL keyboard hook install failed (errno {err}); "
              "falling back to global RegisterHotKey.", file=sys.stderr)
        return False

    print(f"[meter] focus-conditional hotkeys installed "
          f"(active only while Farever PID {target_pid} owns foreground).",
          file=sys.stderr)

    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))

    user32.UnhookWindowsHookEx(hook)
    return True


# ---------------------------------------------------------------------------
# Damage-only session tracker
# ---------------------------------------------------------------------------
@dataclass
class DamageAggregate:
    damage: dict[str, list[float]] = field(
        default_factory=lambda: defaultdict(lambda: [0, 0.0]))
    # 1-second buckets keyed by integer seconds since start_time. Feeds the
    # timeline graph in both the live overlay and the saved screenshot.
    timeline: dict[int, float] = field(
        default_factory=lambda: defaultdict(float))
    start_time: float = 0.0
    end_time: float = 0.0
    duration: float = 0.0

    def reset(self, now: float):
        self.damage.clear()
        self.timeline.clear()
        self.start_time = now
        self.end_time = now
        self.duration = 0.0

    def total(self) -> float:
        return sum(t for _, t in self.damage.values())

    def dps(self) -> float:
        return self.total() / self.duration if self.duration > 0 else 0.0

    def breakdown(self) -> list[tuple[str, int, float]]:
        rows = [(dt, c, t) for dt, (c, t) in self.damage.items()]
        rows.sort(key=lambda r: -r[2])
        return rows


class DamageSession:
    def __init__(self, combat_timeout: float = COMBAT_TIMEOUT_SECS):
        self.lock = threading.Lock()
        self.combat_timeout = combat_timeout
        self.current = DamageAggregate()
        self.session = DamageAggregate()
        self.session.start_time = time.time()
        self.in_combat = False
        self.last_event_time = 0.0
        # 30s parse mode: timer arms when parse_mode flips on, but parse_start
        # only advances off 0.0 when the FIRST damage event arrives — so idle
        # time before pulling the dummy doesn't eat into the 30s window.
        self.parse_mode = False
        self.parse_start = 0.0
        self.parse_frozen = False

    def record_damage(self, dmg_type: str, value: float):
        with self.lock:
            now = time.time()
            if self.parse_mode:
                if self.parse_frozen:
                    return
                if self.parse_start == 0.0:
                    self.parse_start = now
                elif now - self.parse_start >= PARSE_MODE_DURATION_SECS:
                    self.parse_frozen = True
                    self.in_combat = False
                    return
            if not self.in_combat and self.last_event_time > 0:
                self.current.reset(now)
            self.in_combat = True
            # Hoisted above the loop so the timeline-bucket calc below sees a
            # valid start_time on the very first event of an encounter.
            if self.current.start_time == 0:
                self.current.start_time = now
            for agg in (self.current, self.session):
                agg.damage[dmg_type][0] += 1
                agg.damage[dmg_type][1] += value
                if agg.start_time > 0:
                    bucket = max(0, int(now - agg.start_time))
                    agg.timeline[bucket] += value
            self.last_event_time = now
            self.current.end_time = now
            self.current.duration = max(0.001, now - self.current.start_time)
            self.session.end_time = now
            self.session.duration = now - self.session.start_time

    def tick(self):
        with self.lock:
            now = time.time()
            if self.in_combat and (now - self.last_event_time) > self.combat_timeout:
                self.in_combat = False
            # Freeze parse mode even if no event has arrived since the deadline,
            # so the UI doesn't sit on a stale "Logging…" title past 30s.
            if (self.parse_mode and not self.parse_frozen
                    and self.parse_start > 0
                    and now - self.parse_start >= PARSE_MODE_DURATION_SECS):
                self.parse_frozen = True
                self.in_combat = False

    def reset_all(self):
        """Wipe both current encounter and overall session totals."""
        with self.lock:
            now = time.time()
            self.current.reset(now)
            # Clear current.start_time so the encounter clock doesn't start
            # ticking until the first new damage event arrives. Without this,
            # if there's a gap between reset and next combat (e.g. boss
            # intermission, walking to the next pack), the first hit would
            # be credited with `now - reset_time` seconds of duration, which
            # makes the meter look like it skipped that gap and tanks DPS.
            self.current.start_time = 0.0
            self.session.reset(now)
            self.in_combat = False
            self.last_event_time = 0.0
            # Hard reset re-arms parse mode (parse_mode flag itself is sticky
            # — toggling it is a separate explicit action via Ctrl+\).
            self.parse_start = 0.0
            self.parse_frozen = False

    def set_parse_mode(self, enabled: bool):
        """Toggle 30s parse mode. Caller is responsible for calling reset_all()
        if they want a clean slate (the overlay does this on toggle)."""
        with self.lock:
            self.parse_mode = enabled
            self.parse_start = 0.0
            self.parse_frozen = False

    def parse_state(self) -> tuple[bool, bool, float]:
        """Return (enabled, frozen, elapsed_seconds_since_first_hit)."""
        with self.lock:
            if not self.parse_mode:
                return (False, False, 0.0)
            elapsed = (time.time() - self.parse_start) if self.parse_start > 0 else 0.0
            return (True, self.parse_frozen, elapsed)

    def snapshot(self, view: str = "current") -> DamageAggregate:
        with self.lock:
            src = self.current if view == "current" else self.session
            snap = DamageAggregate()
            for k, v in src.damage.items():
                snap.damage[k] = list(v)
            # Shallow copy is fine — buckets are plain floats keyed by int.
            for k, v in src.timeline.items():
                snap.timeline[k] = v
            snap.start_time = src.start_time
            snap.end_time = src.end_time
            if view == "current" and self.in_combat and src.start_time > 0:
                now = time.time()
                idle = now - self.last_event_time
                if idle <= ACTIVE_PROXIMITY_SECS:
                    snap.duration = max(0.001, now - src.start_time)
                else:
                    snap.duration = src.duration
            else:
                snap.duration = src.duration
            return snap

    def status(self) -> tuple[bool, float]:
        with self.lock:
            return (self.in_combat,
                    time.time() - self.last_event_time if self.last_event_time else 0)


# ---------------------------------------------------------------------------
# Frida bytes-in / damage-out backend
# ---------------------------------------------------------------------------
class MeterBackend:
    """Same TX-based player detection as live_meter.py, but only emits
    outgoing-damage events into the DamageSession. No name registry, no
    target tracking, no absorb/heal handling."""

    def __init__(self, session: DamageSession):
        self.session = session
        self.tx_history: "deque[tuple[float, int]]" = deque()
        self.tx_counter: dict[int, int] = {}
        self.player_id: int | None = None
        self.player_block: set[int] = set()
        self._player_lock = threading.Lock()
        self._damaged_targets: set[int] = set()
        self._last_tx_time: float = 0.0
        # Diagnostics — counted unconditionally, printed only in DEBUG mode.
        self._tx_packets = 0
        self._rx_packets = 0
        self._rx_combat_events = 0       # events matching COMBAT_EVENT_NAMES
        self._rx_credited = 0            # events that passed classification
        self._rx_dropped_src = 0         # source not classified as player
        self._rx_dropped_tgt = 0         # target classified as player (self-hit)
        self._last_heartbeat = 0.0

    def reset_player_detection(self):
        """Partial reset of player detection state.

        Wipes the TX window and the damaged-targets exclusion set so the
        displayed player_id can be re-evaluated from fresh TX traffic and
        previously-misclassified mobs get a second look. *Does not* clear
        `player_block` — that's the accumulated set of "this is the player"
        evidence built up over the session, and clearing it on reset loses
        IDs that aren't currently in the TX stream (the cross-cluster drift
        case where the auth/sync entity has moved on but the combat-source
        entity is still active). For a true nuke, restart the meter.
        """
        with self._player_lock:
            self.tx_history.clear()
            self.tx_counter.clear()
            self.player_id = None
            # Intentionally preserved: self.player_block
            self._damaged_targets.clear()
            self._last_tx_time = time.time()

    def consume_tx(self, payload: bytes):
        self._tx_packets += 1
        self._maybe_heartbeat()
        # HTTP GET = dungeon/zone transition → wipe state
        if payload[:4] == b"GET ":
            print("[meter] zone transition (HTTP GET) — resetting.", file=sys.stderr)
            self.reset_player_detection()
            return

        eid = get_entity_id_from_tx(payload)
        if eid is None:
            return
        now = time.time()
        with self._player_lock:
            if self._last_tx_time > 0 and now - self._last_tx_time > SESSION_RESET_IDLE_SECS:
                print(f"[meter] TX idle {now - self._last_tx_time:.1f}s — resetting.",
                      file=sys.stderr)
                self.tx_history.clear()
                self.tx_counter.clear()
                self.player_id = None
                self.player_block.clear()
                self._damaged_targets.clear()
            self._last_tx_time = now

            self.tx_history.append((now, eid))
            cutoff = now - TX_WINDOW_SECS
            while self.tx_history and self.tx_history[0][0] < cutoff:
                self.tx_history.popleft()

            counter = Counter(e for _, e in self.tx_history)
            self.tx_counter = dict(counter)
            if not counter:
                return
            top, top_count = counter.most_common(1)[0]
            # Fixed threshold: a couple of TX appearances filters parser noise
            # without locking out legitimate sub-entities that appear less
            # often than the main entity. Earlier proportional scaling
            # (top_count // 20) demanded dozens of TX hits before including a
            # sub-entity, which caused damage from fresh sub-entities to be
            # dropped until they "caught up".
            threshold = 3

            if (self.player_id is not None
                    and abs(top - self.player_id) > PLAYER_ID_JUMP_THRESHOLD):
                print(f"[meter] player ID jumped {self.player_id} → {top} — clearing.",
                      file=sys.stderr)
                self._damaged_targets.clear()

            self.player_id = top
            # player_block is CUMULATIVE within a session. Once an entity has
            # earned its way in via TX evidence (threshold appearances or a
            # recent grace-window hit), it stays until reset_player_detection.
            #
            # Why: the player can control multiple entity IDs spread far apart
            # (observed ~90k deltas between auth/sync and combat entities).
            # The TX-dominant ID drifts between them over a session. If we
            # rebuild player_block from scratch each tick, the drift drops the
            # previous (still active) entity out — and any damage it deals
            # gets misclassified as someone else's. That's the "meter stops
            # mid-fight" bug.
            #
            # No radius filter on the threshold-based add because TX is clean:
            # only the player's own client emits TX, so every TX entity is
            # ours by construction. The grace-window add keeps the radius
            # filter because it's a lower-confidence inclusion (1 hit suffices).
            for e, n in counter.items():
                if n >= threshold:
                    self.player_block.add(e)
            recent_cutoff = now - 5.0
            for t, e in self.tx_history:
                if t >= recent_cutoff and abs(e - top) <= PLAYER_BLOCK_TX_RADIUS:
                    self.player_block.add(e)
            # If any entity we previously labelled "mob" is now in the player
            # block (TX evidence is stronger than the heuristic), un-label it
            # so its damage events start counting again.
            if self.player_block & self._damaged_targets:
                self._damaged_targets -= self.player_block

    def _is_player(self, entity_id: int) -> bool:
        if not self.player_block:
            return False
        if entity_id in self.player_block:
            return True
        if entity_id in self._damaged_targets:
            return False
        if self.player_id is None:
            return False
        if abs(entity_id - self.player_id) > PLAYER_SUBENTITY_RADIUS:
            return False
        return True

    def consume_rx(self, payload: bytes):
        self._rx_packets += 1
        self._maybe_heartbeat()
        if self.player_id is None:
            return
        parsed = list(scan_events(payload))

        # Pre-pass: anything the player damages is a mob, not the player.
        # IMPORTANT: skip targets that are also in player_block. Some procs/
        # chains/AOE ticks legitimately land on the player's own sub-entities,
        # and once such an ID lands in damaged_targets it's excluded forever —
        # which is what produces the "worked for a while then stopped"
        # classification failure.
        for ev in parsed:
            if ev.name in ("Damage", "BonusDamage", "Projectile"):
                if (ev.source in self.player_block
                        and ev.target not in self.player_block):
                    self._damaged_targets.add(ev.target)

        for ev in parsed:
            if ev.name not in COMBAT_EVENT_NAMES:
                continue
            self._rx_combat_events += 1
            base_kind = COMBAT_EVENT_NAMES[ev.name]
            src_p = self._is_player(ev.source)
            tgt_p = self._is_player(ev.target)

            credited = False
            if base_kind == "damage":
                if src_p and not tgt_p:
                    self._credit(ev, "damage")
                    credited = True
            else:
                # Farever quirk: projectile damage is encoded as a Heal event
                # with source=player + target=enemy. Reclassify only that case;
                # ignore real heals entirely in this damage-only build.
                if src_p and not tgt_p:
                    self._credit(ev, "heal-as-projectile")
                    credited = True

            if not credited:
                if not src_p:
                    self._rx_dropped_src += 1
                elif tgt_p:
                    self._rx_dropped_tgt += 1
                if DEBUG:
                    reason = (
                        "src_not_player" if not src_p else "tgt_is_player"
                    )
                    print(f"[meter:dbg]              DROPPED {ev.name:<12} "
                          f"{ev.dmg_type:<8} {ev.value:>7.1f}  "
                          f"src={ev.source} tgt={ev.target}  reason={reason}",
                          file=sys.stderr)

    def _credit(self, ev, classification: str):
        self._rx_credited += 1
        self.session.record_damage(ev.dmg_type, ev.value)
        if DEBUG:
            src_origin = "TX-block" if ev.source in self.player_block else "radius"
            print(f"[meter:dbg] {classification:>20} {ev.dmg_type:<8} "
                  f"{ev.value:>7.1f}  src={ev.source} ({src_origin})  "
                  f"tgt={ev.target}", file=sys.stderr)

    def _maybe_heartbeat(self):
        """Print a periodic state dump in DEBUG mode. Cheap when off."""
        if not DEBUG:
            return
        now = time.time()
        if now - self._last_heartbeat < 5.0:
            return
        self._last_heartbeat = now
        idle = (now - self._last_tx_time) if self._last_tx_time else float("inf")
        print(f"[meter:hb] tx={self._tx_packets} rx={self._rx_packets} "
              f"combat_evts={self._rx_combat_events} credited={self._rx_credited} "
              f"dropped(src!=p)={self._rx_dropped_src} dropped(self-hit)={self._rx_dropped_tgt} "
              f"player_id={self.player_id} pblock={len(self.player_block)} "
              f"dmg_tgts={len(self._damaged_targets)} tx_idle={idle:.1f}s",
              file=sys.stderr)


# ---------------------------------------------------------------------------
# Minimal damage-only overlay
# ---------------------------------------------------------------------------
class _Row:
    def __init__(self, parent: tk.Frame):
        self.frame = tk.Frame(parent, bg=BG_BODY)
        self.label = tk.Label(self.frame, text="", bg=BG_BODY, fg=FG_TEXT,
                              font=("Consolas", 10), anchor="w")
        self.label.pack(fill="x", padx=2)
        self.bar_bg = tk.Frame(self.frame, bg=BG_BAR_TRACK, height=5,
                               highlightthickness=1,
                               highlightbackground=BG_BODY_SOFT)
        self.bar_bg.pack(fill="x", padx=2, pady=(0, 2))
        self.bar_fg = tk.Frame(self.bar_bg, bg=TYPE_COLORS["Physical"], height=5)
        self.bar_fg.place(relwidth=0.0, relheight=1.0)
        self._packed = False

    def show(self, text: str, color: str, frac: float):
        if not self._packed:
            self.frame.pack(fill="x", pady=(1, 1))
            self._packed = True
        if self.label.cget("text") != text:
            self.label.config(text=text)
        if self.bar_fg.cget("bg") != color:
            self.bar_fg.config(bg=color)
        self.bar_fg.place_configure(relwidth=max(0.01, frac))

    def hide(self):
        if self._packed:
            self.frame.pack_forget()
            self._packed = False


# ---------------------------------------------------------------------------
# Parse-result screenshot rendering
# ---------------------------------------------------------------------------
# Windows TrueType font paths used by the PNG renderer. Picked so the saved
# image mirrors the on-screen meter's typography. Falls back to PIL's default
# bitmap font if any are missing (rendering still works, just plainer).
_FONT_PATHS = {
    ("segoe", False): r"C:\Windows\Fonts\segoeui.ttf",
    ("segoe", True):  r"C:\Windows\Fonts\segoeuib.ttf",
    ("consolas", False): r"C:\Windows\Fonts\consola.ttf",
    ("consolas", True):  r"C:\Windows\Fonts\consolab.ttf",
}


def _font(family: str, size: int, bold: bool = False):
    """Load a TrueType font with a graceful fallback to PIL's default.

    Lazy-imports PIL.ImageFont so the meter still starts if Pillow isn't
    installed — only the screenshot save path needs it.
    """
    from PIL import ImageFont
    path = _FONT_PATHS.get((family, bold))
    if path and Path(path).exists():
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _wrap_text_chars(text: str, max_chars: int) -> list[str]:
    """Naive whitespace-aware word wrap by character count.

    Good enough for the description field on a fixed-width card; not
    typographically perfect for variable-width fonts but renders predictably.
    """
    out: list[str] = []
    for paragraph in text.splitlines():
        if not paragraph.strip():
            out.append("")
            continue
        words = paragraph.split()
        cur = ""
        for w in words:
            if not cur:
                cur = w
            elif len(cur) + 1 + len(w) <= max_chars:
                cur = cur + " " + w
            else:
                out.append(cur)
                cur = w
        if cur:
            out.append(cur)
    return out


def _render_parse_image(snap, name: str, description: str,
                        timestamp_str: str, player_id: int | None):
    """Render a meter-styled PNG summarising a 30s parse. Returns a PIL Image.

    Mirrors the on-screen overlay: dark border, teal header with version + ID,
    cream body with title + total/DPS + per-type bars. Layout heights are
    precomputed so the final image is tight to the content (no whitespace
    padding at the bottom).
    """
    from PIL import Image, ImageDraw

    breakdown = snap.breakdown()
    duration = max(0.001, snap.duration)
    total = snap.total()
    dps = snap.dps()

    width = 420
    border = 2
    header_h = 38
    body_pad = 14

    title_h = 26
    sep_h = 8                       # 1px line + 7px gap

    desc_lines = _wrap_text_chars(description.strip(), 56) if description and description.strip() else []
    desc_h = (16 * len(desc_lines) + 8) if desc_lines else 0

    stats_h = 30
    row_text_h = 17
    row_bar_h = 7                   # 5px bar + 2px gap
    row_gap = 4
    row_h = row_text_h + row_bar_h + row_gap

    rows_block_h = row_h * len(breakdown) if breakdown else 24

    # Timeline section sits between per-type rows and the footer when the
    # parse actually has data. Mirrors the live-overlay layout.
    has_timeline = total > 0 and bool(getattr(snap, "timeline", None))
    timeline_plot_h = 50
    timeline_block_h = (8 + 18 + timeline_plot_h + 8) if has_timeline else 0

    footer_h = 22

    body_h = (body_pad + title_h + sep_h + desc_h + stats_h
              + rows_block_h + timeline_block_h + footer_h + body_pad)
    total_h = border + header_h + body_h + border

    img = Image.new("RGB", (width, total_h), color=BG_BORDER)
    draw = ImageDraw.Draw(img)

    # Body background
    draw.rectangle((border, border + header_h,
                    width - border - 1, total_h - border - 1), fill=BG_BODY)

    # Header bar
    draw.rectangle((border, border,
                    width - border - 1, border + header_h - 1), fill=BG_HEADER)
    f_version = _font("consolas", 12)
    f_header_title = _font("segoe", 14, bold=True)
    f_id = _font("consolas", 12)
    draw.text((border + 10, border + 11), f"v{VERSION}",
              fill=FG_HEADER, font=f_version)
    draw.text((border + 56, border + 9), "Farever Damage Meter",
              fill=FG_HEADER, font=f_header_title)
    if player_id is not None:
        id_short = struct.pack("<Q", player_id)[:4].hex()
        id_text = f"ID: {id_short}"
        bbox = draw.textbbox((0, 0), id_text, font=f_id)
        draw.text((width - border - 10 - (bbox[2] - bbox[0]),
                   border + 11), id_text, fill=FG_HEADER, font=f_id)

    # Body cursor
    x_left = border + body_pad
    x_right = width - border - body_pad
    y = border + header_h + body_pad

    # Title row: parse name (or default) + duration
    f_title = _font("segoe", 14, bold=True)
    f_dim = _font("segoe", 10)
    title_text = name.strip() if name and name.strip() else "30s Parse Result"
    draw.text((x_left, y), title_text, fill=ACCENT, font=f_title)
    secs = int(duration)
    mins, ss = divmod(secs, 60)
    combat_text = f"Time: {mins}:{ss:02d}"
    bbox = draw.textbbox((0, 0), combat_text, font=f_dim)
    draw.text((x_right - (bbox[2] - bbox[0]), y + 6),
              combat_text, fill=FG_DIM, font=f_dim)
    y += title_h

    # Separator
    draw.line((x_left, y, x_right, y), fill=BG_BODY_SOFT, width=1)
    y += sep_h

    # Description (if any)
    if desc_lines:
        f_desc = _font("segoe", 10)
        for line in desc_lines:
            draw.text((x_left, y), line, fill=FG_TEXT, font=f_desc)
            y += 16
        y += 8

    # Stats line
    f_stats = _font("consolas", 14, bold=True)
    if total > 0:
        stats_text = f"Total: {int(total):>6}     DPS: {dps:>7.1f}"
    else:
        stats_text = "No damage recorded"
    draw.text((x_left, y), stats_text, fill=FG_VALUE, font=f_stats)
    y += stats_h

    # Per-type rows
    if breakdown:
        max_total = max((t for _, _, t in breakdown), default=1.0) or 1.0
        f_row = _font("consolas", 11)
        for dt, count, t in breakdown:
            dps_val = t / duration
            text = f"{dt:<10} {int(t):>6}  ({dps_val:>5.1f}/s)  {count} hits"
            draw.text((x_left, y), text, fill=FG_TEXT, font=f_row)
            y += row_text_h
            # Bar background
            draw.rectangle((x_left, y, x_right, y + 5), fill=BG_BAR_TRACK)
            frac = max(0.01, t / max_total)
            fill_x1 = x_left + int((x_right - x_left) * frac)
            color = TYPE_COLORS.get(dt, ACCENT)
            draw.rectangle((x_left, y, fill_x1, y + 5), fill=color)
            y += row_bar_h + row_gap
    else:
        y += 24

    # Timeline section (per-second damage bars)
    if has_timeline:
        draw.line((x_left, y, x_right, y), fill=BG_BODY_SOFT, width=1)
        y += 8
        f_section = _font("segoe", 10, bold=True)
        draw.text((x_left, y), "Damage Timeline",
                  fill=ACCENT, font=f_section)
        y += 18
        num_buckets = max(1, int(math.ceil(max(duration, 1.0))))
        tl_vals = snap.timeline
        max_val = max(tl_vals.values()) if tl_vals else 1.0
        if max_val <= 0:
            max_val = 1.0
        bar_w = (x_right - x_left) / num_buckets
        base_y = y + timeline_plot_h
        for i in range(num_buckets):
            v = tl_vals.get(i, 0.0)
            if v <= 0:
                continue
            bh = int((v / max_val) * timeline_plot_h)
            if bh < 1:
                bh = 1
            x0 = int(x_left + i * bar_w)
            x1 = max(x0 + 1, int(x_left + (i + 1) * bar_w) - 1)
            draw.rectangle((x0, base_y - bh, x1, base_y), fill=ACCENT)
        y += timeline_plot_h + 8

    # Footer: timestamp + credit
    f_foot = _font("segoe", 9)
    footer_text = f"{timestamp_str}   ·   Made by Brudr"
    draw.text((x_left, y), footer_text, fill=FG_DIM, font=f_foot)

    return img


def _sanitize_for_filename(s: str) -> str:
    """Strip filesystem-illegal chars (Windows-strictest set) and truncate."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1F]+', "_", s).strip().strip(".")
    return s[:60]


def _center_on_monitor(anchor_window, w: int, h: int, fallback_window):
    """Return (x, y) centered on the work area of the monitor containing
    `anchor_window`. Falls back to primary-screen center on non-Windows or if
    the Win32 monitor APIs fail. y is biased to the upper third so the popup
    sits comfortably above the centre line rather than under it.
    """
    if sys.platform == "win32":
        try:
            from ctypes import wintypes

            class _RECT(ctypes.Structure):
                _fields_ = [
                    ("left", wintypes.LONG), ("top", wintypes.LONG),
                    ("right", wintypes.LONG), ("bottom", wintypes.LONG),
                ]

            class _MONITORINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.DWORD),
                    ("rcMonitor", _RECT),
                    ("rcWork", _RECT),
                    ("dwFlags", wintypes.DWORD),
                ]

            MONITOR_DEFAULTTONEAREST = 2
            user32 = ctypes.windll.user32
            user32.MonitorFromWindow.restype = ctypes.c_void_p
            user32.GetMonitorInfoW.restype = ctypes.c_int
            user32.GetMonitorInfoW.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(_MONITORINFO)]

            anchor_window.update_idletasks()
            hwnd = anchor_window.winfo_id()
            outer = user32.GetParent(hwnd)
            if outer:
                hwnd = outer

            hmon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
            if not hmon:
                raise RuntimeError("MonitorFromWindow returned NULL")
            mi = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(_MONITORINFO)
            if not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                raise RuntimeError("GetMonitorInfoW failed")

            rc = mi.rcWork  # excludes taskbar
            mon_w = rc.right - rc.left
            mon_h = rc.bottom - rc.top
            x = rc.left + (mon_w - w) // 2
            # Bias to upper-third so the popup doesn't bury itself under the
            # vertical centre on tall monitors.
            y = rc.top + max(0, (mon_h - h) // 3)
            return (x, y)
        except Exception as e:
            print(f"[meter] monitor lookup failed ({e}); "
                  "falling back to primary-screen center.", file=sys.stderr)

    sw = fallback_window.winfo_screenwidth()
    sh = fallback_window.winfo_screenheight()
    return (max(0, (sw - w) // 2), max(0, (sh - h) // 3))


class _ParseResultsPopup:
    """Modeless results window opened when a 30s parse finishes.

    Holds its own snapshot of the parse so subsequent meter activity doesn't
    disturb the captured data. Closing the window dismisses without saving;
    the user can re-parse to get a fresh popup.
    """

    def __init__(self, parent_root: tk.Tk, snap, player_id: int | None):
        self.snap = snap
        self.player_id = player_id
        self.timestamp = datetime.now()
        self.timestamp_str = self.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        self._saved_path: Path | None = None

        self.win = tk.Toplevel(parent_root)
        self.win.title("30s Parse Result")
        self.win.configure(bg=BG_BODY)
        # Briefly topmost so the popup surfaces above the game on freeze, then
        # released so the user can alt-tab freely (modeless behavior).
        self.win.attributes("-topmost", True)
        self.win.after(800, lambda: self._safe_attr("-topmost", False))

        w, h = 440, 380
        # Center on the work area of whichever monitor the meter overlay is
        # on. Previous "anchor below the meter" approach was misreading the
        # parent's geometry on some configs and dumping the popup in the
        # bottom-right of the screen. MonitorFromWindow is the canonical Win32
        # answer to "which display is this window on".
        x, y = _center_on_monitor(parent_root, w, h, self.win)
        self.win.geometry(f"{w}x{h}+{x}+{y}")
        self.win.minsize(420, 360)

        self._build_ui()
        self.win.lift()
        self.win.focus_force()

    def _safe_attr(self, key: str, value):
        try:
            self.win.attributes(key, value)
        except tk.TclError:
            pass

    def _build_ui(self):
        outer = tk.Frame(self.win, bg=BG_BORDER, padx=2, pady=2)
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer, bg=BG_HEADER, height=32)
        header.pack(fill="x")
        tk.Label(header, text="30s Parse Result",
                 bg=BG_HEADER, fg=FG_HEADER,
                 font=("Segoe UI", 11, "bold"),
                 padx=12, pady=6).pack(side="left")
        tk.Label(header, text=self.timestamp_str,
                 bg=BG_HEADER, fg=FG_HEADER,
                 font=("Consolas", 9),
                 padx=12, pady=6).pack(side="right")

        body = tk.Frame(outer, bg=BG_BODY, padx=14, pady=12)
        body.pack(fill="both", expand=True)

        # Summary readout — what's actually in this parse
        total = self.snap.total()
        dps = self.snap.dps()
        n_types = len(self.snap.breakdown())
        summary_text = (f"Total: {int(total)}     "
                        f"DPS: {dps:.1f}     "
                        f"{n_types} damage type{'s' if n_types != 1 else ''}")
        tk.Label(body, text=summary_text, bg=BG_BODY, fg=FG_VALUE,
                 font=("Consolas", 11, "bold"),
                 anchor="w").pack(fill="x", pady=(0, 12))

        tk.Frame(body, bg=BG_BODY_SOFT, height=1).pack(fill="x", pady=(0, 10))

        tk.Label(body, text="Name (optional)", bg=BG_BODY, fg=FG_TEXT,
                 font=("Segoe UI", 9), anchor="w").pack(fill="x")
        self.name_entry = tk.Entry(body, bg="#FFFFFF", fg=FG_VALUE,
                                   font=("Segoe UI", 10),
                                   relief="flat", highlightthickness=1,
                                   highlightbackground=BG_BODY_SOFT,
                                   highlightcolor=ACCENT)
        self.name_entry.pack(fill="x", pady=(2, 10), ipady=4)

        tk.Label(body, text="Description (optional)", bg=BG_BODY, fg=FG_TEXT,
                 font=("Segoe UI", 9), anchor="w").pack(fill="x")
        self.desc_text = tk.Text(body, height=4, bg="#FFFFFF", fg=FG_VALUE,
                                 font=("Segoe UI", 10), wrap="word",
                                 relief="flat", highlightthickness=1,
                                 highlightbackground=BG_BODY_SOFT,
                                 highlightcolor=ACCENT)
        self.desc_text.pack(fill="x", pady=(2, 12))

        btn_row = tk.Frame(body, bg=BG_BODY)
        btn_row.pack(fill="x")
        self.save_btn = tk.Button(
            btn_row, text="Save Screenshot",
            command=self._save,
            bg=BG_HEADER, fg=FG_HEADER,
            activebackground=ACCENT, activeforeground=FG_HEADER,
            font=("Segoe UI", 10, "bold"),
            relief="flat", padx=14, pady=6, cursor="hand2",
        )
        self.save_btn.pack(side="left")
        self.open_btn = tk.Button(
            btn_row, text="Open Folder",
            command=self._open_folder,
            bg=BG_BODY_SOFT, fg=FG_TEXT,
            activebackground=BG_BAR_TRACK, activeforeground=FG_VALUE,
            font=("Segoe UI", 10),
            relief="flat", padx=14, pady=6, cursor="hand2",
            state="disabled",
        )
        self.open_btn.pack(side="left", padx=(8, 0))

        self.status_label = tk.Label(body, text="", bg=BG_BODY, fg=FG_DIM,
                                     font=("Segoe UI", 9),
                                     wraplength=400, justify="left",
                                     anchor="w")
        self.status_label.pack(fill="x", pady=(10, 0))

    def _save(self):
        name = self.name_entry.get().strip()
        description = self.desc_text.get("1.0", "end").strip()
        try:
            img = _render_parse_image(
                self.snap, name, description,
                self.timestamp_str, self.player_id)
        except ImportError:
            self.status_label.config(
                text="Pillow not installed — run:  pip install Pillow",
                fg="#B0392E")
            return
        except Exception as e:
            self.status_label.config(
                text=f"Render failed: {e}", fg="#B0392E")
            return

        try:
            PARSE_SCREENSHOTS_DIR.mkdir(exist_ok=True)
            ts_safe = self.timestamp.strftime("%Y-%m-%d_%H-%M-%S")
            if name:
                name_safe = _sanitize_for_filename(name)
                filename = f"{ts_safe}_{name_safe}.png" if name_safe else f"{ts_safe}.png"
            else:
                filename = f"{ts_safe}.png"
            path = PARSE_SCREENSHOTS_DIR / filename
            img.save(path, "PNG")
            self._saved_path = path
            self.status_label.config(
                text=f"Saved to parse_screenshots/{filename}", fg=ACCENT)
            self.open_btn.config(state="normal")
        except OSError as e:
            self.status_label.config(
                text=f"Save failed: {e}", fg="#B0392E")

    def _open_folder(self):
        if self._saved_path is None:
            return
        try:
            if sys.platform == "win32":
                os.startfile(str(self._saved_path.parent))
            else:
                self.status_label.config(
                    text=f"Folder: {self._saved_path.parent}", fg=FG_DIM)
        except OSError as e:
            self.status_label.config(text=f"Open failed: {e}", fg="#B0392E")


class SimpleOverlay:
    def __init__(self, session: DamageSession, backend: MeterBackend,
                 target_pid: int | None = None):
        self.session = session
        self.backend = backend
        self.view_mode = "current"
        # Used by the focus-conditional hotkey hook to filter keystrokes —
        # None falls back to global RegisterHotKey (works but eats keystrokes
        # in our popup window).
        self._target_pid = target_pid

        self.root = tk.Tk()
        self.root.title("Farever Damage Meter")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.94)
        # Use the transparency color-key as the root background. Anything
        # outside the meter's bordered box (i.e. the hint label area) inherits
        # this color and reads as fully transparent on Windows.
        self.root.configure(bg=TRANSPARENT_KEY)
        try:
            self.root.wm_attributes("-transparentcolor", TRANSPARENT_KEY)
        except tk.TclError:
            # Non-Windows tk builds may not support -transparentcolor.
            pass

        self._rows: list[_Row] = []
        self._visible = True
        self._locked = True
        self._prev_visible_rows = 0   # for shrink-on-rows-hidden detection
        # Flipped to True once the user drags the meter while unlocked, or on
        # startup if a cached position is loaded from disk. After that,
        # shrink/reset events preserve the user's chosen top-left corner
        # instead of snapping back to the default top-right pin.
        self._user_positioned = False
        self._saved_x: int | None = None
        self._saved_y: int | None = None
        cached = _load_position()
        if cached is not None:
            self._saved_x, self._saved_y = cached
            self._user_positioned = True
        # Hotkey thread pushes action names ("toggle" / "reset" / "lock") into
        # this queue. The refresh tick drains it on the tk main thread so we
        # never touch tk objects from the hotkey thread (which is unsafe with
        # the custom pump loop this overlay uses).
        self._hotkey_actions: "queue.Queue[str]" = queue.Queue()
        # Parse-mode popup state. Armed when parse mode toggles on or hard
        # reset happens; consumed on the rising edge of parse_frozen so each
        # 30s window gets exactly one popup.
        self._popup_armed = False
        self._popup_last_frozen = False
        self._build_ui()

        self.root.update_idletasks()
        self._reposition()
        self.root.after(100, self._reposition)
        self.root.after(500, self._reposition)
        self.root.after(50, self._apply_clickthrough)
        self._schedule_refresh()

        _start_global_hotkeys({
            HOTKEY_TOGGLE:    lambda: self._hotkey_actions.put("toggle"),
            HOTKEY_RESET:     lambda: self._hotkey_actions.put("reset"),
            HOTKEY_LOCK:      lambda: self._hotkey_actions.put("lock"),
            HOTKEY_RESET_POS: lambda: self._hotkey_actions.put("reset_pos"),
            HOTKEY_PARSE:     lambda: self._hotkey_actions.put("parse"),
        }, target_pid=self._target_pid)

    def _drain_hotkey_actions(self):
        while True:
            try:
                action = self._hotkey_actions.get_nowait()
            except queue.Empty:
                return
            if action == "toggle":
                self._toggle_visibility()
            elif action == "reset":
                self._reset_session()
            elif action == "lock":
                self._toggle_lock()
            elif action == "reset_pos":
                self._reset_position()
            elif action == "parse":
                self._toggle_parse_mode()

    def _toggle_visibility(self):
        if self._visible:
            self.root.withdraw()
            self._visible = False
        else:
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            # Click-through ex-style is preserved across withdraw/deiconify on
            # Windows, but re-applying is cheap insurance.
            self.root.after(50, self._apply_clickthrough)
            self._visible = True

    def _reset_session(self):
        self.session.reset_all()
        # Also wipe player-ID detection so a wrong lock-on can be corrected by
        # pressing reset. The TX consumption loop re-identifies the player
        # within a few seconds of resumed combat.
        if self.backend is not None:
            self.backend.reset_player_detection()
        # Hard reset re-arms the parse popup so the next freeze opens a fresh
        # one. Also clear the edge-detection flag so the rising edge fires.
        self._popup_armed = True
        self._popup_last_frozen = False
        # Force the next refresh to redraw the "awaiting" placeholder
        self._stats_label.config(text="")
        # Auto-detection in _refresh handles the visual shrink as soon as the
        # rows transition from visible to hidden. Schedule explicit shrinks at
        # a couple of delays too, in case the refresh tick takes longer than
        # usual (e.g. under heavy packet load right at reset time).
        self.root.after(REFRESH_MS + 50, self._shrink_to_natural)
        self.root.after(REFRESH_MS * 2 + 100, self._shrink_to_natural)

    def _toggle_parse_mode(self):
        """Flip 30s parse mode on/off and clear the meter for a fresh window.

        Toggling EITHER direction wipes session data + player detection so the
        user always starts from a known-clean state. Inside parse mode, the
        Shift+\\ hard reset re-arms the 30s timer without disturbing the
        parse_mode flag itself.
        """
        enabled_now, _, _ = self.session.parse_state()
        going_on = not enabled_now
        self.session.set_parse_mode(going_on)
        self.session.reset_all()
        if self.backend is not None:
            self.backend.reset_player_detection()
        # Arm the popup only when turning parse mode ON. Turning it off should
        # not queue a popup that the user wouldn't expect to see.
        self._popup_armed = going_on
        self._popup_last_frozen = False
        self._stats_label.config(text="")
        self.root.after(REFRESH_MS + 50, self._shrink_to_natural)
        self.root.after(REFRESH_MS * 2 + 100, self._shrink_to_natural)

    def _open_parse_popup(self):
        """Open a results popup for the just-frozen parse. Snapshots the data
        so the popup is unaffected by subsequent meter activity."""
        snap = self.session.snapshot("current")
        pid = getattr(self.backend, "player_id", None)
        try:
            _ParseResultsPopup(self.root, snap, pid)
        except tk.TclError as e:
            print(f"[meter] failed to open parse popup: {e}", file=sys.stderr)

    def _shrink_to_natural(self):
        # Capture the current top-left BEFORE wm_geometry("") clears the
        # explicit size — once tk recomputes the natural geometry the window
        # can momentarily snap to a default position, and reading winfo_x/y
        # after that would lose the user's chosen location.
        cur_x = self.root.winfo_x()
        cur_y = self.root.winfo_y()
        # wm_geometry("") tells tk to drop any user-set explicit size, so the
        # next read of winfo_reqheight reflects the *current* layout (post
        # pack_forget) instead of the previous explicit size we set during
        # combat. Two update_idletasks passes catch layout that needs a second
        # round to settle under overrideredirect.
        try:
            self.root.wm_geometry("")
        except tk.TclError:
            pass
        self.root.update_idletasks()
        self.root.update_idletasks()
        w = max(self.root.winfo_reqwidth(), 360)
        h = self.root.winfo_reqheight()
        if self._user_positioned:
            # Prefer the explicitly-saved coordinates if we have them — they
            # survive the wm_geometry("") reset above without depending on
            # whatever transient position the window manager picked.
            if self._saved_x is not None and self._saved_y is not None:
                x, y = self._saved_x, self._saved_y
            else:
                x, y = cur_x, cur_y
        else:
            screen_w = self.root.winfo_screenwidth()
            x = max(0, screen_w - w - MARGIN_FROM_RIGHT)
            y = MARGIN_FROM_TOP
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    # ---- Lock / drag handling ----
    def _toggle_lock(self):
        self._locked = not self._locked
        if sys.platform == "win32":
            hwnd = self.root.winfo_id()
            parent = ctypes.windll.user32.GetParent(hwnd)
            target = parent if parent else hwnd
            try:
                _set_window_clickthrough(target, self._locked)
            except Exception as e:
                print(f"[!] lock toggle failed: {e}", file=sys.stderr)
        if self._locked:
            self._header.unbind("<Button-1>")
            self._header.unbind("<B1-Motion>")
            self._header.unbind("<ButtonRelease-1>")
            self.title_label.unbind("<Button-1>")
            self.title_label.unbind("<B1-Motion>")
            self.title_label.unbind("<ButtonRelease-1>")
        else:
            self._header.bind("<Button-1>", self._drag_start)
            self._header.bind("<B1-Motion>", self._drag_motion)
            self._header.bind("<ButtonRelease-1>", self._drag_end)
            self.title_label.bind("<Button-1>", self._drag_start)
            self.title_label.bind("<B1-Motion>", self._drag_motion)
            self.title_label.bind("<ButtonRelease-1>", self._drag_end)

    def _drag_start(self, event):
        self._drag_offset_x = event.x_root - self.root.winfo_x()
        self._drag_offset_y = event.y_root - self.root.winfo_y()

    def _drag_motion(self, event):
        x = event.x_root - self._drag_offset_x
        y = event.y_root - self._drag_offset_y
        self.root.geometry(f"+{x}+{y}")
        self._user_positioned = True
        self._saved_x = x
        self._saved_y = y

    def _drag_end(self, event):
        # Persist on release rather than on every motion event — drag-motion
        # fires dozens of times per second and we don't need to spam the disk.
        if self._saved_x is not None and self._saved_y is not None:
            _save_position(self._saved_x, self._saved_y)

    def _reset_position(self):
        """Forget the user's chosen position and re-pin to the default top-right."""
        self._user_positioned = False
        self._saved_x = None
        self._saved_y = None
        _clear_position_cache()
        self._shrink_to_natural()

    def _build_ui(self):
        # Floating keybind hint, sitting outside the bordered meter box.
        # The label's bg matches the root window's bg so it reads as a caption
        # rather than a second panel. Packed first so it sits above the meter.
        self._hint_label = tk.Label(
            self.root,
            text=(
                "\\ show/hide   Shift+\\ reset   Ctrl+\\ 30s parse\n"
                "/ lock/unlock   Shift+/ reset pos"
            ),
            bg=TRANSPARENT_KEY, fg="#FFFFFF",
            font=("Segoe UI", 8, "bold"), pady=4, justify="center",
        )
        self._hint_label.pack(fill="x")

        outer = tk.Frame(self.root, bg=BG_BORDER, padx=2, pady=2)
        outer.pack(fill="x")

        self._header = tk.Frame(outer, bg=BG_HEADER, height=28)
        self._header.pack(fill="x")
        self.version_label = tk.Label(
            self._header, text=f"v{VERSION}",
            bg=BG_HEADER, fg=FG_HEADER,
            font=("Consolas", 9), padx=10, pady=4)
        self.version_label.pack(side="left")
        self.title_label = tk.Label(
            self._header, text="Farever Damage Meter",
            bg=BG_HEADER, fg=FG_HEADER,
            font=("Segoe UI", 10, "bold"), padx=10, pady=4)
        self.title_label.pack(side="left")
        # Right-side header slot now shows the detected player entity ID
        # (formatted as the first 4 bytes of the u64, little-endian hex).
        # Reads "ID: —" until the TX-window detector locks on.
        self.view_label = tk.Label(
            self._header, text="ID: —",
            bg=BG_HEADER, fg=FG_HEADER,
            font=("Consolas", 9), padx=10, pady=4)
        self.view_label.pack(side="right")
        self._header_combat_bg = False  # last applied state, for cheap dedupe

        body = tk.Frame(outer, bg=BG_BODY, padx=10, pady=8)
        body.pack(fill="both", expand=True)

        title_row = tk.Frame(body, bg=BG_BODY)
        title_row.pack(fill="x", pady=(0, 2))
        self._section_title_label = tk.Label(
            title_row, text="NO DATA", bg=BG_BODY, fg=ACCENT,
            font=("Segoe UI", 11, "bold"), anchor="w")
        self._section_title_label.pack(side="left")
        self._combat_time_label = tk.Label(
            title_row, text="", bg=BG_BODY, fg=FG_DIM,
            font=("Segoe UI", 9), anchor="e")
        self._combat_time_label.pack(side="right")

        tk.Frame(body, bg=BG_BODY_SOFT, height=1).pack(fill="x", pady=(0, 6))

        self._stats_label = tk.Label(
            body, text="", bg=BG_BODY, fg=FG_VALUE,
            font=("Consolas", 11, "bold"), anchor="w")
        self._stats_label.pack(fill="x", pady=(0, 6))

        # Disclaimer / attribution — only shown on the awaiting-combat screen.
        # Visibility is toggled in _refresh based on whether any damage has
        # been recorded in the current view.
        self._intro_label = tk.Label(
            body,
            text=(
                "Made by Brudr — uses Frida to capture combat events from "
                "the game's network traffic. For research purposes only; "
                "does not access Farever's game memory."
            ),
            bg=BG_BODY, fg=FG_DIM,
            font=("Segoe UI", 8), justify="left", anchor="w",
            wraplength=340,
        )
        self._intro_label.pack(fill="x", pady=(0, 4))
        self._intro_visible = True

        rows_container = tk.Frame(body, bg=BG_BODY)
        rows_container.pack(fill="x")
        for _ in range(MAX_ROWS):
            self._rows.append(_Row(rows_container))

        # NOTE: The Damage Timeline graph is intentionally NOT rendered in the
        # live overlay — it's only included in the saved screenshot PNG (see
        # _render_parse_image). Keeps the on-screen meter compact and avoids
        # the constant 4-Hz canvas redraw while in combat.

        self.root.minsize(360, 0)

    def _reposition(self):
        self.root.update_idletasks()
        if self._user_positioned and self._saved_x is not None:
            self.root.geometry(f"+{self._saved_x}+{self._saved_y}")
            return
        screen_w = self.root.winfo_screenwidth()
        win_w = max(self.root.winfo_width(), self.root.winfo_reqwidth(), 360)
        x = max(0, screen_w - win_w - MARGIN_FROM_RIGHT)
        self.root.geometry(f"+{x}+{MARGIN_FROM_TOP}")

    def _apply_clickthrough(self):
        if sys.platform != "win32":
            return
        hwnd = self.root.winfo_id()
        parent = ctypes.windll.user32.GetParent(hwnd)
        target = parent if parent else hwnd
        try:
            _make_window_clickthrough(target)
        except Exception as e:
            print(f"[!] click-through setup failed: {e}", file=sys.stderr)

    def _maintain_visibility(self):
        """Re-assert HWND_TOPMOST whenever someone other than our own popup
        owns the foreground window.

        Windows can quietly drop WS_EX_NOACTIVATE topmost overlays behind the
        foreground app on focus changes — most commonly when alt-tabbing back
        into a fullscreen-borderless game. SetWindowPos with HWND_TOPMOST +
        SWP_NOACTIVATE is idempotent (no flicker if we're already on top) and
        cheap enough to run every refresh tick. We skip the call when our own
        popup is foregrounded so we don't fight it for z-order.
        """
        if sys.platform != "win32":
            return
        if not self._visible:
            return  # user explicitly hid the meter via `\` — respect that
        try:
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            fg = user32.GetForegroundWindow()
            if fg:
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
                # Our own popup window lives in this Python process. Don't
                # shove the meter on top of it.
                if pid.value == os.getpid():
                    return
            hwnd = self.root.winfo_id()
            outer = user32.GetParent(hwnd)
            target = outer if outer else hwnd
            HWND_TOPMOST = -1
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOACTIVATE = 0x0010
            SWP_SHOWWINDOW = 0x0040
            user32.SetWindowPos(
                target, HWND_TOPMOST, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
        except Exception:
            # Visibility maintenance is best-effort; any error here would
            # spam stderr every 250ms. Swallow silently.
            pass

    def _schedule_refresh(self):
        self._refresh()
        self.root.after(REFRESH_MS, self._schedule_refresh)

    def _refresh(self):
        self._drain_hotkey_actions()
        self._maintain_visibility()
        self.session.tick()
        snap = self.session.snapshot(self.view_mode)
        in_combat, idle = self.session.status()
        parse_enabled, parse_frozen, parse_elapsed = self.session.parse_state()

        # Rising-edge detection: open the popup the first refresh tick after
        # parse_frozen flips True. Guarded by _popup_armed so toggling parse
        # mode off (or skipping arm on a stale freeze) doesn't open a window.
        if (parse_enabled and parse_frozen and not self._popup_last_frozen
                and self._popup_armed):
            self._popup_armed = False
            self._open_parse_popup()
        self._popup_last_frozen = parse_frozen

        if parse_enabled:
            if parse_frozen:
                title = "Parse Complete (30s)  ·  Shift+\\ to reset"
            elif parse_elapsed > 0:
                # Ceil so the first tick reads "30s" and the final tick reads
                # "1s" rather than briefly flashing "0s".
                remaining = max(
                    0, math.ceil(PARSE_MODE_DURATION_SECS - parse_elapsed))
                title = f"Parse Mode  ·  {remaining}s remaining"
            else:
                title = "Parse Mode  ·  awaiting first hit"
        elif in_combat:
            # Idle counts up from the last damage event; the encounter closes
            # when it crosses combat_timeout. Ceil so the user sees "Ends in
            # 30s" the moment the timer starts and "Ends in 1s" right up to
            # the transition, instead of a confusing "Ends in 0s" hang.
            secs_left = max(0, math.ceil(self.session.combat_timeout - idle))
            title = f"Logging ends in {secs_left}s"
        else:
            title = "Farever Damage Meter"
        if not self._locked:
            title += "  ·  UNLOCKED"
        if self.title_label.cget("text") != title:
            self.title_label.config(text=title)

        if in_combat != self._header_combat_bg:
            bg = BG_HEADER_COMBAT if in_combat else BG_HEADER
            self._header.config(bg=bg)
            self.version_label.config(bg=bg)
            self.title_label.config(bg=bg)
            self.view_label.config(bg=bg)
            self._header_combat_bg = in_combat

        pid = getattr(self.backend, "player_id", None)
        if pid is None:
            id_text = "ID: —"
        else:
            short = struct.pack("<Q", pid)[:4].hex()
            id_text = f"ID: {short}"
        if self.view_label.cget("text") != id_text:
            self.view_label.config(text=id_text)

        if snap.start_time > 0:
            secs = int(snap.duration)
            mins, ss = divmod(secs, 60)
            combat_text = f"Time in Combat: {mins}:{ss:02d}"
        else:
            combat_text = ""
        if self._combat_time_label.cget("text") != combat_text:
            self._combat_time_label.config(text=combat_text)

        total = snap.total()
        rate = snap.dps()
        breakdown = snap.breakdown()

        if total <= 0:
            stats_text = "Awaiting combat data…"
        else:
            stats_text = f"Total: {int(total):>6}     DPS: {rate:>7.1f}"
        if self._stats_label.cget("text") != stats_text:
            self._stats_label.config(text=stats_text)

        if parse_enabled and parse_frozen:
            section_title = "Parse Results (30s)"
        elif parse_enabled and total > 0:
            section_title = "Parse Logging…"
        elif parse_enabled:
            section_title = "PARSE READY"
        elif in_combat:
            section_title = "Logging Damage"
        elif total > 0:
            section_title = "Logged Damage"
        else:
            section_title = "NO DATA"
        if self._section_title_label.cget("text") != section_title:
            self._section_title_label.config(text=section_title)

        # Show the attribution/disclaimer only on the awaiting screen.
        show_intro = total <= 0
        if show_intro and not self._intro_visible:
            self._intro_label.pack(fill="x", pady=(0, 4),
                                   before=self._rows[0].frame.master)
            self._intro_visible = True
        elif not show_intro and self._intro_visible:
            self._intro_label.pack_forget()
            self._intro_visible = False

        max_total = max((t for _, _, t in breakdown), default=1.0) or 1.0
        duration = max(0.001, snap.duration)
        visible_now = 0
        for i, row in enumerate(self._rows):
            if total > 0 and i < len(breakdown):
                dt, count, total_v = breakdown[i]
                dps_val = total_v / duration
                text = f"{dt:<10} {int(total_v):>6}  ({dps_val:>5.1f}/s)  {count} hits"
                color = TYPE_COLORS.get(dt, ACCENT)
                frac = total_v / max_total if max_total > 0 else 0
                row.show(text, color, frac)
                visible_now += 1
            else:
                row.hide()

        # If per-type rows just transitioned to fewer/zero, schedule a shrink.
        # after_idle runs after the current event handler settles layout, so
        # winfo_reqheight will reflect the freshly-hidden rows.
        if visible_now < self._prev_visible_rows:
            self.root.after_idle(self._shrink_to_natural)
        self._prev_visible_rows = visible_now

    def run(self):
        try:
            while True:
                try:
                    self.root.update_idletasks()
                    self.root.update()
                except tk.TclError:
                    return
                time.sleep(0.02)
        except KeyboardInterrupt:
            try:
                self.root.destroy()
            except tk.TclError:
                pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    session = DamageSession()
    backend = MeterBackend(session)

    print(f"[*] Attaching to {TARGET_PROCESS}...", file=sys.stderr)
    try:
        frida_session = frida.attach(TARGET_PROCESS)
    except frida.ProcessNotFoundError:
        sys.exit(f"[!] {TARGET_PROCESS} not running. Launch the game first.")

    # Resolve Farever's PID so the keyboard hook can filter keystrokes to only
    # fire when the game has focus. If lookup fails, fall back to global
    # RegisterHotKey hotkeys.
    farever_pid: int | None = None
    try:
        farever_pid = frida.get_local_device().get_process(TARGET_PROCESS).pid
    except Exception as e:
        print(f"[meter] PID lookup failed for focus-conditional hotkeys "
              f"({e}); hotkeys will fire globally.", file=sys.stderr)

    def on_message(message, data):
        if message["type"] != "send":
            if message["type"] == "error":
                print(f"[!] script error: {message.get('description')}",
                      file=sys.stderr)
            return
        p = message["payload"]
        if not data:
            return
        try:
            if p["dir"] == "tx":
                backend.consume_tx(data)
            else:
                backend.consume_rx(data)
        except Exception as e:
            print(f"[!] error in handler: {e}", file=sys.stderr)

    script = frida_session.create_script(HOOK_SCRIPT_PATH.read_text(encoding="utf-8"))
    script.on("message", on_message)
    script.load()
    print("[*] Hook loaded. Starting overlay...", file=sys.stderr)
    print("[*] Hotkeys:  \\ show/hide   Shift+\\ reset   Ctrl+\\ 30s parse   "
          "/ lock/unlock   Shift+/ reset pos", file=sys.stderr)

    overlay = SimpleOverlay(session, backend, target_pid=farever_pid)

    try:
        overlay.run()
    finally:
        try:
            script.unload()
            frida_session.detach()
        except Exception:
            pass

    print("[*] Stopped.", file=sys.stderr)


if __name__ == "__main__":
    main()
