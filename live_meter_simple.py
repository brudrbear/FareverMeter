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
import os
import queue
import struct
import sys
import threading
import time
import tkinter as tk
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

import frida

from event_parser import scan_events, get_entity_id_from_tx, COMBAT_EVENT_NAMES

TARGET_PROCESS = "Farever.exe"
HOOK_SCRIPT_PATH = Path(__file__).with_name("hook_ssl.js")

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
# keypresses. RegisterHotKey installs system-wide hotkeys that fire regardless
# of which window has focus — exactly what we want for an overlay.
WM_HOTKEY = 0x0312
MOD_SHIFT = 0x0004
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
VK_OEM_5 = 0xDC  # the '\|' key on a US keyboard layout
VK_OEM_2 = 0xBF  # the '/?' key on a US keyboard layout

HOTKEY_TOGGLE = 1
HOTKEY_RESET = 2
HOTKEY_LOCK = 3


def _start_global_hotkeys(callbacks: dict[int, callable]) -> None:
    """Spawn a daemon thread that registers global hotkeys and pumps WM_HOTKEY.

    `callbacks` maps a HOTKEY_* id to a no-arg function (called on the hotkey
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


# ---------------------------------------------------------------------------
# Damage-only session tracker
# ---------------------------------------------------------------------------
@dataclass
class DamageAggregate:
    damage: dict[str, list[float]] = field(
        default_factory=lambda: defaultdict(lambda: [0, 0.0]))
    start_time: float = 0.0
    end_time: float = 0.0
    duration: float = 0.0

    def reset(self, now: float):
        self.damage.clear()
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

    def record_damage(self, dmg_type: str, value: float):
        with self.lock:
            now = time.time()
            if not self.in_combat and self.last_event_time > 0:
                self.current.reset(now)
            self.in_combat = True
            for agg in (self.current, self.session):
                agg.damage[dmg_type][0] += 1
                agg.damage[dmg_type][1] += value
            self.last_event_time = now
            self.current.end_time = now
            if self.current.start_time == 0:
                self.current.start_time = now
            self.current.duration = max(0.001, now - self.current.start_time)
            self.session.end_time = now
            self.session.duration = now - self.session.start_time

    def tick(self):
        with self.lock:
            now = time.time()
            if self.in_combat and (now - self.last_event_time) > self.combat_timeout:
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

    def snapshot(self, view: str = "current") -> DamageAggregate:
        with self.lock:
            src = self.current if view == "current" else self.session
            snap = DamageAggregate()
            for k, v in src.damage.items():
                snap.damage[k] = list(v)
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


class SimpleOverlay:
    def __init__(self, session: DamageSession, backend: MeterBackend):
        self.session = session
        self.backend = backend
        self.view_mode = "current"

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
        # Hotkey thread pushes action names ("toggle" / "reset" / "lock") into
        # this queue. The refresh tick drains it on the tk main thread so we
        # never touch tk objects from the hotkey thread (which is unsafe with
        # the custom pump loop this overlay uses).
        self._hotkey_actions: "queue.Queue[str]" = queue.Queue()
        self._build_ui()

        self.root.update_idletasks()
        self._reposition()
        self.root.after(100, self._reposition)
        self.root.after(500, self._reposition)
        self.root.after(50, self._apply_clickthrough)
        self._schedule_refresh()

        _start_global_hotkeys({
            HOTKEY_TOGGLE: lambda: self._hotkey_actions.put("toggle"),
            HOTKEY_RESET:  lambda: self._hotkey_actions.put("reset"),
            HOTKEY_LOCK:   lambda: self._hotkey_actions.put("lock"),
        })

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
        # Force the next refresh to redraw the "awaiting" placeholder
        self._stats_label.config(text="")
        # Auto-detection in _refresh handles the visual shrink as soon as the
        # rows transition from visible to hidden. Schedule explicit shrinks at
        # a couple of delays too, in case the refresh tick takes longer than
        # usual (e.g. under heavy packet load right at reset time).
        self.root.after(REFRESH_MS + 50, self._shrink_to_natural)
        self.root.after(REFRESH_MS * 2 + 100, self._shrink_to_natural)

    def _shrink_to_natural(self):
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
        screen_w = self.root.winfo_screenwidth()
        x = max(0, screen_w - w - MARGIN_FROM_RIGHT)
        self.root.geometry(f"{w}x{h}+{x}+{MARGIN_FROM_TOP}")

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
            self.title_label.unbind("<Button-1>")
            self.title_label.unbind("<B1-Motion>")
        else:
            self._header.bind("<Button-1>", self._drag_start)
            self._header.bind("<B1-Motion>", self._drag_motion)
            self.title_label.bind("<Button-1>", self._drag_start)
            self.title_label.bind("<B1-Motion>", self._drag_motion)

    def _drag_start(self, event):
        self._drag_offset_x = event.x_root - self.root.winfo_x()
        self._drag_offset_y = event.y_root - self.root.winfo_y()

    def _drag_motion(self, event):
        x = event.x_root - self._drag_offset_x
        y = event.y_root - self._drag_offset_y
        self.root.geometry(f"+{x}+{y}")

    def _build_ui(self):
        # Floating keybind hint, sitting outside the bordered meter box.
        # The label's bg matches the root window's bg so it reads as a caption
        # rather than a second panel. Packed first so it sits above the meter.
        self._hint_label = tk.Label(
            self.root,
            text="\\ show/hide    Shift+\\ reset    / lock/unlock",
            bg=TRANSPARENT_KEY, fg="#FFFFFF",
            font=("Segoe UI", 8, "bold"), pady=4,
        )
        self._hint_label.pack(fill="x")

        outer = tk.Frame(self.root, bg=BG_BORDER, padx=2, pady=2)
        outer.pack(fill="x")

        self._header = tk.Frame(outer, bg=BG_HEADER, height=28)
        self._header.pack(fill="x")
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
        tk.Label(title_row, text="DAMAGE", bg=BG_BODY, fg=ACCENT,
                 font=("Segoe UI", 11, "bold"), anchor="w").pack(side="left")
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

        self.root.minsize(360, 0)

    def _reposition(self):
        self.root.update_idletasks()
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

    def _schedule_refresh(self):
        self._refresh()
        self.root.after(REFRESH_MS, self._schedule_refresh)

    def _refresh(self):
        self._drain_hotkey_actions()
        self.session.tick()
        snap = self.session.snapshot(self.view_mode)
        in_combat, _ = self.session.status()

        title = "Farever Damage Meter"
        if in_combat:
            title += "  ·  IN COMBAT"
        if not self._locked:
            title += "  ·  UNLOCKED"
        if self.title_label.cget("text") != title:
            self.title_label.config(text=title)

        if in_combat != self._header_combat_bg:
            bg = BG_HEADER_COMBAT if in_combat else BG_HEADER
            self._header.config(bg=bg)
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
    print("[*] Hotkeys:  \\ show/hide   Shift+\\ reset   / lock/unlock",
          file=sys.stderr)

    overlay = SimpleOverlay(session, backend)

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
