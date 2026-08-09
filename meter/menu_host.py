"""The settings panel, as a WebView2 window in its own process.

WHY A SECOND PROCESS
--------------------
pywebview refuses to run anywhere but the main thread (it raises outright, see
webview/__init__.py), and the meter's main thread is already Tk's mainloop.
Two GUI toolkits cannot both own it, so the panel moved out of the process
instead of out of the toolkit. Three things fell out of that for free:

  * a crash or a hang in the panel cannot take the frida hook down with it,
    which matters more here than it usually would — unloading a wedged hook is
    how this game gets crashed,
  * the panel can be DPI-aware on its own terms, and
  * the panel can be restarted while the overlay keeps running.

WHY PIPES AND NOT A SOCKET
--------------------------
The obvious IPC for this is a loopback socket, and it is the wrong choice: a
process that opens a listening socket can make Windows Firewall put a dialog in
front of somebody who is mid-raid. stdin/stdout carry the same JSON and ask
nobody's permission.

THE PROTOCOL
------------
One JSON object per line, in both directions.

  parent -> here   {"t": "state",  "d": {...}}      the whole panel state
                   {"t": "show"} / {"t": "hide"}
                   {"t": "ret",    "id": N, "r": ...}   reply to a call
                   {"t": "quit"}

  here -> parent   {"t": "ready"}
                   {"t": "call",   "id": N, "m": "...", "p": {...}}
                   {"t": "geom",   "x": .., "y": .., "w": .., "h": ..}
                   {"t": "typing", "on": true}
                   {"t": "closed"}

This module is deliberately dumb. It owns the window and the pipe and nothing
else: every label is computed by the meter and every button calls back into it.
The panel's behaviour lives in web/menu.js, its business logic in the meter.
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

# ---------------------------------------------------------------------------
# Everything here has to happen before `import webview`
# ---------------------------------------------------------------------------
# Same reasoning as the meter's own declaration, and needed independently
# because awareness is per-process: without it Windows bitmap-stretches this
# window by the system scale and the panel is huge and blurry at 300%.
if sys.platform == "win32":
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            pass

# ...and this is the answer to "some people have 300% scale and the meters are
# huge". Being DPI-aware stops WINDOWS scaling us, but Chromium does its own:
# it reads the monitor scale and makes one CSS pixel three device pixels, so
# the panel would come out exactly as big as before by a different route.
# Pinning the device scale factor to 1 means one CSS pixel is one real pixel
# whatever the user's display setting says.
#
# Measured: with this at 2, window.devicePixelRatio reports 2 and a 604px
# viewport becomes 302px. It is authoritative, and it is read once when the
# WebView2 environment is created — which is why it is set here and not later.
#
# The user's OWN size preference is deliberately NOT applied here. It is a CSS
# zoom on the document (see setZoom in menu.js) so the slider can move without
# restarting the browser environment.
os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
                      "--force-device-scale-factor=1")

import webview  # noqa: E402  (must follow the environment set-up above)

HERE = Path(__file__).resolve().parent
# The frozen build puts the web assets under res/web; from source they sit
# beside this file. Resolved the same way the meter resolves its own data.
if getattr(sys, "frozen", False):
    WEB_DIR = Path(sys._MEIPASS) / "res" / "web"
else:
    WEB_DIR = HERE / "web"

# Default panel size. Deliberately modest: the panel is now scrollable, so this
# is a comfortable reading size rather than "tall enough for the biggest tab",
# which is what used to run it off the bottom of people's screens.
DEFAULT_W, DEFAULT_H = 760, 620
MIN_W, MIN_H = 520, 320

HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE = 0x0001, 0x0002
SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000


def _log(msg):
    """The parent collects our stderr into its own log, so this is all the
    logging this process needs."""
    print(f"[menu] {msg}", file=sys.stderr, flush=True)


class Pipe:
    """The line-JSON link to the meter.

    Writes are serialised behind a lock because both the js_api thread and the
    window-event threads send on it. Reads run on one thread that never blocks
    on anything but stdin.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._waiters = {}          # call id -> (Event, [result])
        self._next_id = 1
        self.on_message = None      # set by MenuWindow before the reader starts

    def send(self, obj):
        line = json.dumps(obj, separators=(",", ":"))
        with self._lock:
            try:
                sys.stdout.write(line + "\n")
                sys.stdout.flush()
            except (OSError, ValueError):
                # The parent went away. Nothing to recover to — the window is
                # meaningless without it, so let the process end.
                os._exit(0)

    def call(self, method, params=None, timeout=10.0):
        """Ask the meter to do something, and wait for its answer.

        Most callers ignore the result: a toggle's real reply is the state push
        that follows it. The ones that don't (opening a saved dataset, say)
        would otherwise need a second event just to carry a return value.
        """
        with self._lock:
            cid = self._next_id
            self._next_id += 1
        ev = threading.Event()
        box = [None]
        self._waiters[cid] = (ev, box)
        self.send({"t": "call", "id": cid, "m": method, "p": params or {}})
        if not ev.wait(timeout):
            self._waiters.pop(cid, None)
            _log(f"call {method} timed out")
            return None
        self._waiters.pop(cid, None)
        return box[0]

    def _resolve(self, cid, result):
        got = self._waiters.get(cid)
        if got:
            ev, box = got
            box[0] = result
            ev.set()

    def read_forever(self):
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                _log(f"unparseable line: {line[:120]!r}")
                continue
            if msg.get("t") == "ret":
                self._resolve(msg.get("id"), msg.get("r"))
            elif self.on_message:
                try:
                    self.on_message(msg)
                except Exception as e:      # a bad message must not kill the pipe
                    _log(f"handler error on {msg.get('t')!r}: {e!r}")
        # stdin closed: the meter has stopped.
        os._exit(0)


