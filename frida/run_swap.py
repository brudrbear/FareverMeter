"""Host for mount_swap_probe.js — proves the mount arg-swap.

    py frida\\run_swap.py [seconds] [path\\to\\hlboot.dat]

Hooks ent.Hero.setMount on the local hero and swaps the requested mount kind
for a random one from the player's own collection, which is the whole
random-favorite feature minus the favorites list. Run it, summon your mount a
few times, and watch which animal appears.
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

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 300.0


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
        "fn": {"postUpdate": proto("client.BaseCamera", "postUpdate"),
               "setMount": proto("ent.Hero", "setMount"),
               "set_mountId": proto("ent.Hero", "set_mountId")},
        "Hero": offs("ent.Hero", player="player", mountId="mountId"),
        "Player": offs("st.Player", accountProgress="accountProgress",
                       isMe="isMe"),
        "AccountProgress": offs("st.player.AccountProgress",
                                collection="collection"),
        "Collection": offs("st.player.Collection", mounts="mounts"),
        "ArrayProxyData": offs("hxbit.ArrayProxyData", array="array"),
        "ArrayDyn": offs("hl.types.ArrayDyn", array="array"),
    }
    for k, v in p["fn"].items():
        if v is None:
            raise SystemExit(f"[!] {k} did not resolve - aborting.")
    for grp in ("Hero", "Player", "AccountProgress", "Collection",
                "ArrayProxyData", "ArrayDyn"):
        if not p[grp]:
            raise SystemExit(f"[!] offsets for {grp} did not resolve - aborting.")
    return p


def on_message(message, data):
    if message["type"] == "error":
        print("[JS ERROR]", message.get("description"))
        return
    p = message.get("payload") or {}
    if p.get("kind") == "log":
        print(str(p["msg"]).encode("ascii", "replace").decode(), flush=True)


def main():
    p = build_targets()
    print(f"[*] setMount findex={p['fn']['setMount']}  "
          f"set_mountId findex={p['fn']['set_mountId']}", flush=True)

    data = (HERE.parent / "analysis_out" / "resolver_data.json").read_text(encoding="utf-8")
    off = (HERE.parent / "analysis_out" / "meter_offsets.json").read_text(encoding="utf-8")
    js = (HERE / "mount_swap_probe.js").read_text(encoding="utf-8")
    src = (f"const DATA = {data};\nconst OFF = {off};\n"
           f"const P = {json.dumps(p)};\n" + js)

    session = frida.attach("Farever.exe")
    script = session.create_script(src)
    script.on("message", on_message)
    script.load()
    print(f"[*] attached for {DURATION:.0f}s. WAIT for 'PROBE ARMED', then "
          "summon your mount a few times. Ctrl+C to stop.", flush=True)
    try:
        time.sleep(DURATION)
    except KeyboardInterrupt:
        print("\n[*] stopping early.")
    finally:
        try:
            script.unload()
            session.detach()
        except Exception:
            pass
    print("[done]")


if __name__ == "__main__":
    main()
