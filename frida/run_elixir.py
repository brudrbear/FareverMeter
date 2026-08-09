"""Host for elixir_probe.js — what does drinking an elixir change on the hero?

    py frida\\run_elixir.py [seconds] [calibration_seconds] [path\\to\\hlboot.dat]

Defaults: 600s run, 25s calibration.

THE QUESTION. You drink an Elixir of Abundance ("Your chance to find rare
components while gathering is increased"). What moves on the client's copy of
the character?

Static analysis (see elixir_probe.js's header) predicts NOTHING moves except a
Status appearing. Elixir of Abundance is the only elixir in the game whose
effect is not an item `affixes` row, and its dedicated status row is an empty
shell — no affixes, no vars, no mastery, no script. There is no gathering
attribute among the 78, and no gathering affix among the 12.

That is a prediction. This runs the experiment: it snapshots ~400 named fields
across the hero, its attribute block, its affix manager, its player record and
the full 78-attribute map, learns which of them move on their own, and then
reports every change to the ones that don't.

THE IN-GAME SHOPPING LIST, in order. Do not improvise — the calibration phase
turns anything you do early into a permanently ignored field.

  1. WAIT for "PROBE ARMED". Then stand still and do nothing at all for the
     calibration window. Don't move, don't swing, don't open a menu. Combat,
     sprinting and menus all touch fields that would otherwise be reported.

  2. WAIT for "CALIBRATED". Now the probe is listening.

  3. CONTROL FIRST — drink an Elixir of Minor Strength (or Dexterity, Armor,
     any of the stat ones). This one has a known cdb effect (+3 Strength,
     TAttribute_Flat) so it PROVES THE PROBE WORKS. If the affix cache and the
     attribute map don't both move here, nothing this run says about abundance
     means anything, and that is the whole point of doing it first.

  4. Then drink the ELIXIR OF ABUNDANCE. Stand still for ~15s after.

  5. Optional but useful: go hit an ore node or a herb with the abundance buff
     up. If any client-side gathering bonus exists it would have to be read at
     that moment, and a field that only moves while gathering would show here.

Everything is resolved by NAME from hlboot.dat at launch. The probe reads and
logs; nothing is called and nothing is written, bar the two std map natives
needed to enumerate the attribute map.
"""
import json
import sys
import time
from pathlib import Path

import frida

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "hltools"))
from hlbc_parser import HLCode, HOBJ, HSTRUCT, HSTRUCT as _HS, HPACKED  # noqa: E402
from gamepath import find_hlboot                   # noqa: E402
from datafresh import assert_resolver_current      # noqa: E402

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 600.0
CALIB = float(sys.argv[2]) if len(sys.argv) > 2 else 25.0

# Every object the snapshot covers: label, offset chain from the hero, class.
# The chain is resolved field-by-field at launch so a rename shows up as a
# missing root rather than as a silently wrong pointer.
ROOT_SPECS = [
    ("Hero", [], "ent.Hero"),
    ("attr", [("ent.Unit", "attr")], "ent.UnitAttributes"),
    ("affixMgr", [("ent.Unit", "affixes")], "ent.AffixManager"),
    ("player", [("ent.Hero", "player")], "st.Player"),
    ("player.stats", [("ent.Hero", "player"), ("st.Player", "stats")],
     "st.player.HeroStats"),
    ("player.progress", [("ent.Hero", "player"), ("st.Player", "progress")],
     "st.player.Progress"),
    ("player.account", [("ent.Hero", "player"), ("st.Player", "accountProgress")],
     "st.player.AccountProgress"),
    ("loadout", [("ent.Hero", "loadout")], "st.Loadout"),
    ("spec", [("ent.Hero", "specialization")], "st.player.HeroSpecialization"),
]