class Api:
    """What the page can call. Everything here runs on a WebView2 thread."""

    def __init__(self, pipe):
        self.pipe = pipe
        self.hwnd = 0

    # -- the panel's own window ------------------------------------------
    def grab(self):
        """Start a drag or resize: hand the page the window's current rect.

        The panel moves itself from JS rather than asking Windows to run its
        own move loop, and that is a deliberate second attempt.

        The first version posted WM_NCLBUTTONDOWN with ReleaseCapture, from a
        js_api thread. Both halves were wrong there — ReleaseCapture only
        affects the CALLING thread's capture, so the WebView2 child kept the
        mouse, and a SendMessage would have parked the bridge inside the window
        thread's modal loop for the length of the drag. The panel could not be
        moved at all.

        Posting WM_SYSCOMMAND instead fixed the deadlock but not the drag: the
        move loop only tracks the mouse for a window Windows considers active,
        and this one is a topmost tool window over a game, which is exactly the
        case where that is not reliable.

        So: JS owns the gesture, with a pointer capture that survives the
        cursor leaving the header, and calls place() as it moves. It gives up
        Windows' edge snapping, which is a fair price for a panel that
        actually moves.
        """
        return _rect(self.hwnd) if self.hwnd else None

    def place(self, x, y, w=None, h=None):
        """Move and optionally resize, in physical pixels.

        SetWindowPos rather than pywebview's move()/resize(): those take
        LOGICAL pixels and re-scale by the monitor DPI, which would put the
        system scaling we went to some trouble to remove straight back.
        """
        if not self.hwnd:
            return
        flags = SWP_NOZORDER | SWP_NOACTIVATE
        if w is None or h is None:
            w = h = 0
            flags |= SWP_NOSIZE
        ctypes.windll.user32.SetWindowPos(
            self.hwnd, 0, int(x), int(y),
            max(int(w), MIN_W) if w else 0, max(int(h), MIN_H) if h else 0,
            flags)

    def settle(self):
        """The gesture finished — report where the panel ended up.

        Once per drag, not once per step. pywebview's own moved/resized events
        cannot be relied on here because the window is being moved by our own
        SetWindowPos rather than by Windows' move loop, and sixty reports a
        second down the pipe would be the wrong fix anyway.
        """
        if not self.hwnd:
            return
        got = _rect(self.hwnd)
        if got:
            self.pipe.send({"t": "geom", "x": got[0], "y": got[1],
                            "w": got[2], "h": got[3]})

    # -- what the page tells the meter -----------------------------------
    def call(self, method, params=None):
        return self.pipe.call(method, params)

    def notify(self, method, params=None):
        """Fire and forget: a toggle whose only answer is the next state push.
        Saves the page a round trip it would only throw away."""
        self.pipe.send({"t": "call", "id": 0, "m": method, "p": params or {}})

    def typing(self, on):
        """A search box gained or lost the caret.

        The meter needs to know: while this window holds the keyboard the game
        cannot see its own Escape, so the meter has to hand focus back before
        Escape will close the game's menu. Same handshake the Tk panel's
        _set_typing did, over a pipe instead of a binding.
        """
        self.pipe.send({"t": "typing", "on": bool(on)})


