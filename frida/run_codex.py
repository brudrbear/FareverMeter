"""Host for codex_probe.js — the character's codex (hunting log).

    py frida\\run_codex.py [seconds] [path\\to\\hlboot.dat]

Static analysis found the whole shape already, so this session is only about
what static cannot answer. Two stores exist and the feature needs to know which
one to trust:

    st.player.Progress.unitsProgress : hxbit.MapData   (replicated, per char)
    data.CodexData.unitNodes         : StringMap       (the UI's derived tree)

The in-game shopping list:

  1. Stand somewhere with mobs around. The tick dumps unitsProgress wholesale —
     that says whether it holds kill COUNTS keyed by unit id, and whether it is
     replicated to us at all.
  2. Kill a few of ONE kind. The EVT lines say which client function fires and
     what it carries; the next tick says which numbers moved.
  3. Open the Codex window once. `inited` should flip, at which point every
     nearby unit gets its codex node(s) dumped — name, _progress, maxProgress,
     completed, thresholds. That also answers whether the codex must be opened
     before any of it is readable.
  4. Kill something whose codex entry is CLOSE to full, if you know of one.
     The completion edge is the popup we most want to get right.

Findices and offsets are resolved by NAME out of hlboot.dat, never hardcoded.

ONE FRIDA SESSION AT A TIME on this game: close the meter before running this.
"""
import json
import sys
import threading
import time
from pathlib import Path

import frida

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "hltools"))
from hlbc_parser import HLCode, HOBJ, HSTRUCT      # noqa: E402
from gamepath import find_hlboot                   # noqa: E402
from datafresh import assert_resolver_current      # noqa: E402

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 900.0

# The nine candidates for "a kill credited my codex", client-side. Spread this
# wide on purpose: the item-pickup probe burned three rounds discovering that
# every obvious candidate was server code.
HOOKS = [
    ("st.player.Progress", "incrementUnitKilled"),
    ("st.player.Progress", "syncProgressRank"),
    ("st.player.Progress", "notifyCodexRankProgress"),
    ("st.player.Progress", "getNbUnitKilled"),
    ("st.Player", "notifyCodexUnit"),
    ("st.Player", "notifyCodexUnit__impl"),
    ("st.Player", "onUnitCodexRankProgress"),
    ("st.Player", "onUnitCodexRankProgress__impl"),
    ("st.Player", "notifyUnitKilled__impl"),
]


def build_targets():
    code = HLCode(find_hlboot(argv_index=2)).parse()
    assert_resolver_current(code)
    byname = {t.name: t for t in code.types
              if t.kind in (HOBJ, HSTRUCT) and t.name}

    def offs(cls, *fields):
        t = byname.get(cls)
        if not t:
            return {}
        o = code.field_offsets(t.index)
        missing = [f for f in fields if f not in o]
        if missing:
            raise SystemExit(f"[!] {cls}: fields not found {missing} — "
                             "layout changed, re-survey before probing.")
        return {f: o[f][0] for f in fields}

    def proto(cls, meth):
        t = byname.get(cls)
        return next((p.findex for p in t.protos if p.name == meth), None) if t else None

    def static_binding(cls, name):
        """A static class's method findex, via its bindings table."""
        t = byname.get(cls)
        if not t:
            return None
        for fid, findex in t.bindings:
            if 0 <= fid < len(t.fields) and t.fields[fid].name == name:
                return findex
        return None

    def native(name):
        return next((n.findex for n in code.natives
                     if n.lib == "std" and n.name == name), None)

    # _Data.$Unit_flags_Impl_ is a CDB flag set: each constant is its BIT INDEX,
    # in declaration order, terminated by COUNT/NAMES. Read the index off the
    # declaration rather than hardcoding 18 — a patch that adds a flag above it
    # would otherwise silently shift the meaning of the bit we test.
    ft = byname.get("_Data.$Unit_flags_Impl_")
    no_codex_bit = None
    if ft:
        idx = 0
        for f in ft.fields:
            if f.name in ("COUNT", "NAMES"):
                break
            if f.name == "NoCodex":
                no_codex_bit = idx
                break
            idx += 1
    if no_codex_bit is None:
        raise SystemExit("[!] Unit flags NoCodex not found — CDB flags changed.")

    hooks = {}
    for cls, meth in HOOKS:
        fi = proto(cls, meth)
        if fi is None:
            print(f"    [!] hook target absent in this build: {cls}.{meth}")
        hooks[f"{cls}.{meth}"] = fi

    b = {
        "Player": offs("st.Player", "progress"),
        "Progress": offs("st.player.Progress", "unitsProgress", "itemProgress",
                         "counters"),
        "MapData": offs("hxbit.MapData", "map"),
        "StringMap": offs("haxe.ds.StringMap", "h"),
        "CodexNode": offs("data.CodexNode", "name", "fullPath", "_progress",
                          "completionProgress", "maxProgress", "completed",
                          "_progressLevel", "progressThresholds", "children"),
        "fn": {
            "getUnitNode": static_binding("data.$CodexData", "getUnitNode"),
            "isInCodex": static_binding("data.$CodexData", "isInCodex"),
            "shouldShowUnit": static_binding("data.$CodexData", "shouldShowUnit"),
            "getProgress": static_binding("data.$CodexData", "getProgress"),
            "setUnitProgressRank": static_binding("data.$CodexData",
                                                  "setUnitProgressRank"),
        },
        "natives": {"hbkeys": native("hbkeys"), "hbget": native("hbget"),
                    "hbsize": native("hbsize")},
        "hooks": hooks,
        "NoCodexBit": no_codex_bit,
    }
    # setUnitProgressRank is a hook target too, but it is a STATIC so it has no
    # proto — add it to the hook table now that it's resolved.
    if b["fn"]["setUnitProgressRank"] is not None:
        b["hooks"]["data.CodexData.setUnitProgressRank"] = \
            b["fn"]["setUnitProgressRank"]
    return b


