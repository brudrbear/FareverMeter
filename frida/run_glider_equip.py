"""Host for glider_equip_probe.js — the glider re-equip proof (round 4).

    py frida\\run_glider_equip.py [seconds] [path\\to\\hlboot.dat]

Proves the real-re-equip design end to end:
  * a manual equip is captured and its argument kinds logged. Round 3d
    established that args[2] is a CLOSURE (kind 10 / HFUN, freshly allocated
    per click) — the UI's optional result callback, NOT the item row — and
    that args[3] is caller register leftover. Only the kind String matters;
  * only after the GO file appears next to this script
    (frida\\glider_equip_GO.txt) does the probe arm the auto-equip: each
    local glide-end calls Collection.equipItem(collection, random unlocked
    glider, null) — the UI's own call. Cooldown 5s, cap 5/session.

The GO file is the review gate: the host polls for it and calls the agent's
rpc go(). Delete it before launching to re-run the gated flow.
"""
import json
import sys
import time
from pathlib import Path

import frida

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "hltools"))
from hlbc_parser import HLCode, HOBJ, HSTRUCT      # noqa: E402
from gamepath import find_hlboot                   # noqa: E402
from datafresh import assert_resolver_current      # noqa: E402

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 600.0
GO_FILE = HERE / "glider_equip_GO.txt"


def build_targets():
    code = HLCode(find_hlboot(argv_index=2)).parse()
    assert_resolver_current(code)
    byname = {t.name: t for t in code.types
              if t.kind in (HOBJ, HSTRUCT) and t.name}

    def offs(cls, **want):
        t = byname.get(cls)
        if not t:
            return {}
        o = code.field_offsets(t.index)
        return {out: o[f][0] for out, f in want.items() if f in o}

    def proto(cls, meth):
        t = byname.get(cls)
        if not t:
            return None
        return next((x.findex for x in t.protos if x.name == meth), None)

    p = {
        "fn": {"postUpdate": proto("client.BaseCamera", "postUpdate")},
        "hooks": {
            "Collection.equipItem":    proto("st.player.Collection", "equipItem"),
            "Hero.toggleGlide":        proto("ent.Hero", "toggleGlide"),
            "UnitView.displayGearSlot":
                proto("client.UnitView", "displayGearSlot"),
            "UnitView.loadGearProps":  proto("client.UnitView", "loadGearProps"),
        },
        "Hero": offs("ent.Hero", player="player", name="name"),
        "Player": offs("st.Player", accountProgress="accountProgress",
                       name="name", isMe="isMe"),
        "AccountProgress": offs("st.player.AccountProgress",
                                collection="collection"),
        "Collection": offs("st.player.Collection", gliders="gliders"),
        "ArrayProxyData": offs("hxbit.ArrayProxyData", array="array"),
        "ArrayDyn": offs("hl.types.ArrayDyn", array="array"),
    }
    for grp, need in (("Hero", 2), ("Player", 3), ("AccountProgress", 1),
                      ("Collection", 1), ("ArrayProxyData", 1),
                      ("ArrayDyn", 1)):
        if len(p[grp]) < need:
            raise SystemExit(f"[!] offsets for {grp} did not fully resolve "
                             f"({p[grp]}) - aborting rather than guessing.")
    if p["fn"]["postUpdate"] is None:
        raise SystemExit("[!] client.BaseCamera.postUpdate not found - no "
                         "game-thread anchor; aborting.")
    missing = [k for k, v in p["hooks"].items() if v is None]
    if missing:
        print(f"[!] unresolved hooks (skipped): {missing}", flush=True)
    return p


def on_message(message, data):
    if message["type"] == "error":
        print("[JS ERROR]", message.get("description"))
        return
    p = message.get("payload") or {}
    if p.get("kind") == "log":
        print(str(p["msg"]).encode("ascii", "replace").decode(), flush=True)


def main():
    if GO_FILE.exists():
        print(f"[!] {GO_FILE.name} already exists — delete it first so the "
              "gate is meaningful.", flush=True)
        return
    p = build_targets()

    data = (HERE.parent / "analysis_out" / "resolver_data.json").read_text(encoding="utf-8")
    off = (HERE.parent / "analysis_out" / "meter_offsets.json").read_text(encoding="utf-8")
    js = (HERE / "glider_equip_probe.js").read_text(encoding="utf-8")
    src = (f"const DATA = {data};\nconst OFF = {off};\n"
           f"const P = {json.dumps(p)};\n" + js)

    session = frida.attach("Farever.exe")
    script = session.create_script(src)
    script.on("message", on_message)
    script.load()
    print(f"[*] attached, running for {DURATION:.0f}s. Equip a glider "
          "manually once for the capture; the auto-equip stays disarmed "
          f"until {GO_FILE.name} appears.", flush=True)
    posted_go = False
    deadline = time.time() + DURATION
    try:
        while time.time() < deadline:
            time.sleep(1.0)
            if not posted_go and GO_FILE.exists():
                posted_go = True
                try:
                    proven = script.exports_sync.go()
                except AttributeError:      # older frida python API
                    proven = script.exports.go()
                print(f"[*] GO file seen — rpc go() returned proven={proven}",
                      flush=True)
    except KeyboardInterrupt:
        print("\n[*] stopping early.")
    finally:
        # unload/detach can wedge (observed 2026-08-02: the run sat past its
        # deadline inside cleanup). Killing this process with live hooks is
        # the known game-crasher, so if cleanup hangs we say so and WAIT —
        # the session dies with the game, never the other way round.
        import threading

        def _cleanup():
            try:
                script.unload()
                session.detach()
            except Exception:
                pass

        t = threading.Thread(target=_cleanup, daemon=False)
        t.start()
        t.join(timeout=15.0)
        if t.is_alive():
            print("[!] unload is wedged. Do NOT kill this process — quit the "
                  "game via its own UI when convenient and this will exit on "
                  "its own.", flush=True)
            t.join()          # blocks until the frida session dies
    print("[done]")


if __name__ == "__main__":
    main()
