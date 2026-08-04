"""Host for boost_probe.js — is a Swarmstrike Accord proc traceable to whoever
actually swung?

    py frida\\run_boost.py [seconds] [path\\to\\hlboot.dat]

The meter currently pulls every boosted hit into a BOOST column because the
game credits the bonus to the buff's CASTER rather than to the player whose
attack set it off. The cdb says the bonus is dealt by a STATUS
(`DS_Bladeleaf_Skill2_Status`) sitting on each blessed ally, whose script runs
on that ally's own attacks — so a per-ally identity may exist on the damage
event. This probe reads every candidate field side by side and says which, if
any, names the swinger.

RUN IT FROM A BUFFED ALLY'S CLIENT, not the wielder's. Stand in range of the
player carrying Wingsabers (`DS_Z1RBee_AssWiz`) so the blessing lands on you,
then attack normally. Your own swings are the controlled case: a field that
names the swinger has to come back as your hero.

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

# Both ids the cdb carries for Swarmstrike Accord. The parent is the cast that
# applies the blessing; the _Status suffix is the per-ally buff whose
# BonusDamage step is the damage in question. Matched as PREFIXES so whichever
# of the two the DamageResult actually names is caught — which one it is has
# never been measured, and is itself an output of this probe (it is what
# BOOST_SKILL_IDS in the meter should be pinned to).
BOOST_PREFIXES = ["DS_Bladeleaf_Skill2"]


def build_targets():
    code = HLCode(find_hlboot(argv_index=2)).parse()
    assert_resolver_current(code)   # stale analysis_out reads as 'found nothing'
    byname = {t.name: t for t in code.types
              if t.kind in (HOBJ, HSTRUCT) and t.name}

    def offs(cls, *fields):
        t = byname.get(cls)
        if not t:
            return {}
        o = code.field_offsets(t.index)
        return {f: o[f][0] for f in fields if f in o}

    def descendants(root):
        """Every class whose super-chain reaches `root`, root included.

        Reading a class's fields off an object that isn't one of its instances
        does not throw — it returns plausible garbage. Membership first."""
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
        # ctx and weakSource are not in meter_offsets.json — the meter has
        # never read either. weakSource in particular was left untested by the
        # summon spike, which only ruled out serverSource.
        "DamageResult": offs("st.skill.DamageResult", "ctx", "weakSource"),
        "SkillContext": offs("st.skill.SkillContext", "baseSkill"),
        "BaseSkill": offs("st.skill.BaseSkill", "kind", "owner", "ownerPlayer"),
        "Unit": offs("ent.Unit", "kind"),
        "Hero": offs("ent.Hero", "name"),
        "Player": offs("st.Player", "name", "isMe"),
        "unitClasses": descendants("ent.Unit"),
        "foeClasses": descendants("ent.Foe"),
        "boostPrefixes": BOOST_PREFIXES,
    }
    for grp, need in (("DamageResult", "ctx"), ("DamageResult", "weakSource"),
                      ("SkillContext", "baseSkill"), ("BaseSkill", "owner")):
        if need not in b[grp]:
            raise SystemExit(f"[!] {grp}.{need} not found — the class changed; "
                             "re-run discovery before trusting this probe.")
    return b


def on_message(message, data):
    if message["type"] == "error":
        print("[JS ERROR]", message.get("description"))
        return
    p = message.get("payload") or {}
    if p.get("kind") == "log":
        # Console is cp1252; player names can carry anything. A
        # UnicodeEncodeError here would kill the probe instead of reporting.
        print(str(p["msg"]).encode("ascii", "replace").decode(), flush=True)


def main():
    b = build_targets()
    print(f"[*] DamageResult.ctx@{b['DamageResult']['ctx']} "
          f"weakSource@{b['DamageResult']['weakSource']} "
          f"BaseSkill.owner@{b['BaseSkill']['owner']} "
          f"ownerPlayer@{b['BaseSkill']['ownerPlayer']}")
    print(f"[*] matching skill kinds prefixed: {', '.join(BOOST_PREFIXES)}")
    print(f"[*] {len(b['unitClasses'])} unit classes, "
          f"{len(b['foeClasses'])} foe classes")

    out = HERE.parent / "analysis_out"
    data = (out / "resolver_data.json").read_text(encoding="utf-8")
    off = (out / "meter_offsets.json").read_text(encoding="utf-8")
    js = (HERE / "boost_probe.js").read_text(encoding="utf-8")
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
