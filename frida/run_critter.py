"""Host for critter_probe.js — the critter-collection spike.

    py frida\\run_critter.py [seconds] [path\\to\\hlboot.dat]

Attaches to a running Farever and answers what static analysis cannot: which
store the "already collected" state actually lives in, and what its keys are.

    st.player.Collection.pets : hxbit.ArrayProxyData  (account, mounts sibling)
    st.player.Progress.pets   : hxbit.MapData         (per char, codex sibling)

The in-game shopping list:

  1. Just stand there. The rest dump prints both stores — the array's element
     type (String unit kind vs item id) and the map's keys + value class.
  2. Net a critter you ALREADY have. hasPet's argument + result path shows
     how the game itself checks "collected", and whether a dupe even calls
     addPet.
  3. Net a critter you DON'T have. Both stores get re-dumped on the length
     edge; whichever one moved is the one the filter should read.

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

    def native(name):
        return next((n.findex for n in code.natives
                     if n.lib == "std" and n.name == name), None)

    p = {
        "fn": {"postUpdate": proto("client.BaseCamera", "postUpdate")},
        # Everything the probe hooks, resolved by name so a patch can't feed
        # it stale findices. Log-only, remember — nothing here is called.
        "hooks": {
            "Hero.tryCaptureCritter":      proto("ent.Hero", "tryCaptureCritter"),
            "Hero.notifyCapture__impl":    proto("ent.Hero", "notifyCapture__impl"),
            "Hero.notifyCaptureMiss__impl":
                proto("ent.Hero", "notifyCaptureMiss__impl"),
            "Collection.hasPet":           proto("st.player.Collection", "hasPet"),
            "Collection.addPet":           proto("st.player.Collection", "addPet"),
            "Collection.equipPet":         proto("st.player.Collection", "equipPet"),
            "Collection.implEquipPet":
                proto("st.player.Collection", "implEquipPet"),
            "Progress.set_pets":           proto("st.player.Progress", "set_pets"),
        },
        "Hero": offs("ent.Hero", player="player", name="name"),
        "Unit": offs("ent.Unit", kind="kind"),
        "Player": offs("st.Player", accountProgress="accountProgress",
                       progress="progress", name="name", isMe="isMe"),
        "AccountProgress": offs("st.player.AccountProgress",
                                collection="collection"),
        "Collection": offs("st.player.Collection", mounts="mounts",
                           pets="pets"),
        "Progress": offs("st.player.Progress", pets="pets"),
        "ArrayProxyData": offs("hxbit.ArrayProxyData", array="array"),
        "ArrayDyn": offs("hl.types.ArrayDyn", array="array"),
        "MapData": offs("hxbit.MapData", map="map"),
        "StringMap": offs("haxe.ds.StringMap", h="h"),
        "Item": offs("st.Item", kind="kind"),
        "natives": {"hbkeys": native("hbkeys"), "hbget": native("hbget")},
    }
    for grp, need in (("Hero", 2), ("Player", 4), ("AccountProgress", 1),
                      ("Collection", 2), ("Progress", 1),
                      ("ArrayProxyData", 1), ("ArrayDyn", 1), ("MapData", 1),
                      ("StringMap", 1), ("Item", 1), ("Unit", 1)):
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
    print(f"[*] Collection.pets@{p['Collection']['pets']}  "
          f"Progress.pets@{p['Progress']['pets']}  "
          f"map natives={p['natives']}", flush=True)

    data = (HERE.parent / "analysis_out" / "resolver_data.json").read_text(encoding="utf-8")
    off = (HERE.parent / "analysis_out" / "meter_offsets.json").read_text(encoding="utf-8")
    js = (HERE / "critter_probe.js").read_text(encoding="utf-8")
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
