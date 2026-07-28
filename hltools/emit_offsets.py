"""
emit_offsets.py — compute every runtime field offset the meter needs and write
analysis_out/meter_offsets.json. Re-run after a Farever patch.

Offsets come from hlbc_parser.field_offsets() (mirrors hl_get_obj_rt) and the
HL virtual vfield indices for the skill-name chain.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from hlbc_parser import HLCode, HOBJ, HSTRUCT, HVIRTUAL
from gamepath import find_hlboot

# Overridable for the same reason as build_targets.py's: the installed meter
# runs this from a bundle directory that doesn't survive the process.
_OUT_DIR = Path(os.environ.get("FAREVER_ANALYSIS_OUT")
                or Path(__file__).resolve().parent.parent / "analysis_out")
_OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT = _OUT_DIR / "meter_offsets.json"


def main():
    hlboot = find_hlboot()
    print(f"[*] parsing {hlboot}")
    code = HLCode(hlboot).parse()
    byname = {t.name: t for t in code.types
              if t.kind in (HOBJ, HSTRUCT) and t.name}

    def offs(name):
        return code.field_offsets(byname[name].index)

    dr = offs("st.skill.DamageResult")
    hd = offs("st.skill.HitData")
    ua = offs("ent.UnitAttributes")
    hero = offs("ent.Hero")
    layer = offs("st.GameLayer")
    tstate = offs("TimeState")
    wevents = offs("st.event.WorldEvents")
    wevent = offs("st.event.WorldEvent")
    player = offs("st.Player")
    group = offs("st.Group")
    base = offs("st.skill.BaseSkill")
    string = offs("String")
    entity = offs("ent.Entity")
    state = offs("st.State")
    inter = offs("ent.Interactible")
    activity = offs("st.Activity")
    arrobj = offs("hl.types.ArrayObj")
    foe = offs("ent.Foe")
    unit = offs("ent.Unit")
    elem = offs("ent.Element")
    cam = offs("client.BaseCamera")

    # skill display-name chain: BaseSkill.inf (virtual #963) -> texts (#973) -> name
    row = code.types[963]
    texts_ti = next(f.type_index for f in row.vfields if f.name == "texts")
    texts = code.types[texts_ti]
    vidx = lambda vt, nm: next(i for i, f in enumerate(vt.vfields) if f.name == nm)

    meta = {
        "String": {"bytes": string["bytes"][0], "length": string["length"][0]},
        "DamageResult": {k: dr[k][0] for k in
            ["_amount", "affinity", "_critical", "_kill", "_hitCount",
             "_block", "target", "serverSource", "ctx", "baseSkill"]},
        "BaseSkill": {"kind": base["kind"][0], "inf": base["inf"][0],
                      "owner": base["owner"][0],
                      "ownerPlayer": base["ownerPlayer"][0]},
        "HitData": {"baseSkill": hd["baseSkill"][0]},
        "UnitAttributes": {"unit": ua["unit"][0], "health": ua["health"][0]},
        # Hero.layer is st.State.layer, inherited — it points at the GameLayer
        # the hero is in, which is how the hook reaches the rift flag without
        # calling anything (a plain pointer walk is safe off the game thread).
        "Hero": {"name": hero["name"][0], "player": hero["player"][0],
                 "isInCombat": hero["isInCombat"][0],
                 "layer": hero["layer"][0]},
        "GameLayer": {"isRift": layer["isRift"][0],
                      "mainActivity": layer["mainActivity"][0],
                      "worldEvents": layer["worldEvents"][0],
                      "time": layer["_time"][0],
                      # Minimap: the layer keeps these lists built already, so
                      # the sweep is a walk of three arrays rather than a
                      # search. units = heroes + foes, interactibles = chests /
                      # orbs / obelisks / respawn points, entities = the widest
                      # net and the only place activities show up.
                      "units": layer["units"][0],
                      "interactibles": layer["interactibles"][0],
                      "entities": layer["entities"][0]},
        # Every drawable thing descends from ent.Entity, so one set of position
        # offsets serves heroes, foes, interactibles and activities alike.
        # rotationZ is radians (measured: observed values span ~2*pi).
        "Entity": {"posx": entity["posx"][0], "posy": entity["posy"][0],
                   "posz": entity["posz"][0],
                   "rotationZ": entity["rotationZ"][0],
                   "radius": entity["radius"][0]},
        # Despawned-but-still-listed entries. Filtered out of the sweep.
        "State": {"removed": state["removed"][0]},
        # `enabled` is reported per interactible rather than filtered on, so
        # whether a looted chest flips this flag or leaves the array entirely
        # stays a display decision instead of an assumption baked into the hook.
        "Interactible": {"enabled": inter["enabled"][0],
                         "isOffScreen": inter["isOffScreen"][0]},
        "Activity": {"kind": activity["kind"][0]},
        # Every placed world object is an ent.Element. `kind` is its id
        # ("Z1_World_Greenlands_WorldChest_60", "RedOrb_World_140") and
        # `stateId` its state machine — measured: chests read Closed or Locked,
        # obelisks Closed, orbs Enabled. currentVisualState is NOT the same
        # thing: it reads "Opened" on chests that are plainly shut.
        "Element": {"kind": elem["kind"][0], "stateId": elem["stateId"][0],
                    "currentVisualState": elem["currentVisualState"][0]},
        # curDirection is the camera yaw actually being rendered; `direction`
        # is the value it is easing towards. Following the eased one would make
        # the minimap lead the view it is supposed to match.
        "Camera": {"direction": cam["direction"][0],
                   "curDirection": cam["curDirection"][0],
                   "distance": cam["distance"][0]},
        # A foe with a summonOwner is somebody's pet, not a mob. That's the
        # only reliable way to tell them apart — they're the same class.
        # `kind` is the internal id ("Crimson_Z2W_Sword"); `inf` is the CDB row
        # it came from, whose texts.name is the display name on the nameplate.
        "Unit": {"kind": unit["kind"][0], "inf": unit["inf"][0]},
        "Foe": {"summonOwner": foe["summonOwner"][0],
                "persistantSummon": foe["persistantSummon"][0]},
        # hl.types.ArrayObj: length, then a pointer to an hl_varray whose
        # ELEMENTS START AT +24, past its (t, at, size, pad) header. Reading
        # from +0 yields the header as your first entity and faults instantly.
        "ArrayObj": {"length": arrobj["length"][0], "array": arrobj["array"][0],
                     "data": 24},
        # Rift countdown: worldEvents.currentEvents is an hxbit proxy array
        # (same shape as Group.players), holding st.event.WorldEvent objects.
        # startTime and serverNow share the server clock.
        "TimeState": {"serverNow": tstate["serverNow"][0],
                      "serverStart": tstate["serverStart"][0]},
        "WorldEvents": {"currentEvents": wevents["currentEvents"][0]},
        "WorldEvent": {"kind": wevent["kind"][0],
                       "creationTime": wevent["creationTime"][0],
                       "startTime": wevent["startTime"][0],
                       "stopTime": wevent["stopTime"][0]},
        "Player": {"name": player["name"][0], "group": player["group"][0],
                   "isMe": player["isMe"][0], "lobbyId": player["lobbyId"][0]},
        "Group": {"groupId": group["groupId"][0], "players": group["players"][0]},
        # HL virtual field indices for the skill display name
        "SkillRow": {"id_vidx": vidx(row, "id"), "texts_vidx": vidx(row, "texts")},
        "Texts": {"name_vidx": vidx(texts, "name"), "desc_vidx": vidx(texts, "desc")},
    }
    OUT.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"[written] {OUT}")
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
