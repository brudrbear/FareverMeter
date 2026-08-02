"""Host for glider_probe.js — the glider-discovery spike.

    py frida\\run_glider.py [seconds] [path\\to\\hlboot.dat]

Round 2 (round 1 proved the deploy chain carries no glider id and the UI
equip is Collection.equipItem(kind, item, 65535), same as mounts). Attaches
to a running Farever and answers:
  * is the equipped glider item visible client-side in
    hero.loadout.equipment.content (walked at arm, watched for the equip
    edge);
  * where the view resolves which glider MODEL to show — per deploy
    (UnitView.toggleGlider -> displayGearSlot / spawnItemModelInSetup each
    open) or once at gear-build time (updateGear at spawn/equip only). Every
    gear-display function is hooked; lines mentioning "Glider" always print;
  * whether the transmog path (Hero.getGearOverride / refreshGear) runs at
    deploy time.

Everything is resolved by NAME from hlboot.dat at launch. Hooks log only.
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

    def offs(cls, **want):
        t = byname.get(cls)
        if not t:
            return {}
        o = code.field_offsets(t.index)
        return {out: o[f][0] for out, f in want.items() if f in o}

    def proto(cls, meth):
        t = byname.get(cls)
        if not t:
            return None
        return next((x.findex for x in t.protos if x.name == meth), None)

    p = {
        "fn": {"postUpdate": proto("client.BaseCamera", "postUpdate")},
        # Everything the probe hooks, resolved by name so a patch can't feed
        # it stale findices. Log-only, remember — nothing here is called.
        "hooks": {
            # Deploy edge (round 1: bools only, but they timestamp the glide
            # against the gear-display calls below).
            "Hero.toggleGlide":        proto("ent.Hero", "toggleGlide"),
            "UnitView.toggleGlider":   proto("client.UnitView", "toggleGlider"),
            # The gear-display family — which of these resolves the glider
            # model, and when?
            "UnitView.updateGear":     proto("client.UnitView", "updateGear"),
            "UnitView.displayGear":    proto("client.UnitView", "displayGear"),
            "UnitView.displayGearSlot":
                proto("client.UnitView", "displayGearSlot"),
            "UnitView.getSlotItemDisplayed":
                proto("client.UnitView", "getSlotItemDisplayed"),
            "UnitView.applyItemGearProps":
                proto("client.UnitView", "applyItemGearProps"),
            "UnitView.loadGearProps":  proto("client.UnitView", "loadGearProps"),
            "UnitView.spawnItemModelInSetup":
                proto("client.UnitView", "spawnItemModelInSetup"),
            "UnitView.attachItemModelInSetup":
                proto("client.UnitView", "attachItemModelInSetup"),
            "UnitView.setGearSlotVisiblity":
                proto("client.UnitView", "setGearSlotVisiblity"),
            "UnitView.displaySkin":    proto("client.UnitView", "displaySkin"),
            # The transmog path — an alternative swap point if it runs at
            # deploy time.
            "Hero.getGearOverride":    proto("ent.Hero", "getGearOverride"),
            "Hero.refreshGear":        proto("ent.Hero", "refreshGear"),
            "Hero.refreshGear__impl":  proto("ent.Hero", "refreshGear__impl"),
            # The equip path (round 1: fires with the glider kind).
            "Collection.equipItem":    proto("st.player.Collection", "equipItem"),
            "Collection.implEquipItem":
                proto("st.player.Collection", "implEquipItem"),
            "Equipment.doEquipItem":   proto("st.Equipment", "doEquipItem"),
            "Equipment.refreshItem":   proto("st.Equipment", "refreshItem"),
            # Counter-only in the JS (hot): movement predicates + model loads.
            "loadModel":               proto("client.UnitView", "loadModel"),
            "addModel":                proto("client.UnitView", "addModel"),
            "isInGlidePush":           proto("ent.Hero", "isInGlidePush"),
        },
        "Hero": offs("ent.Hero", player="player", gliding="gliding",
                     name="name", loadout="loadout"),
        "Unit": offs("ent.Unit", kind="kind"),
        "Player": offs("st.Player", accountProgress="accountProgress",
                       name="name", isMe="isMe"),
        "AccountProgress": offs("st.player.AccountProgress",
                                collection="collection"),
        "Collection": offs("st.player.Collection", gliders="gliders"),
        "Loadout": offs("st.Loadout", equipment="equipment"),
        "Equipment": offs("st.Equipment", content="content"),
        "ArrayProxyData": offs("hxbit.ArrayProxyData", array="array"),
        "ArrayDyn": offs("hl.types.ArrayDyn", array="array"),
        "Item": offs("st.Item", kind="kind", uid="__uid"),
    }
    for grp, need in (("Hero", 4), ("Player", 3), ("AccountProgress", 1),
                      ("Collection", 1), ("Loadout", 1), ("Equipment", 1),
                      ("ArrayProxyData", 1), ("ArrayDyn", 1), ("Item", 2),
                      ("Unit", 1)):
        if len(p[grp]) < need:
            raise SystemExit(f"[!] offsets for {grp} did not fully resolve "
                             f"({p[grp]}) - aborting rather than guessing.")
    if p["fn"]["postUpdate"] is None:
        raise SystemExit("[!] client.BaseCamera.postUpdate not found - no "
                         "game-thread anchor; aborting.")
    missing = [k for k, v in p["hooks"].items() if v is None]
    if missing:
        # Named, not fatal: a renamed method should cost that hook, not the run.
        print(f"[!] unresolved hooks (skipped): {missing}", flush=True)
    return p


def on_message(message, data):
    if message["type"] == "error":
        print("[JS ERROR]", message.get("description"))
        return
    p = message.get("payload") or {}
    if p.get("kind") == "log":
        print(str(p["msg"]).encode("ascii", "replace").decode(), flush=True)


def main():
    p = build_targets()
    print(f"[*] Hero.player@{p['Hero']['player']} "
          f"gliding@{p['Hero']['gliding']} "
          f"loadout@{p['Hero']['loadout']}  "
          f"Loadout.equipment@{p['Loadout']['equipment']}  "
          f"Equipment.content@{p['Equipment']['content']}  "
          f"Collection.gliders@{p['Collection']['gliders']}", flush=True)

    data = (HERE.parent / "analysis_out" / "resolver_data.json").read_text(encoding="utf-8")
    off = (HERE.parent / "analysis_out" / "meter_offsets.json").read_text(encoding="utf-8")
    js = (HERE / "glider_probe.js").read_text(encoding="utf-8")
    src = (f"const DATA = {data};\nconst OFF = {off};\n"
           f"const P = {json.dumps(p)};\n" + js)

    session = frida.attach("Farever.exe")
    script = session.create_script(src)
    script.on("message", on_message)
    script.load()
    print(f"[*] attached, running for {DURATION:.0f}s. WAIT for the "
          "'PROBE ARMED' line before doing anything in game. Ctrl+C to stop "
          "early.", flush=True)
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
