"""Host for nodefinder_probe.js — find blessed gathering nodes.

    py frida\\run_nodefinder.py [seconds] [--allow-interact] [path\\to\\hlboot.dat]

WHAT ROUND 3 SETTLED. ent.GameObject.addStatus never runs on the client: across
212 st.skill.Status.init calls during combat and a rift it fired zero times, and
Status.init turned out to be running inside network deserialisation. Statuses
arrive from the server already built. Gatherable.hit / consume / doActionServer
/ setActiveAffix likewise never fired. The single client-side entry point is

    Gatherable.tryRequestInteraction(node: Gatherable, hero: Hero)   [arity 2]

The client asks; the server decides and replicates the answer back. So there is
no "grant me a buff" to call, and the useful tool is the one that finds the
blessed node for you.

DEFAULT IS READ-ONLY. The census reads `affixId`, a REPLICATED property, so the
client knows which affix a node rolled before you touch it. Blessed nodes are
listed with distance and compass bearing. This is the part worth shipping in the
meter.

--allow-interact additionally binds tryRequestInteraction so it can be called on
command, on the game thread, against a named node that is logged before the
call. Nothing is called without an explicit command.

THE IN-GAME SHOPPING LIST:
  1. Wait for "PROBE ARMED".
  2. Run around ore/herb country. Every 5s the census prints the affixId
     distribution across every node in the layer plus any blessed ones.
     ~90% of nodes should share one value (that is "no affix"); the rest spread
     across the four elements, with Chaos rare.
  3. Walk up to a blessed node and say so — that is the interact test target.
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

CLS = "ent.interactible.Gatherable"

argv = [a for a in sys.argv[1:]]
ALLOW = "--allow-interact" in argv
argv = [a for a in argv if a != "--allow-interact"]
DURATION = float(argv[0]) if argv else 600.0
HLBOOT_ARG = argv[1] if len(argv) > 1 else None

# Index -> label for Gatherable.props.affixes. Ore and Plant declare the SAME
# order in data.cdb, so one table serves both. Printed alongside the raw number
# so a 1-based or -1-for-none encoding shows up as a mismatch instead of a
# silently wrong label.
AFFIX_NAMES = ["Physical", "Fire", "Ice", "Nature", "Wind", "Chaos", "Spark"]


def build_targets():
    code = HLCode(find_hlboot(argv_index=99) if HLBOOT_ARG is None
                  else HLBOOT_ARG).parse()
    assert_resolver_current(code)
    byname = {t.name: t for t in code.types
              if t.kind in (HOBJ, HSTRUCT) and t.name}

    def proto(cls, meth):
        t = byname.get(cls)
        if not t:
            return None
        return next((x.findex for x in t.protos if x.name == meth), None)

    def offs(cls, *want):
        t = byname.get(cls)
        if not t:
            return {}
        o = code.field_offsets(t.index)
        return {f: o[f][0] for f in want if f in o}

    def descendants(root):
        out, changed = {root}, True
        while changed:
            changed = False
            for t in code.types:
                if t.kind not in (HOBJ, HSTRUCT) or not t.name or t.name in out:
                    continue
                si = t.super_index
                if si is not None and 0 <= si < len(code.types):
                    sup = code.types[si]
                    if sup.name in out:
                        out.add(t.name)
                        changed = True
        return sorted(out)

    b = {
        "fn": {
            "postUpdate": proto("client.BaseCamera", "postUpdate"),
            "tryRequestInteraction": proto(CLS, "tryRequestInteraction"),
        },
        "G": offs(CLS, "affixId", "hitPoints", "gatherers", "respawnTime"),
        "gatherClasses": descendants(CLS),
        "affixNames": AFFIX_NAMES,
        "allowInteract": ALLOW,
    }
    if b["fn"]["postUpdate"] is None:
        raise SystemExit("[!] client.BaseCamera.postUpdate not found - aborting.")
    if "affixId" not in b["G"]:
        raise SystemExit("[!] Gatherable.affixId not found - class layout "
                         "changed; re-survey before probing.")
    if ALLOW and b["fn"]["tryRequestInteraction"] is None:
        raise SystemExit("[!] --allow-interact asked for, but "
                         "tryRequestInteraction did not resolve - aborting "
                         "rather than binding a guessed findex.")
    return b


def on_message(message, data):
    if message["type"] == "error":
        print("[JS ERROR]", message.get("description"))
        return
    pl = message.get("payload") or {}
    if pl.get("kind") == "log":
        print(str(pl["msg"]).encode("ascii", "replace").decode(), flush=True)


def main():
    b = build_targets()
    print(f"[*] Gatherable classes: {b['gatherClasses']}", flush=True)
    print(f"[*] offsets: {b['G']}   findices: {b['fn']}", flush=True)
    print("[*] MODE: " + ("INTERACT ENABLED (calls only on command)" if ALLOW
                          else "READ-ONLY"), flush=True)

    out = HERE.parent / "analysis_out"
    data = (out / "resolver_data.json").read_text(encoding="utf-8")
    off = (out / "meter_offsets.json").read_text(encoding="utf-8")
    js = (HERE / "nodefinder_probe.js").read_text(encoding="utf-8")
    src = (f"const DATA = {data};\nconst OFF = {off};\n"
           f"const B = {json.dumps(b)};\n" + js)

    session = frida.attach("Farever.exe")
    script = session.create_script(src)
    script.on("message", on_message)
    script.load()
    print(f"[*] attached for {DURATION:.0f}s.", flush=True)

    # An interact is requested by dropping a file next to the log, so the host
    # stays non-interactive (stdin is not a console here) and every request is
    # an explicit, deliberate act.
    trigger = Path(HERE.parent / "analysis_out" / "INTERACT_NOW")
    try:
        end = time.time() + DURATION
        while time.time() < end:
            time.sleep(1.0)
            if ALLOW and trigger.exists():
                try:
                    txt = trigger.read_text(encoding="utf-8").strip()
                except Exception:
                    txt = ""
                try:
                    trigger.unlink()
                except Exception:
                    pass
                print(f"[*] interact requested (target={txt or 'nearest'})", flush=True)
                script.post({"type": "interact", "ptr": txt or None})
    except KeyboardInterrupt:
        print("\n[*] stopping early.")
    finally:
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
            print("[!] unload is wedged. Do NOT kill this process - quit the "
                  "game via its own UI when convenient and this will exit on "
                  "its own.", flush=True)
            t.join()
    print("[done]")


if __name__ == "__main__":
    main()
