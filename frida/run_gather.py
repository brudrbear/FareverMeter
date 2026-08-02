"""Host for gather_probe.js — ore/herb node discovery for the minimap.

    py frida\\run_gather.py [seconds] [path\\to\\hlboot.dat]

Static analysis already answered "what class are gathering nodes"
(ent.interactible.Gatherable, an ent.Element subclass). This session answers
what static cannot: which layer array they live in, what a mined-out node
looks like in memory (hitPoints? stateId? visual state? removed? gone?), what
getNoActionReason says when you can't gather (wrong tool / nothing at all),
and what the CDB row carries per kind (display name, required tool,
max hitPoints, respawn time).

The in-game shopping list, best done near known ore AND herb nodes:

  1. Stand near a fresh node — the NEAR lines dump its full state.
  2. Mine/gather one to depletion — hit/set_hitPoints/consume events plus
     CHANGE lines show the depletion state machine.
  3. Stay near the depleted node ~a minute — does respawnTime tick, does a
     respawn event fire, do the fields return to fresh?
  4. Walk up WITHOUT the right tool (or to a node of the other profession) —
     getNoActionReason's vocabulary is the "mineable by me" signal.

Findices and offsets are resolved by NAME out of hlboot.dat, not hardcoded.
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


def build_targets():
    code = HLCode(find_hlboot(argv_index=2)).parse()
    assert_resolver_current(code)
    byname = {t.name: t for t in code.types
              if t.kind in (HOBJ, HSTRUCT) and t.name}

    def proto(cls, meth):
        t = byname.get(cls)
        return next((p.findex for p in t.protos if p.name == meth), None) if t else None

    def offs(cls, *fields):
        t = byname.get(cls)
        if not t:
            return {}
        o = code.field_offsets(t.index)
        return {f: o[f][0] for f in fields if f in o}

    def descendants(root):
        ti = byname[root].index
        out = []
        for t in code.types:
            if t.kind not in (HOBJ, HSTRUCT) or not t.name:
                continue
            i, seen = t.index, set()
            while 0 <= i < len(code.types) and i not in seen:
                seen.add(i)
                if i == ti:
                    out.append(t.name)
                    break
                i = code.types[i].super_index
        return out

    CLS = "ent.interactible.Gatherable"
    b = {
        "fn": {m: proto(CLS, m) for m in
               ("hit", "set_hitPoints", "consume", "respawn",
                "tryRequestInteraction", "getNoActionReason")},
        "G": offs(CLS, "gatherInf", "respawnTime", "lastLocalInteractTime",
                  "hitPoints", "affixId", "gatherers"),
        "gatherClasses": descendants(CLS),
    }
    if b["fn"]["hit"] is None and b["fn"]["set_hitPoints"] is None:
        raise SystemExit("[!] Gatherable.hit / set_hitPoints not found — "
                         "class layout changed, re-survey before probing.")
    if "hitPoints" not in b["G"] or "gatherInf" not in b["G"]:
        raise SystemExit("[!] Gatherable.hitPoints/gatherInf not found — "
                         "class layout changed.")
    return b


def on_message(message, data):
    if message["type"] == "error":
        print("[JS ERROR]", message.get("description"))
        return
    p = message.get("payload") or {}
    if p.get("kind") == "log":
        print(str(p["msg"]).encode("ascii", "replace").decode(), flush=True)


def main():
    b = build_targets()
    print(f"[*] Gatherable classes: {b['gatherClasses']}")
    print(f"[*] offsets: {b['G']}")
    print(f"[*] hooks: { {k: v for k, v in b['fn'].items()} }")

    out = HERE.parent / "analysis_out"
    data = (out / "resolver_data.json").read_text(encoding="utf-8")
    off = (out / "meter_offsets.json").read_text(encoding="utf-8")
    js = (HERE / "gather_probe.js").read_text(encoding="utf-8")
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
        try:
            script.unload()
            session.detach()
        except Exception:
            pass
    print("[done]")


if __name__ == "__main__":
    main()