DUMP_PATH = HERE.parent / "analysis_out" / "codex_progress_dump.json"


def on_message(message, data):
    if message["type"] == "error":
        print("[JS ERROR]", message.get("description"))
        print("        ", message.get("stack") or message.get("fileName"))
        return
    p = message.get("payload") or {}
    if not isinstance(p, dict) or "kind" not in p:
        # Anything unrecognised is printed rather than dropped — a silent probe
        # is indistinguishable from a broken one, which cost a round once.
        print("[msg]", str(message)[:400], flush=True)
        return
    if p.get("kind") == "log":
        print(str(p["msg"]).encode("ascii", "replace").decode(), flush=True)
    elif p.get("kind") == "progressdump":
        # The whole replicated table as {id: {count, rank}} — 300+ samples, and
        # the raw material for deriving which rank-threshold set each unit uses
        # (Foe [1,8,20] / BigFoe [1,4,10] / EliteAndBoss [1,1,1]) instead of
        # guessing at what "big" means.
        DUMP_PATH.write_text(json.dumps(
            {"units": p.get("units") or {}, "items": p.get("items") or {}},
            indent=1), encoding="utf-8")
        print(f"[written] {DUMP_PATH} "
              f"({len(p.get('units') or {})} units, "
              f"{len(p.get('items') or {})} items)", flush=True)


def main():
    b = build_targets()
    print(f"[*] Progress offsets : {b['Progress']}")
    print(f"[*] CodexNode offsets: {b['CodexNode']}")
    print(f"[*] static fns       : {b['fn']}")
    print(f"[*] map natives      : {b['natives']}")
    print(f"[*] NoCodex bit      : {b['NoCodexBit']}")
    print(f"[*] hooks            : {b['hooks']}")

    out = HERE.parent / "analysis_out"
    data = (out / "resolver_data.json").read_text(encoding="utf-8")
    off = (out / "meter_offsets.json").read_text(encoding="utf-8")
    js = (HERE / "codex_probe.js").read_text(encoding="utf-8")
    src = (f"const DATA = {data};\nconst OFF = {off};\n"
           f"const B = {json.dumps(b)};\n" + js)

    session = frida.attach("Farever.exe")
    script = session.create_script(src)
    script.on("message", on_message)
    script.load()
    print(f"[*] recording for {DURATION:.0f}s. Wait for '>>> PROBE ARMED <<<'. "
          "Ctrl+C to stop early.")
    try:
        time.sleep(DURATION)
    except KeyboardInterrupt:
        print("\n[*] stopping early.")
    finally:
        # script.unload() WEDGES on this game (measured, twice) — the per-frame
        # postUpdate Interceptor is the prime suspect. Do it on a watchdog
        # thread so a hang is reported rather than looking like a freeze, and
        # NEVER hard-kill this process: a half-reverted inline hook has crashed
        # the game before.
        done = threading.Event()

        def cleanup():
            try:
                script.unload()
                session.detach()
            except Exception:
                pass
            done.set()

        t = threading.Thread(target=cleanup, daemon=True)
        t.start()
        t.join(15)
        if not done.is_set():
            print("[!] detach is wedged (known on this game). DO NOT kill this "
                  "process — quit the game and it will exit on its own.")
            t.join()
    print("[done]")


if __name__ == "__main__":
    main()