class MenuWindow:
    def __init__(self, pipe, geom):
        self.pipe = pipe
        self.api = Api(pipe)
        self._shown = False
        self._closing = False
        self._want = geom           # re-applied in attach(); see _fix_size
        self._last_geom = None      # dedupes the move/resize event pair
        self.window = webview.create_window(
            "Farever+ Controls",
            html=_document(),
            width=int(geom.get("w") or DEFAULT_W),
            height=int(geom.get("h") or DEFAULT_H),
            x=geom.get("x"), y=geom.get("y"),
            min_size=(MIN_W, MIN_H),
            frameless=True,
            easy_drag=False,        # the header does it, natively — see Api.drag
            resizable=True,
            on_top=True,
            hidden=True,            # the meter decides when, following the game
            background_color="#1B1B22",
            js_api=self.api,
        )
        self.window.events.moved += self._on_geom
        self.window.events.resized += self._on_geom
        self.window.events.closing += self._on_closing
        pipe.on_message = self._on_message

    # -- window plumbing --------------------------------------------------
    def attach(self):
        """Take the panel out of the taskbar, and set its real size.

        webview.start(func) starts this thread BEFORE it creates the window
        (see its start(): thread.start() precedes guilib.create_window), so the
        native Form does not exist yet and there is nothing here to attach to.
        Waiting for it beats hooking an event: `shown` never fires for a window
        that starts hidden, which this one does.
        """
        for _ in range(400):                       # ~10s, then give up quietly
            if getattr(self.window, "native", None) is not None:
                break
            time.sleep(0.025)
        self.api.hwnd = _own_hwnd(self.window)
        if not self.api.hwnd:
            _log("no window handle — drag, resize and sizing are inert")
        if self.api.hwnd:
            u = ctypes.windll.user32
            ex = u.GetWindowLongW(self.api.hwnd, GWL_EXSTYLE)
            u.SetWindowLongW(self.api.hwnd, GWL_EXSTYLE, ex | WS_EX_TOOLWINDOW)
            self._fix_size()
        self.pipe.send({"t": "ready"})

    def _fix_size(self):
        """Set the real size ourselves, because pywebview's is short by the
        frame it then removes.

        Measured: asking for 780x640 produced a 764x601 window — exactly one
        Win11 frame less (16 wide, 39 tall). pywebview sizes the WinForms Form
        while it still has a border and only then sets FormBorderStyle.None, so
        the client area becomes the whole window and the chrome's dimensions
        are simply lost.

        On its own that is a cosmetic 16x39. It is not, because we persist the
        size we ended up with and pass it back on the next launch — so the
        shortfall applies to the shortfall, and the panel shrinks a little
        every time the meter starts. Asking Windows directly skips the whole
        problem.
        """
        w = int(self._want.get("w") or DEFAULT_W)
        h = int(self._want.get("h") or DEFAULT_H)
        x, y = self._want.get("x"), self._want.get("y")
        SWP_NOZORDER, SWP_NOACTIVATE, SWP_NOMOVE = 0x0004, 0x0010, 0x0002
        flags = SWP_NOZORDER | SWP_NOACTIVATE
        if x is None or y is None:
            x = y = 0
            flags |= SWP_NOMOVE
        ctypes.windll.user32.SetWindowPos(
            self.api.hwnd, 0, int(x), int(y), max(w, MIN_W), max(h, MIN_H),
            flags)

    def _on_geom(self, *_a):
        """Position and size, whenever either changes.

        The meter persists them with every other window's, so the panel comes
        back where and how the user left it — which is the whole point of
        making it resizable.
        """
        if not self.api.hwnd:
            return
        got = _rect(self.api.hwnd)
        if not got or got == self._last_geom:
            return          # a move and a resize both fire; report each once
        self._last_geom = got
        x, y, w, h = got
        self.pipe.send({"t": "geom", "x": x, "y": y, "w": w, "h": h})

    def _on_closing(self):
        # The panel has no close button of its own, but alt-F4 reaches it.
        # Treat that as "hide", not "quit": the meter is still running and the
        # panel has to come back the next time the game's menu opens.
        if self._closing:
            return True
        self.hide()
        self.pipe.send({"t": "closed"})
        return False

    def show(self):
        if not self._shown:
            self.window.show()
            self._shown = True
        # Re-asserted on EVERY show, not only the first, and not left to
        # pywebview's on_top. A topmost window loses that standing whenever
        # something else claims it — which over a game happens constantly — and
        # the symptom is a panel that stops appearing on Escape while still
        # being, as far as this process knows, shown.
        #
        # NOACTIVATE so re-raising it never pulls focus off the game by itself;
        # the user clicking it is what should do that.
        if self.api.hwnd:
            ctypes.windll.user32.SetWindowPos(
                self.api.hwnd, ctypes.c_void_p(HWND_TOPMOST), 0, 0, 0, 0,
                SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)

    def hide(self):
        if self._shown:
            # Where it was when it went away, in case the last gesture's
            # settle() never made it — closing the escape menu mid-drag, say.
            self.api.settle()
            self.window.hide()
            self._shown = False

    # -- messages from the meter -----------------------------------------
    def _on_message(self, msg):
        t = msg.get("t")
        if t == "state":
            self._push(msg.get("d") or {})
        elif t == "sheet":
            # The status icon sprite sheet, once per page load. Passed as JSON
            # arguments for the same reason the state is — it is a data URI,
            # and interpolating half a megabyte into a script expression is a
            # different kind of mistake but still a mistake.
            try:
                self.window.evaluate_js(
                    "window.setIconSheet(%s,%d,%d)"
                    % (json.dumps(msg.get("uri") or ""),
                       int(msg.get("cell") or 64), int(msg.get("cols") or 16)))
            except Exception as e:
                _log(f"icon sheet push failed: {e!r}")
        elif t == "show":
            self.show()
        elif t == "hide":
            self.hide()
        elif t == "quit":
            self._closing = True
            try:
                self.window.destroy()
            finally:
                os._exit(0)

    def _push(self, data):
        """Hand one state object to the page.

        Passed as a JSON string argument rather than interpolated into the
        script text: the state carries player names and shard ids straight off
        the wire, and a name with a quote in it would otherwise end the
        expression and break the panel.
        """
        try:
            self.window.evaluate_js(
                f"window.applyState({json.dumps(json.dumps(data))})")
        except Exception as e:
            _log(f"state push failed: {e!r}")


