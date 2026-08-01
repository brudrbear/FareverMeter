"""Host for summon_probe.js — is summon/pet damage visible, and whose is it?

    py frida\\run_summon.py [seconds] [path\\to\\hlboot.dat]

The meter drops every dealer that is not an ent.Hero, so a summoned imp or a
totem contributes nothing to its owner's parse. This probe tallies EVERY dealer
class through ent.Unit.onInflictDamage and, for each non-hero dealer, prints
the six candidate links back to an owner side by side.

Play a summoning build: cast the summon, let it attack, and keep hitting things
yourself so the hero baseline is in the same log.

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

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 300.0


def build_targets():
    code = HLCode(find_hlboot(argv_index=2)).parse()
    assert_resolver_current(code)   # stale analysis_out reads as 'found nothing'
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
        """Every class whose super-chain reaches `root`, root included.

        The probe dispatches ent.Foe methods and reads ent.Foe fields; doing
        either on an object that is not a Foe reads past the end of it, which
        does not throw — it produces plausible garbage. Membership first.
        """
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
            "isSummon": proto("ent.Foe", "isSummon"),
            "get_summonHero": proto("ent.Foe", "get_summonHero"),
        },
        # ent.Foe.summonOwner is ent.GameObject, NOT ent.Hero — so whatever it
        # points at has to be type-checked at runtime rather than assumed.
        "Foe": offs("ent.Foe", "summonOwner", "summonSourceSkill",
                    "persistantSummon"),
        # ownerPlayer is declared way up on st.BaseState, so every unit, skill
        # and summon carries one. That breadth is why it needs measuring: a
        # field present on everything is not automatically meaningful.
        "GameObject": offs("ent.GameObject", "ownerPlayer"),
        "BaseSkill": offs("st.skill.BaseSkill", "kind", "owner", "ownerPlayer"),
        "Unit": offs("ent.Unit", "kind"),
        "Hero": offs("ent.Hero", "name"),
        "Player": offs("st.Player", "name", "isMe"),
        "foeClasses": descendants("ent.Foe"),
        "unitClasses": descendants("ent.Unit"),
    }
    for need in ("summonOwner", "summonSourceSkill"):
        if need not in b["Foe"]:
            raise SystemExit(f"[!] ent.Foe.{need} not found — the class changed; "
                             "re-run discovery before trusting this probe.")
    if b["fn"]["isSummon"] is None:
        print("[warn] ent.Foe.isSummon not found — candidate F will be skipped")
    return b


def on_message(message, data):
    if message["type"] == "error":
        print("[JS ERROR]", message.get("description"))
        return
    p = message.get("payload") or {}
    if p.get("kind") == "log":
        # Console is cp1252; game text (hero names, skill ids) can carry
        # anything. A UnicodeEncodeError here would kill the probe instead of
        # reporting what it found.
        print(str(p["msg"]).encode("ascii", "replace").decode(), flush=True)


def main():
    b = build_targets()
    print(f"[*] isSummon findex={b['fn']['isSummon']} "
          f"get_summonHero findex={b['fn']['get_summonHero']}")
    print(f"[*] Foe.summonOwner@{b['Foe']['summonOwner']} "
          f"summonSourceSkill@{b['Foe']['summonSourceSkill']} "
          f"GameObject.ownerPlayer@{b['GameObject']['ownerPlayer']} "
          f"BaseSkill.owner@{b['BaseSkill']['owner']}")
    print(f"[*] {len(b['foeClasses'])} foe classes, "
          f"{len(b['unitClasses'])} unit classes")

    out = HERE.parent / "analysis_out"
    data = (out / "resolver_data.json").read_text(encoding="utf-8")
    off = (out / "meter_offsets.json").read_text(encoding="utf-8")
    js = (HERE / "summon_probe.js").read_text(encoding="utf-8")
    src = (f"const DATA = {data};\nconst OFF = {off};\n"
           f"const B = {json.dumps(b)};\n" + js)

    session = frida.attach("Farever.exe")
    script = session.create_script(src)
    script.on("message", on_message)
    script.load()
    print(f"[*] recording for {DURATION:.0f}s. Wait for '>>> PROBE ARMED <<<' "
          "before doing anything in game. Ctrl+C to stop early.")
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
