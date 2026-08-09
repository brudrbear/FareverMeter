"""Host for statusapply_probe.js — measure how the game applies a status.

    py frida\\run_statusapply.py [seconds] [path\\to\\hlboot.dat]

THIS ROUND CALLS NOTHING. It hooks and watches, so that a later round can call
addStatus (or the gather path) with a signature that was measured rather than
guessed. hlbc_parser stops before function bodies, so a proto gives a findex and
no signature at all; the glider work already lost a round to an argument that
looked like a cdb row and was actually a closure.

TWO PATHS ARE BEING MEASURED AT ONCE, because they need the same in-game work:

  PATH A — grant the status directly.
      ent.GameObject.addStatus      (what a cdb script's u.addStatus() reaches)
      script.UnitScript.addStatus   (the script-facing wrapper)
      st.skill.Status.init          (the far end — what gets constructed)

  PATH B — ask the game to gather a node and let the SERVER grant the buff.
      ent.interactible.Gatherable.hit / tryRequestInteraction /
      doActionServer / consume / setActiveAffix

Path B is likelier to stick: statuses are hxbit-replicated and server-owned, so
a direct client-side grant may simply be overwritten, whereas the gather path is
the route the server already trusts. Path B's risk is the opposite one — a
distance or state check refusing it (Loot_MaxInteractDistance is 5 in the cdb,
and canConsume / getNoActionReason exist to say no).

THE IN-GAME SHOPPING LIST:

  1. WAIT for "PROBE ARMED".
  2. Fight something for ~30s. Dash, use class skills, eat food. Everything that
     buffs anything routes through addStatus, so this fills in its shapes fast.
  3. Then find a BLESSED node — an ore or herb with the elemental FX on it
     (10% of nodes roll one; Fire/Ice/Nature/Wind are 24% each of that, Chaos
     4%) — and gather it to completion. That fills in the Gatherable shapes and,
     crucially, shows addStatus being called with the exact buff we want.
  4. If you happen to find a CHAOS node, gather that too and say so. It is the
     one affix with no status row in the cdb, so whatever it does is only
     visible live.

Everything is resolved by NAME from hlboot.dat at launch.
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

GATHERABLE = "ent.interactible.Gatherable"


def build_targets():
    code = HLCode(find_hlboot(argv_index=2)).parse()
    assert_resolver_current(code)
    byname = {t.name: t for t in code.types
              if t.kind in (HOBJ, HSTRUCT) and t.name}

    def proto(cls, meth):
        t = byname.get(cls)
        if not t:
            return None
        return next((x.findex for x in t.protos if x.name == meth), None)

    def offs(cls, **want):
        t = byname.get(cls)
        if not t:
            return {}
        o = code.field_offsets(t.index)
        return {out: o[f][0] for out, f in want.items() if f in o}

    p = {
        "fn": {
            "addStatus":       proto("ent.GameObject", "addStatus"),
            "scriptAddStatus": proto("script.UnitScript", "addStatus"),
            "statusInit":      proto("st.skill.Status", "init"),
            "gHit":            proto(GATHERABLE, "hit"),
            "gTryRequest":     proto(GATHERABLE, "tryRequestInteraction"),
            "gDoAction":       proto(GATHERABLE, "doActionServer"),
            "gConsume":        proto(GATHERABLE, "consume"),
            "gSetAffix":       proto(GATHERABLE, "setActiveAffix"),
        },
        "Unit": offs("ent.Unit", kind="kind"),
        "Status": offs("st.skill.Status", kind="kind"),
    }
    if p["fn"]["addStatus"] is None:
        raise SystemExit("[!] ent.GameObject.addStatus not found - aborting.")
    missing = [k for k, v in p["fn"].items() if v is None]
    if missing:
        print(f"[!] not resolved (those hooks are skipped): {missing}")
    return p


def on_message(message, data):
    if message["type"] == "error":
        print("[JS ERROR]", message.get("description"))
        return
    pl = message.get("payload") or {}
    if pl.get("kind") == "log":
        print(str(pl["msg"]).encode("ascii", "replace").decode(), flush=True)


def main():
    p = build_targets()
    print("[*] findices: " + json.dumps(p["fn"]), flush=True)

    data = (HERE.parent / "analysis_out" / "resolver_data.json").read_text(encoding="utf-8")
    off = (HERE.parent / "analysis_out" / "meter_offsets.json").read_text(encoding="utf-8")
    js = (HERE / "statusapply_probe.js").read_text(encoding="utf-8")
    src = (f"const DATA = {data};\nconst OFF = {off};\n"
           f"const P = {json.dumps(p)};\n" + js)

    session = frida.attach("Farever.exe")
    script = session.create_script(src)
    script.on("message", on_message)
    script.load()
    print(f"[*] attached, running for {DURATION:.0f}s. Nothing is called - this "
          "round only watches. Ctrl+C to stop early.", flush=True)
    try:
        time.sleep(DURATION)
    except KeyboardInterrupt:
        print("\n[*] stopping early.")
    finally:
        try:
            script.post({"type": "summary"})
            time.sleep(0.8)
        except Exception:
            pass
        # Hard-killing this host with live hooks is the known game-crasher, so
        # if the unload wedges we say so and WAIT.
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