def _own_hwnd(window):
    """The panel's real window handle.

    Asked of pywebview directly rather than found by enumeration. The first
    version of this walked EnumWindows looking for a visible window belonging
    to this process, and got 0 every time — the panel is created hidden, so
    there was nothing visible to find, and everything that needed the handle
    (the drag, the resize grips, the size fix) silently did nothing.
    """
    if sys.platform != "win32":
        return 0
    # window.native is the WinForms Form, assigned in its constructor, so it
    # exists as soon as the window does — hidden or not.
    try:
        native = getattr(window, "native", None)
        if native is not None:
            return int(native.Handle.ToInt64())
    except Exception as e:
        _log(f"handle via window.native failed ({e!r}); enumerating")
    # Fallback: any top-level window this process owns, visible or not.
    u = ctypes.windll.user32
    me = os.getpid()
    found = []

    def visit(hwnd, _lp):
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == me and u.GetWindow(hwnd, 4) == 0:   # GW_OWNER
            found.append(hwnd)
            return False
        return True

    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    try:
        u.EnumWindows(proc(visit), 0)
    except Exception:
        return 0
    return found[0] if found else 0


def _rect(hwnd):
    """(x, y, w, h) in PHYSICAL pixels.

    Not window.x / window.width: pywebview reports and accepts LOGICAL pixels
    and converts using the monitor's DPI (see its _scale). We pin Chromium's
    device scale factor to 1 precisely so that one CSS pixel is one real pixel,
    and letting pywebview's own scaling back in through the geometry would put
    the system scaling we just removed straight back — multiplied afresh on
    every save/restore round trip.
    """
    r = wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return (r.left, r.top, r.right - r.left, r.bottom - r.top)


def _document():
    """One self-contained HTML document, assembled from the three files.

    Inlined rather than loaded as file:// URLs so there is no origin to get
    wrong and no second request to fail — and the assets stay three editable
    files rather than one unreadable string constant.
    """
    def read(name):
        try:
            return (WEB_DIR / name).read_text(encoding="utf-8")
        except OSError as e:
            _log(f"missing web asset {name}: {e}")
            return ""

    html = read("menu.html")
    return (html
            .replace("/*CSS*/", read("menu.css"))
            .replace("/*JS*/", read("menu.js")))


def main():
    # Line buffering matters on both: the parent reads us a line at a time and
    # a buffered reply is a reply that arrives after the timeout.
    try:
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    geom = {}
    if len(sys.argv) > 1:
        try:
            geom = json.loads(sys.argv[1])
        except ValueError:
            pass
    pipe = Pipe()
    win = MenuWindow(pipe, geom)
    threading.Thread(target=pipe.read_forever, daemon=True).start()
    webview.start(win.attach, debug=bool(os.environ.get("FAREVER_MENU_DEBUG")))
    # start() returns when the window is destroyed for good.
    pipe.send({"t": "closed"})


if __name__ == "__main__":
    main()
