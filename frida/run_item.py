"""Host for item_probe.js — the item-pickup spike.

    py frida\\run_item.py [seconds] [path\\to\\hlboot.dat]

Attaches to a running Farever and hooks the candidate pickup paths: the
ItemGain notification toast, the Loadout/Player state layer, and the ground
pickup RPC. Pick items up — junk, gear, and ideally something legendary — and
each event prints with the item's kind and rarity string. Buy something from a
vendor too, so gains that are NOT pickups can be told apart.

Prints a SUMMARY at the end: which hooks fired, and every distinct
kind/rarity seen with the hooks that saw it.

Like run_boss.py, every findex and offset is resolved by NAME from hlboot.dat
at launch — a stale hardcoded findex would hook some unrelated function, which
is a crash in a live game rather than a bad reading.
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
    """Resolve every findex and offset the probe needs, by name."""
    code = HLCode(find_hlboot(argv_index=2)).parse()
    assert_resolver_current(code)   # stale analysis_out reads as 'found nothing'
    byname = {t.name: t for t in code.types
              if t.kind in (HOBJ, HSTRUCT) and t.name}

    def proto(cls, meth):
        t = byname.get(cls)
        if not t:
            return None
        return next((p.findex for p in t.protos if p.name == meth), None)

    def offs(cls, *fields):
        t = byname.get(cls)
        if not t:
            return {}
        o = code.field_offsets(t.index)
        return {f: o[f][0] for f in fields if f in o}

    fn = {
        "NotifyManager.queue": proto("ui.notify.NotifyManager", "queue"),
        "NotifyManager.show": proto("ui.notify.NotifyManager", "show"),
        "NotifyManager.getNotify": proto("ui.notify.NotifyManager", "getNotify"),
        "ItemGain.init": proto("ui.notify.ItemGain", "init"),
        "ItemGain.getItemFormat": proto("ui.notify.ItemGain", "getItemFormat"),
        "SideNotify.init": proto("ui.notify.SideNotify", "init"),
        "Loadout.gainItem": proto("st.Loadout", "gainItem"),
        "Loadout.onGainItems": proto("st.Loadout", "onGainItems"),
        "Player.notifyItemLooted": proto("st.Player", "notifyItemLooted"),
        "Player.makeItemPickup": proto("st.Player", "makeItemPickup"),
        "Hero.addInventoryDrop": proto("ent.Hero", "addInventoryDrop"),
        "SkillObject.rpcDoPickup": proto("st.skill.SkillObject", "rpcDoPickup"),
        "HeroData.addLootEntry": proto("st.player.HeroData", "addLootEntry"),
        "Progress.incrementItemLoot": proto("st.player.Progress",
                                            "incrementItemLoot"),
    }
    i = {
        "fn": fn,
        "ItemGain": offs("ui.notify.ItemGain", "kind", "args", "item", "count"),
        "Item": offs("st.Item", "kind", "inf"),
        # Measured: `rarity` is declared on st.item.Weapon and NOWHERE else in
        # the runtime item hierarchy (Item -> Gear -> Armor/Weapon; Armor has
        # no rarity string). The probe therefore reads it only off objects
        # whose class is exactly st.item.Weapon — on anything else offset 160
        # is past the end of the object.
        "Weapon": offs("st.item.Weapon", "rarity", "level", "upgradeLevel"),
    }

    if fn["ItemGain.init"] is None and fn["NotifyManager.queue"] is None:
        raise SystemExit("[!] neither ui.notify.ItemGain.init nor "
                         "NotifyManager.queue found - the notify UI was "
                         "renamed; re-run discovery.")
    if not i["ItemGain"] or not i["Item"] or not i["Weapon"]:
        raise SystemExit("[!] ItemGain/Item/Weapon offsets did not resolve - "
                         "aborting rather than reading guessed addresses.")
    missing = [k for k, v in fn.items() if v is None]
    if missing:
        print(f"[!] not found (skipped): {', '.join(missing)}")
    return i


def on_message(message, data):
    if message["type"] == "error":
        print("[JS ERROR]", message.get("description"))
        return
    p = message.get("payload") or {}
    if p.get("kind") == "log":
        # Console is cp1252; game text can be anything.
        print(str(p["msg"]).encode("ascii", "replace").decode(), flush=True)


def main():
    i = build_targets()
    print(f"[*] ItemGain.item@{i['ItemGain'].get('item')} "
          f"count@{i['ItemGain'].get('count')}  Item.kind@{i['Item'].get('kind')}  "
          f"Weapon.rarity@{i['Weapon'].get('rarity')}")

    data = (HERE.parent / "analysis_out" / "resolver_data.json").read_text(encoding="utf-8")
    off = (HERE.parent / "analysis_out" / "meter_offsets.json").read_text(encoding="utf-8")
    js = (HERE / "item_probe.js").read_text(encoding="utf-8")
    src = (f"const DATA = {data};\nconst OFF = {off};\n"
           f"const I = {json.dumps(i)};\n" + js)

    session = frida.attach("Farever.exe")
    script = session.create_script(src)
    script.on("message", on_message)
    script.load()
    print(f"[*] probing for {DURATION:.0f}s - go pick things up. "
          "Ctrl+C to stop early and still get the summary.")
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
        try:
            script.unload()
            session.detach()
        except Exception:
            pass
    print("[done]")


if __name__ == "__main__":
    main()
