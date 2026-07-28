"""
find_damage.py — locate the damage-application function(s) and the combat
type layouts in Farever's hlboot.dat, using hlbc_parser.

Writes a report to analysis_out/ and prints the strongest candidates.
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from hlbc_parser import HLCode, HOBJ, HSTRUCT

HLBOOT = r"E:\SteamLibrary\steamapps\common\Farever\hlboot.dat"
OUT = Path(__file__).resolve().parent.parent / "analysis_out"
OUT.mkdir(exist_ok=True)

# Method-name tokens that plausibly name a damage-application routine.
DMG_METHOD_RX = re.compile(
    r"(applyDamage|dealDamage|takeDamage|doDamage|onDamage|inflict|"
    r"computeDamage|hurt|damage|onHit|applyHit|dealHit|receiveHit)",
    re.IGNORECASE)

# Class names of interest for combat.
CLASS_TOKENS = ("Entity", "Player", "Hero", "Monster", "Mob", "Creature",
                "Skill", "Spell", "Ability", "Damage", "Combat", "Buff",
                "Projectile", "Weapon", "Actor", "Unit", "Character")


def main():
    code = HLCode(HLBOOT).parse()
    names = code.findex_names()
    lines = []

    def out(s=""):
        lines.append(s)
        print(s)

    out(f"# Farever combat analysis  (hlboot v{code.version}, "
        f"{len(code.types)} types, {code.counts['nfunctions']} functions)")
    out()

    # ---- 1) damage-ish methods ----
    out("## Candidate damage/hit methods (Class.method -> findex)")
    hits = []
    for t in code.types:
        if t.kind not in (HOBJ, HSTRUCT) or not t.name:
            continue
        for p in t.protos:
            if DMG_METHOD_RX.search(p.name):
                sig = code.type_str(p.findex) if False else ""
                hits.append((f"{t.name}.{p.name}", p.findex, t.index))
    # de-dupe, sort by how "core" the name looks
    def rank(item):
        name = item[0].lower()
        for i, kw in enumerate(["applydamage", "dealdamage", "takedamage",
                                "ondamage", "inflict", "computedamage",
                                "hurt", "onhit", "damage", "hit"]):
            if kw in name:
                return i
        return 99
    hits.sort(key=rank)
    for name, findex, tindex in hits[:60]:
        out(f"    {name:<55} findex={findex:<6} (type {tindex})")
    out(f"  ...({len(hits)} total)")
    out()

    # ---- 2) key combat classes and their fields ----
    out("## Combat class field layouts")
    wanted = []
    for t in code.obj_types():
        base = t.name.split(".")[-1].lstrip("$")
        if any(tok == base or tok in base for tok in CLASS_TOKENS):
            wanted.append(t)
    # Prioritize exact/short names
    wanted.sort(key=lambda t: (len(t.name), t.name))
    shown = 0
    for t in wanted:
        # skip impl/iterator noise
        if any(x in t.name for x in ("Iterator", "_Impl_", "Builder", "Editor",
                                     "UI", "Menu", "Tooltip", "Icon")):
            continue
        sup = code.types[t.super_index].name if 0 <= t.super_index < len(code.types) \
            and code.types[t.super_index].name else None
        out(f"\n### {t.name}" + (f"  (extends {sup})" if sup else ""))
        out(f"    fields ({len(t.fields)}):")
        for f in t.fields[:40]:
            out(f"      {f.name:<28} : {code.type_str(f.type_index)}")
        if t.protos:
            pnames = ", ".join(p.name for p in t.protos[:24])
            out(f"    methods: {pnames}{' ...' if len(t.protos) > 24 else ''}")
        shown += 1
        if shown >= 25:
            out("\n  ...(more classes omitted)")
            break

    (OUT / "combat_analysis.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n[written] {OUT / 'combat_analysis.md'}")


if __name__ == "__main__":
    main()
