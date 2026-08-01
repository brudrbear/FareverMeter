"""Host for ledger_probe.js — how does the GAME credit a summon's damage?

    py frida\\run_ledger.py [seconds] [path\\to\\hlboot.dat]

ent.Unit.combatDamages / combatDamageHistory are the client's own damage
ledger — the arrays behind Farever's built-in combat meter. Reading them turns
"which field links a summon to its owner" (already measured) into "how does the
game itself account for a summon's damage", which is an oracle rather than
another sample.

Play a summoning build and hit things with the summon up. Watch whether
combatDamages grows to 2 entries (summon counted separately) or stays at 1
(summon folded into its owner).

The element type of both arrays is erased in the bytecode, so this run REPORTS
the runtime type and a hexdump rather than guessing a layout. The decode
follows offline, by name, from hlboot.dat.
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

    b = {
        # BOTH implementations. ent.Foe OVERRIDES ent.Unit.recordsDamage, and
        # calling the base findex on a Foe runs the wrong function entirely —
        # which is how the first run reported recordsDamage=no for every foe in
        # a rift. There is no vtable dispatch through a raw findex call, so the
        # probe has to pick the implementation by class itself.
        "fn": {"recordsDamage": proto("ent.Unit", "recordsDamage"),
               "recordsDamageFoe": proto("ent.Foe", "recordsDamage")},
        "Unit": offs("ent.Unit", "kind", "combatDamages", "combatDamageHistory"),
        "Foe": offs("ent.Foe", "summonOwner"),
        "Hero": offs("ent.Hero", "name"),
        "Player": offs("st.Player", "name"),
        "foeClasses": descendants("ent.Foe"),
        "unitClasses": descendants("ent.Unit"),
    }
    for need in ("combatDamages", "combatDamageHistory"):
        if need not in b["Unit"]:
            raise SystemExit(f"[!] ent.Unit.{need} not found — the class changed.")
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
    print(f"[*] recordsDamage findex={b['fn']['recordsDamage']}  "
          f"combatDamages@{b['Unit']['combatDamages']}  "
          f"combatDamageHistory@{b['Unit']['combatDamageHistory']}")

    out = HERE.parent / "analysis_out"
    data = (out / "resolver_data.json").read_text(encoding="utf-8")
    off = (out / "meter_offsets.json").read_text(encoding="utf-8")
    js = (HERE / "ledger_probe.js").read_text(encoding="utf-8")
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
