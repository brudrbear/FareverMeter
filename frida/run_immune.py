"""Host for immune_probe.js — why immune hits still get counted.

    py frida\\run_immune.py [seconds] [path\\to\\hlboot.dat]

Attaches to a running Farever and tallies every damage instance by the fields
that could mark it as nullified — _block, blocker, effect, isInvulnerable() —
alongside the target's health, which is the ground truth.

Hit a normal mob first, then a boss during an immunity phase, and compare the
buckets. The one where the target's health never moves is the one the meter
should be ignoring.

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

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 240.0


def build_targets():
    code = HLCode(find_hlboot(argv_index=2)).parse()
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

    # Every class descending from ent.Unit. The probe calls
    # ent.Unit.isInvulnerable, and dispatching that on a non-Unit GameObject
    # would be calling a method the object does not have — so the probe checks
    # membership first, and this is where the list comes from.
    unit_ti = byname["ent.Unit"].index
    unit_classes = []
    for t in code.types:
        if t.kind not in (HOBJ, HSTRUCT) or not t.name:
            continue
        i, seen = t.index, set()
        while 0 <= i < len(code.types) and i not in seen:
            seen.add(i)
            if i == unit_ti:
                unit_classes.append(t.name)
                break
            i = code.types[i].super_index

    b = {
        "fn": {
            # ent.Unit overrides ent.GameObject's, and the override is the one
            # that knows about a unit's immunity phases.
            "isInvulnerable": proto("ent.Unit", "isInvulnerable"),
        },
        "DamageResult": offs("st.skill.DamageResult", "_block", "blocker",
                             "effect", "_hitCount"),
        "Unit": offs("ent.Unit", "kind", "attr"),
        "UnitAttributes": offs("ent.UnitAttributes", "health"),
        "unitClasses": unit_classes,
    }
    for need in ("_block", "blocker", "effect"):
        if need not in b["DamageResult"]:
            raise SystemExit(f"[!] DamageResult.{need} not found — the struct "
                             "changed; re-run discovery.")
    return b


def on_message(message, data):
    if message["type"] == "error":
        print("[JS ERROR]", message.get("description"))
        return
    p = message.get("payload") or {}
    if p.get("kind") == "log":
        print(p["msg"], flush=True)


def main():
    b = build_targets()
    print(f"[*] isInvulnerable findex={b['fn']['isInvulnerable']}  "
          f"DamageResult _block@{b['DamageResult']['_block']} "
          f"blocker@{b['DamageResult']['blocker']} "
          f"effect@{b['DamageResult']['effect']}  "
          f"({len(b['unitClasses'])} unit classes)")

    data = (HERE.parent / "analysis_out" / "resolver_data.json").read_text(encoding="utf-8")
    off = (HERE.parent / "analysis_out" / "meter_offsets.json").read_text(encoding="utf-8")
    js = (HERE / "immune_probe.js").read_text(encoding="utf-8")
    src = (f"const DATA = {data};\nconst OFF = {off};\n"
           f"const B = {json.dumps(b)};\n" + js)

    session = frida.attach("Farever.exe")
    script = session.create_script(src)
    script.on("message", on_message)
    # Returns promptly because the agent defers its scan off load(). Doing that
    # scan synchronously blows this handshake's timeout — which is how the
    # first version of this probe failed, capturing nothing.
    script.load()
    print(f"[*] tallying for {DURATION:.0f}s - hit a normal mob, then a boss "
          "during its immune phase. Ctrl+C to stop early.")
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
