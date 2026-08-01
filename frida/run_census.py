"""Host for census_probe.js — completeness + ground truth for summon damage.

    py frida\\run_census.py [seconds] [path\\to\\hlboot.dat]

Three questions in one run, best done at a TRAINING DUMMY:

  1. COMPLETENESS. Hooks ent.Foe.set_summonOwner / initSummonedFoe so every
     summon is recorded at BIRTH, not when it happens to swing. Any summon that
     deals damage without a birth record is a hole in the ownership path — the
     only way to find a summon that arrives by a route nobody looked for.

  2. GROUND TRUTH. Sums my damage + my summon's + everyone else's against the
     target's actual health loss. If attributing summon damage is right, the
     ratio including it sits closer to 1.0 than the ratio without it. A dummy is
     the only clean place to measure this: it stays alive, so the sample gets
     large and the one-hit sampling bias washes out.

  3. THE DANGLING OWNER. summonOwner is a raw ent.Hero pointer. Every census
     entry is re-validated on a timer; a summon whose owner stops reading as an
     ent.Hero, or whose owner's name changes, is the failure that would put a
     garbage name on the meter. Provoke it by dying, or zoning, with a summon up.

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

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 420.0


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
        "fn": {
            "set_summonOwner": proto("ent.Foe", "set_summonOwner"),
            "initSummonedFoe": proto("ent.Foe", "initSummonedFoe"),
        },
        "Foe": offs("ent.Foe", "summonOwner", "summonSourceSkill"),
        "Unit": offs("ent.Unit", "kind", "attr", "combatDamages",
                     "combatDamageHistory"),
        # maxHealth reads 0 for a whole boss fight (measured) — only `health`
        # is trustworthy, and only for "how much did it go down by".
        "UnitAttributes": offs("ent.UnitAttributes", "health"),
        "Hero": offs("ent.Hero", "name"),
        "foeClasses": descendants("ent.Foe"),
        "unitClasses": descendants("ent.Unit"),
    }
    if b["fn"]["set_summonOwner"] is None and b["fn"]["initSummonedFoe"] is None:
        raise SystemExit("[!] neither ent.Foe.set_summonOwner nor initSummonedFoe "
                         "found — the census cannot be built.")
    if "summonOwner" not in b["Foe"]:
        raise SystemExit("[!] ent.Foe.summonOwner not found — class changed.")
    if "health" not in b["UnitAttributes"]:
        raise SystemExit("[!] ent.UnitAttributes.health not found — the "
                         "conservation check needs it.")
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
    print(f"[*] set_summonOwner findex={b['fn']['set_summonOwner']} "
          f"initSummonedFoe findex={b['fn']['initSummonedFoe']}")
    print(f"[*] Foe.summonOwner@{b['Foe']['summonOwner']} "
          f"Unit.attr@{b['Unit']['attr']} "
          f"UnitAttributes.health@{b['UnitAttributes']['health']}")

    out = HERE.parent / "analysis_out"
    data = (out / "resolver_data.json").read_text(encoding="utf-8")
    off = (out / "meter_offsets.json").read_text(encoding="utf-8")
    js = (HERE / "census_probe.js").read_text(encoding="utf-8")
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
