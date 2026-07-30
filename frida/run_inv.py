"""Host for inv_probe.js — the inventory-watcher spike.

    py frida\\run_inv.py [seconds] [path\\to\\hlboot.dat]

Attaches to a running Farever and polls the local hero's replicated loadout:
inventory content, equipment content, and weaponInHand. Every item add/remove
prints with class, kind and — for weapons — the live rarity string. This is
the round-4 follow-up to run_item.py, which established that item gains never
pass through client-side functions (they arrive as hxbit state replication),
so watching the state is the only client-side way to see them.

Offsets are resolved by NAME from hlboot.dat at launch, as always.
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
    # The findices below come from this parse; `anchors` and `funcs` come from
    # the generated file. Mixing a fresh half with a post-patch stale half is
    # what made this probe log nothing for a full run — see datafresh.py.
    assert_resolver_current(code)
    byname = {t.name: t for t in code.types
              if t.kind in (HOBJ, HSTRUCT) and t.name}

    def offs(cls, **want):
        """{out_name: offset} for fields of `cls`, given out_name=field_name."""
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
        # The game-thread anchor: HL calls (the getHero lookup) are legal only
        # inside a hook on a game-thread function. postUpdate is the same
        # anchor the shipping meter borrows for its CDB name lookups.
        "fn": {"postUpdate": proto("client.BaseCamera", "postUpdate")},
        "Hero": offs("ent.Hero", loadout="loadout", weaponInHand="weaponInHand"),
        "Loadout": offs("st.Loadout", inventory="inventory",
                        equipment="equipment"),
        # st.Equipment extends st.Inventory, so `content` resolves through the
        # super chain and one offset serves both containers.
        "Inventory": offs("st.Inventory", content="content"),
        "Item": offs("st.Item", kind="kind", uid="__uid"),
        "Weapon": offs("st.item.Weapon", rarity="rarity", level="level"),
    }
    for grp, need in (("Hero", 2), ("Loadout", 2), ("Inventory", 1),
                      ("Item", 2), ("Weapon", 2)):
        if len(p[grp]) < need:
            raise SystemExit(f"[!] offsets for {grp} did not fully resolve "
                             f"({p[grp]}) - aborting rather than guessing.")
    if p["fn"]["postUpdate"] is None:
        raise SystemExit("[!] client.BaseCamera.postUpdate not found - no "
                         "game-thread anchor for the hero lookup; aborting.")
    # Equipment must actually inherit content from st.Inventory, or its sweep
    # would read a wrong offset — verify, don't assume.
    eq = offs("st.Equipment", content="content")
    if eq.get("content") != p["Inventory"]["content"]:
        raise SystemExit(f"[!] st.Equipment.content@{eq.get('content')} != "
                         f"st.Inventory.content@{p['Inventory']['content']} - "
                         "the containers differ; fix the probe.")
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
    print(f"[*] Hero.loadout@{p['Hero']['loadout']} "
          f"weaponInHand@{p['Hero']['weaponInHand']}  "
          f"Loadout.inventory@{p['Loadout']['inventory']} "
          f"equipment@{p['Loadout']['equipment']}  "
          f"Inventory.content@{p['Inventory']['content']}  "
          f"Item.uid@{p['Item']['uid']}  Weapon.rarity@{p['Weapon']['rarity']}",
          flush=True)

    data = (HERE.parent / "analysis_out" / "resolver_data.json").read_text(encoding="utf-8")
    off = (HERE.parent / "analysis_out" / "meter_offsets.json").read_text(encoding="utf-8")
    js = (HERE / "inv_probe.js").read_text(encoding="utf-8")
    src = (f"const DATA = {data};\nconst OFF = {off};\n"
           f"const P = {json.dumps(p)};\n" + js)

    session = frida.attach("Farever.exe")
    script = session.create_script(src)
    script.on("message", on_message)
    script.load()
    print(f"[*] attached, running for {DURATION:.0f}s. WAIT for the "
          "'PROBE ARMED' line before touching items - until the hero latches, "
          "nothing is being watched. Ctrl+C to stop early.", flush=True)
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
