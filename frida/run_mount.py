"""Host for mount_probe.js — the mount-discovery spike.

    py frida\\run_mount.py [seconds] [path\\to\\hlboot.dat]

Attaches to a running Farever and answers three things:
  * the unlocked-mount list (hero -> player -> accountProgress -> collection
    -> mounts), typed at every hop on the first walk;
  * which function fires when the selected mount changes (the setMount /
    set_mountId family, ui.Console.equipMount — all hooked, args logged),
    with ent.Hero.mountId watched for edges as ground truth;
  * what marks mounting and dismounting (ent.Unit.mountUnit / leaveVehicle /
    onVehicleChange — the SkillKind vocabulary says dismount is Exit_Vehicle).

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
            "Hero.setMount":           proto("ent.Hero", "setMount"),
            "Hero.setMount__impl":     proto("ent.Hero", "setMount__impl"),
            "Hero.assignMount":        proto("ent.Hero", "assignMount"),
            "Hero.setupMount":         proto("ent.Hero", "setupMount"),
            "Hero.set_mountId":        proto("ent.Hero", "set_mountId"),
            "Hero.onVehicleChange":    proto("ent.Hero", "onVehicleChange"),
            "updateMount":             proto("ent.Hero", "updateMount"),
            "Unit.mountUnit":          proto("ent.Unit", "mountUnit"),
            "Unit.requestMountUnit__impl":
                proto("ent.Unit", "requestMountUnit__impl"),
            "Unit.leaveVehicle":       proto("ent.Unit", "leaveVehicle"),
            "Console.equipMount":      proto("ui.Console", "equipMount"),
            "Collection.equipItem":    proto("st.player.Collection", "equipItem"),
            "Collection.implEquipItem":
                proto("st.player.Collection", "implEquipItem"),
            "Collection.addItem":      proto("st.player.Collection", "addItem"),
            "SkillMount.onStartStep":  proto("st.skill.Mount", "onStartStep"),
            "UnitView.setMount":       proto("client.UnitView", "setMount"),
            "updateMountAnim":         proto("client.UnitView", "updateMountAnim"),
            "controlMount":            proto("client.PlayerController",
                                             "controlMount"),
        },
        "Hero": offs("ent.Hero", player="player", mountId="mountId",
                     name="name"),
        # ent.Unit has no name field — hero names are ent.Hero.name and foe
        # names come from the CDB — so kind is the id an arg dump can show.
        "Unit": offs("ent.Unit", kind="kind"),
        "Player": offs("st.Player", accountProgress="accountProgress",
                       name="name", isMe="isMe"),
        "AccountProgress": offs("st.player.AccountProgress",
                                collection="collection"),
        "Collection": offs("st.player.Collection", mounts="mounts",
                           gliders="gliders", pets="pets"),
        "ArrayProxyData": offs("hxbit.ArrayProxyData", array="array"),
        "ArrayDyn": offs("hl.types.ArrayDyn", array="array"),
        "Item": offs("st.Item", kind="kind", uid="__uid"),
    }
    for grp, need in (("Hero", 3), ("Player", 3), ("AccountProgress", 1),
                      ("Collection", 3), ("ArrayProxyData", 1),
                      ("ArrayDyn", 1), ("Item", 2), ("Unit", 1)):
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
          f"mountId@{p['Hero']['mountId']}  "
          f"Player.accountProgress@{p['Player']['accountProgress']}  "
          f"AccountProgress.collection@{p['AccountProgress']['collection']}  "
          f"Collection.mounts@{p['Collection']['mounts']}", flush=True)

    data = (HERE.parent / "analysis_out" / "resolver_data.json").read_text(encoding="utf-8")
    off = (HERE.parent / "analysis_out" / "meter_offsets.json").read_text(encoding="utf-8")
    js = (HERE / "mount_probe.js").read_text(encoding="utf-8")
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