def build_targets():
    code = HLCode(find_hlboot(argv_index=3)).parse()
    assert_resolver_current(code)
    byname = {t.name: t for t in code.types
              if t.kind in (HOBJ, HSTRUCT) and t.name}

    def offs(cls, **want):
        t = byname.get(cls)
        if not t:
            return {}
        o = t and code.field_offsets(t.index)
        return {out: o[f][0] for out, f in want.items() if f in o}

    def proto(cls, meth):
        t = byname.get(cls)
        if not t:
            return None
        return next((x.findex for x in t.protos if x.name == meth), None)

    def native(name):
        return next((n.findex for n in code.natives
                     if n.lib == "std" and n.name == name), None)

    # --- the watched field tables ------------------------------------------
    # Every named field of every watched class, with its runtime offset and hl
    # kind. Nameless fields (the anonymous trailing virtual hxbit adds) and
    # inline structs (which are not pointer-shaped and would be misread) are
    # dropped rather than guessed at.
    fields = {}
    for _, _, cls in ROOT_SPECS:
        t = byname.get(cls)
        if not t:
            print(f"[!] class {cls} not found — that root will be skipped.")
            continue
        table = []
        for name, (off, kind, _ts) in code.field_offsets(t.index).items():
            if not name or kind in (_HS, HPACKED):
                continue
            table.append([name, off, kind])
        table.sort(key=lambda r: r[1])
        fields[cls] = table

    # --- the offset chains --------------------------------------------------
    roots, chains = [], {}
    for label, spec, cls in ROOT_SPECS:
        if cls not in fields:
            continue
        chain, ok = [], True
        for owner, field in spec:
            t = byname.get(owner)
            o = code.field_offsets(t.index) if t else {}
            if field not in o:
                print(f"[!] {owner}.{field} not found — root {label} skipped.")
                ok = False
                break
            chain.append(o[field][0])
        if ok:
            roots.append([label, chain, cls])
            chains[label] = chain

    # The attribute enum's own field order IS the IntMap's key space: the
    # generated _Data.$AttributeKind_Impl_ lists all 78 attributes in cdb sheet
    # order followed by a `toString`, which is dropped.
    attr_names = {}
    ae = byname.get("_Data.$AttributeKind_Impl_")
    if ae:
        for i, f in enumerate(ae.fields):
            if f.name == "toString":
                continue
            attr_names[i] = f.name

    p = {
        "fn": {"postUpdate": proto("client.BaseCamera", "postUpdate")},
        "fields": fields,
        "roots": roots,
        "chain": {
            "attr": chains.get("attr", []),
            "affixes": chains.get("affixMgr", []),
        },
        "attrNames": attr_names,
        "calibMs": int(CALIB * 1000),
        "Unit": offs("ent.Unit", kind="kind", statuses="statuses", attr="attr",
                     affixes="affixes"),
        "UnitAttributes": offs("ent.UnitAttributes", attributes="attributes"),
        "AffixManager": offs("ent.AffixManager", cache="cache",
                             appsByUid="appsByUid"),
        # Where any stat modification in this game actually lives. If the
        # abundance elixir grants anything at all, a row appears here.
        "AffixApplication": offs("ent.AffixApplication", kind="kind",
                                 baseVal="baseVal", source="source",
                                 target="target", target2="target2",
                                 instigator="instigator", uid="uid"),
        "Status": offs("st.skill.Status", kind="kind", stacks="stacks",
                       duration="duration", originItem="originItem"),
        "Item": offs("st.Item", kind="kind"),
        "ArrayProxyData": offs("hxbit.ArrayProxyData", array="array"),
        "ArrayDyn": offs("hl.types.ArrayDyn", array="array"),
        "natives": {"hikeys": native("hikeys"), "higet": native("higet")},
    }

    for grp, need in (("Unit", 3), ("UnitAttributes", 1), ("AffixManager", 1),
                      ("AffixApplication", 4), ("Status", 2),
                      ("ArrayProxyData", 1), ("ArrayDyn", 1)):
        if len(p[grp]) < need:
            raise SystemExit(f"[!] offsets for {grp} did not fully resolve "
                             f"({p[grp]}) - aborting rather than guessing.")
    if p["fn"]["postUpdate"] is None:
        raise SystemExit("[!] client.BaseCamera.postUpdate not found - no "
                         "game-thread anchor; aborting.")
    if not roots:
        raise SystemExit("[!] no snapshot roots resolved - aborting.")
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
    nf = sum(len(v) for v in p["fields"].values())
    print(f"[*] {nf} fields across {len(p['roots'])} objects: "
          + ", ".join(r[0] for r in p["roots"]), flush=True)
    print(f"[*] AffixApplication@{p['AffixApplication']}  "
          f"AffixManager.cache@{p['AffixManager'].get('cache')}  "
          f"attributes@{p['UnitAttributes'].get('attributes')}  "
          f"{len(p['attrNames'])} attribute names  "
          f"map natives={p['natives']}", flush=True)

    data = (HERE.parent / "analysis_out" / "resolver_data.json").read_text(encoding="utf-8")
    off = (HERE.parent / "analysis_out" / "meter_offsets.json").read_text(encoding="utf-8")
    js = (HERE / "elixir_probe.js").read_text(encoding="utf-8")
    src = (f"const DATA = {data};\nconst OFF = {off};\n"
           f"const P = {json.dumps(p)};\n" + js)

    session = frida.attach("Farever.exe")
    script = session.create_script(src)
    script.on("message", on_message)
    script.load()
    print(f"[*] attached, running for {DURATION:.0f}s. WAIT for 'PROBE ARMED', "
          f"then STAND STILL for the {CALIB:.0f}s calibration, then follow the "
          "shopping list in this file's docstring. Ctrl+C to stop early.",
          flush=True)
    try:
        time.sleep(DURATION)
    except KeyboardInterrupt:
        print("\n[*] stopping early.")
    finally:
        # unload/detach can wedge on this game, and killing this process with
        # live hooks is the known game-crasher. If cleanup hangs we say so and
        # WAIT — the session dies with the game, never the other way round.
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
            print("[!] unload is wedged. Do NOT kill this process — quit the "
                  "game via its own UI when convenient and this will exit on "
                  "its own.", flush=True)
            t.join()
    print("[done]")


if __name__ == "__main__":
    main()
