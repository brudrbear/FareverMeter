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

    def descendants(root):
        """Every class that inherits from `root`, itself included.

        Needed because a summon's runtime class is not always the literal
        `ent.Foe` — the hook has to recognise the whole family before it dares
        read `summonOwner` off a dealer, since that offset means something
        entirely different on a class that hasn't got the field."""
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
    step = offs("st.skill.SkillStep")
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
    bosses = offs("ui.hud.BossesInfo")
    bossinfo = offs("ui.hud.BossInfo")
    loadout = offs("st.Loadout")
    inv = offs("st.Inventory")
    equip = offs("st.Equipment")
    item = offs("st.Item")
    weapon = offs("st.item.Weapon")
    world = offs("world.World")
    gath = offs("ent.interactible.Gatherable")
    acct = offs("st.player.AccountProgress")
    coll = offs("st.player.Collection")
    aproxy = offs("hxbit.ArrayProxyData")
    adyn = offs("hl.types.ArrayDyn")

    # st.Equipment extends st.Inventory, so one `content` offset serves both
    # containers. Verified rather than assumed — if the two ever diverge, the
    # equipment sweep would read a wrong offset and report nonsense items.
    if equip["content"][0] != inv["content"][0]:
        raise SystemExit(
            f"[!] st.Equipment.content@{equip['content'][0]} != "
            f"st.Inventory.content@{inv['content'][0]} — the containers no "
            "longer share a layout; fix the inventory sweep before shipping.")

    # skill display-name chain: BaseSkill.inf (virtual #963) -> texts (#973) -> name
    row = code.types[963]
    texts_ti = next(f.type_index for f in row.vfields if f.name == "texts")
    texts = code.types[texts_ti]
    vidx = lambda vt, nm: next(i for i, f in enumerate(vt.vfields) if f.name == nm)

    meta = {
        "String": {"bytes": string["bytes"][0], "length": string["length"][0]},
        # `blocker` and `effect` are read for the nullified-hit diagnostic: the
        # meter counts a hit's _amount whether or not the target actually took
        # it, so damage against a boss in an immunity phase inflates the parse.
        # Which of _block / blocker / effect marks that is not settled yet —
        # these ship so the hook can report them from normal play instead of
        # needing a probe session timed to an immune phase.
        "DamageResult": {k: dr[k][0] for k in
            ["_amount", "affinity", "_critical", "_kill", "_hitCount",
             "_block", "blocker", "effect", "target", "serverSource", "ctx",
             "baseSkill"]},
        # dynVal1-3 are how MOST player heal skills carry their amount: the
        # cdb's skill@steps@effects rows name `dynVal` rather than a baseVal or
        # a scaling ratio, and these three f64s are hxbit-replicated, so the
        # number the server computed is sitting here on the client. That is the
        # only reason healing can be counted at all — nothing else on a client
        # knows what a heal was worth (TESTING.md, "Healing is estimated").
        "BaseSkill": {"kind": base["kind"][0], "inf": base["inf"][0],
                      "owner": base["owner"][0],
                      "ownerPlayer": base["ownerPlayer"][0],
                      "dynVal1": base["dynVal1"][0],
                      "dynVal2": base["dynVal2"][0],
                      "dynVal3": base["dynVal3"][0]},
        "HitData": {"baseSkill": hd["baseSkill"][0],
                    "step": hd["step"][0]},
        "SkillStep": {"index": step["index"][0]},
        # The rest of the heal skills scale off one of the caster's primary
        # attributes (Faith on 17 of them, then Intellect/Strength/Dexterity).
        "UnitAttributes": {"unit": ua["unit"][0], "health": ua["health"][0],
                           "faith": ua["faith"][0],
                           "intellect": ua["intellect"][0],
                           "strength": ua["strength"][0],
                           "dexterity": ua["dexterity"][0]},
        # Hero.layer is st.State.layer, inherited — it points at the GameLayer
        # the hero is in, which is how the hook reaches the rift flag without
        # calling anything (a plain pointer walk is safe off the game thread).
        "Hero": {"name": hero["name"][0], "player": hero["player"][0],
                 "isInCombat": hero["isInCombat"][0],
                 "layer": hero["layer"][0],
                 # Legendary-pickup cue: the hero's containers, plus the
                 # equipped weapon (which is the same st.item.Weapon pointer
                 # the equipment slot holds — measured).
                 "loadout": hero["loadout"][0],
                 "weaponInHand": hero["weaponInHand"][0],
                 # The live summoned mount (null while dismounted) — the
                 # random-mount hook logs it; the swap itself rides setMount.
                 "mountId": hero["mountId"][0],
                 # The class ("Warrior"/"Priest"/"Rogue"/"Mage") and level, for
                 # the Social tab's roster. st.player.HeroData would be the
                 # tidier home for both, but it is null on the client for every
                 # player including yourself — the entity is the only source.
                 "kind": hero["kind"][0], "level": hero["_level"][0]},
        "Loadout": {"inventory": loadout["inventory"][0],
                    "equipment": loadout["equipment"][0]},
        # content is an ArrayObj of SLOT VIRTUALS, not of items: each entry is
        # a standalone hl vvirtual carrying inline {count:Int, item:st.Item}.
        # The hook reads the `item` field by name out of the virtual's own
        # field table — see readSlot() in meter_hook.js. Decoding entries as
        # st.Item directly does not throw, it just yields garbage.
        "Inventory": {"content": inv["content"][0]},
        # __uid is NOT stable identity — it is reassigned on every container
        # move, so a uid-diff alone reports a re-equip as a fresh pickup. It is
        # emitted because it is still the only per-slot discriminator; the
        # pickup rule guards on `kind` as well.
        "Item": {"kind": item["kind"][0], "uid": item["__uid"][0]},
        # `rarity` is declared ONLY on st.item.Weapon (hierarchy:
        # st.Item -> st.item.Gear -> st.item.Armor / st.item.Weapon). Reading
        # it at any other class is past the end of the object. Live values are
        # capitalised: Legendary, Epic, Rare.
        "Weapon": {"rarity": weapon["rarity"][0], "level": weapon["level"][0]},
        "GameLayer": {"isRift": layer["isRift"][0],
                      "mainActivity": layer["mainActivity"][0],
                      "worldEvents": layer["worldEvents"][0],
                      "time": layer["_time"][0],
                      # world -> world.World, whose `level` string is the
                      # honest zone/world identity. Main.getMapId() — the old
                      # zone signal — turned out to return the MACHINE NAME.
                      "world": layer["world"][0],
                      # Minimap: the layer keeps these lists built already, so
                      # the sweep is a walk of three arrays rather than a
                      # search. units = heroes + foes, interactibles = chests /
                      # orbs / obelisks / respawn points, entities = the widest
                      # net and the only place activities show up.
                      "units": layer["units"][0],
                      "interactibles": layer["interactibles"][0],
                      "entities": layer["entities"][0],
                      # The whole-shard roster — every player the client has
                      # state for, not merely the ones streamed in around you.
                      # This is what the Social tab lists; `units` would only
                      # ever show your neighbours.
                      "players": layer["players"][0]},
        # The loaded level's identity, for the zone signal and the map
        # backdrop. `level` is the primary; name/branchName/_isWorldMap ship
        # so the hook can report what they actually hold from normal play —
        # field names lie in this game until measured.
        "World": {"level": world["level"][0],
                  "name": world["name"][0],
                  "branchName": world["branchName"][0],
                  "_isWorldMap": world["_isWorldMap"][0]},
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
        # Ore/herb nodes (ent.interactible.Gatherable, an Element subclass).
        # hitPoints is the replicated "gatherable right now" signal — measured
        # 2026-08-01: each gather tick steps it down (200..0), depletion flips
        # enabled 1->0 and NOTHING else (stateId stays "None"), and the respawn
        # arrives as hp back at max. gatherInf is the CDB row; its texts.type
        # ("Ore" | "Plant") and texts.name resolve on the game thread the same
        # way foe nameplates do.
        "Gatherable": {"hitPoints": gath["hitPoints"][0],
                       "gatherInf": gath["gatherInf"][0]},
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
        "Unit": {"kind": unit["kind"][0], "inf": unit["inf"][0],
                 # attr.health is what the boss bar is actually reading. NOTE:
                 # UnitAttributes.maxHealth reads 0 for the whole of a boss
                 # fight (measured), so health is only good for "did it die",
                 # never for a percentage.
                 "attr": unit["attr"][0]},
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
        # `uid` is the player's STEAM ACCOUNT ID, not an internal handle:
        # "S" + the id's bytes as hex in LITTLE-ENDIAN order, trailing zero
        # bytes trimmed. Measured 2026-08-02 (frida/steamid_probe.js) and
        # calibrated against both steam_get_steam_id() and the registry's
        # ActiveUser. Read the digits big-endian and you get a wrong,
        # plausible-looking number — see steam64_from_uid() in the meter.
        # `hero` is the player's live ent.Hero; it was populated for 24/24
        # players on the layer when measured, which is what lets the Social
        # tab show a class for everyone rather than only for people nearby.
        "Player": {"name": player["name"][0], "group": player["group"][0],
                   "isMe": player["isMe"][0], "lobbyId": player["lobbyId"][0],
                   "accountProgress": player["accountProgress"][0],
                   "uid": player["uid"][0], "hero": player["hero"][0]},
        # The account-wide unlock collection (mount walk measured 2026-08-01,
        # gliders 2026-08-02 — same shape): player -> accountProgress ->
        # collection -> mounts/gliders, an ArrayProxyData whose ArrayDyn wraps
        # an ArrayObj of kind Strings.
        "AccountProgress": {"collection": acct["collection"][0]},
        "Collection": {"mounts": coll["mounts"][0],
                       "gliders": coll["gliders"][0]},
        "ArrayProxyData": {"array": aproxy["array"][0]},
        "ArrayDyn": {"array": adyn["array"][0]},
        "Group": {"groupId": group["groupId"][0], "players": group["players"][0]},
        # The game's boss/elite healthbar. `bossInfos` is NOT a fixed pool —
        # measured lengths were only ever 0 (no bar) or 1 (bar up), so its
        # length alone says whether a bar is on screen. Each entry's `active`
        # is the per-slot gate; UIElement.visible tracks it but diverged on a
        # couple of samples at transitions, so `active` is the one to read.
        "BossesInfo": {"bossInfos": bosses["bossInfos"][0]},
        "BossInfo": {"active": bossinfo["active"][0],
                     "unit": bossinfo["unit"][0]},
        # Which runtime classes are foes — the set a dealer must belong to
        # before `Foe.summonOwner` may be read off it. Measured 2026-07-30:
        # only 7 classes descend from ent.Foe, so this is a small closed set,
        # and a summon's class is not reliably the literal "ent.Foe".
        "foeClasses": descendants("ent.Foe"),
        # HL virtual field indices for the skill display name
        "SkillRow": {"id_vidx": vidx(row, "id"), "texts_vidx": vidx(row, "texts")},
        "Texts": {"name_vidx": vidx(texts, "name"), "desc_vidx": vidx(texts, "desc")},
    }
    OUT.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"[written] {OUT}")

    # Display names, from the game's own data.cdb (res.light.pak, found
    # 2026-08-01 — NOT res.pak, which only carries assets and the non-English
    # lang exports). Items name the Mounts/Gliders tabs; units name the boss
    # kill toast — a bar's unit kind is routinely NOT the name on the bar
    # (measured 2026-08-02: unit 'Cleodora' displays 'Queen Honeyzabeth',
    # 'Phrixes' displays 'High Inquisitor Chakram').
    # Cosmetic, so a failure costs the labels and nothing else — but it is
    # still reported, because "the tab shows ids again" should be explainable
    # from the log.
    try:
        item_names, unit_names = extract_display_names(Path(hlboot).parent)
        out_names = _OUT_DIR / "item_names.json"
        out_names.write_text(json.dumps(item_names, ensure_ascii=False,
                                        indent=0), encoding="utf-8")
        print(f"[written] {out_names} ({len(item_names)} items)")
        out_units = _OUT_DIR / "unit_names.json"
        out_units.write_text(json.dumps(unit_names, ensure_ascii=False,
                                        indent=0), encoding="utf-8")
        print(f"[written] {out_units} ({len(unit_names)} units)")
        specs = extract_heal_specs(Path(hlboot).parent)
        out_heal = _OUT_DIR / "heal_specs.json"
        out_heal.write_text(json.dumps(specs, indent=0), encoding="utf-8")
        print(f"[written] {out_heal} ({len(specs)} heal skills)")
    except Exception as e:
        print(f"[!] display names skipped ({e}) — mount labels and boss "
              f"toasts fall back to ids")

    print(json.dumps(meta, indent=2))


# skill@steps@effects.effect is an enum; "5:Damage,Heal,Shield,GainAtb,Status"
# makes Heal index 1. Read off the column's own typeStr rather than hardcoded,
# because a patch that inserts an effect kind would silently re-point it.
HEAL_EFFECT_NAME = "Heal"


def extract_heal_specs(game_dir):
    """skill id -> {step index: how that step's heal amount is computed}.

    This is what lets a client know what a heal was WORTH. Measured
    2026-08-03: no heal amount is ever sent to a client (fifteen entry points
    hooked, only playHitHealFX fires and its HitData.amount reads 0), so the
    only way to count a heal that restored nothing is to compute it the way the
    game does. The cdb says how, per skill:

      dyn  -> the amount is in BaseSkill.dynVal1/2/3, which ARE replicated
      scale-> ratio x one of the caster's attributes (Faith on most of them)
      base -> a flat number

    Shapes seen in this build (44 heal skills): dyn only (12), scale only (27),
    base+dyn (4), base+scale (2), base only (2). Where both a base and a dyn
    are given the dyn is the real value and the base is its floor, so the host
    prefers dyn when it is non-zero.
    """
    import pak_extract
    data, entries, data_off = pak_extract.load(Path(game_dir) / "res.light.pak")
    e = next(x for x in entries if x.path.endswith("data.cdb"))
    cdb = json.loads(data[data_off + e.pos: data_off + e.pos + e.size])
    sheets = {sh["name"]: sh for sh in cdb["sheets"]}

    # Which enum index means Heal, from the column definition itself.
    heal_idx = 1
    for c in sheets.get("skill@steps@effects", {}).get("columns", []):
        if c.get("name") == "effect" and isinstance(c.get("typeStr"), str):
            parts = c["typeStr"].split(":", 1)
            if len(parts) == 2:
                names = parts[1].split(",")
                if HEAL_EFFECT_NAME in names:
                    heal_idx = names.index(HEAL_EFFECT_NAME)
            break

    out = {}
    for ln in sheets["skill"]["lines"]:
        sid = ln.get("id")
        if not isinstance(sid, str):
            continue
        steps = {}
        for i, st in enumerate(ln.get("steps") or []):
            specs = []
            for ef in (st.get("effects") or []):
                if ef.get("effect") != heal_idx:
                    continue
                scale = [[sc.get("ratio"), sc.get("atb")]
                         for sc in (ef.get("scaling") or [])
                         if isinstance(sc.get("ratio"), (int, float))
                         and isinstance(sc.get("atb"), str)]
                spec = {}
                if isinstance(ef.get("baseVal"), (int, float)):
                    spec["base"] = ef["baseVal"]
                if scale:
                    spec["scale"] = scale
                if isinstance(ef.get("dynVal"), int) and ef["dynVal"] > 0:
                    spec["dyn"] = ef["dynVal"]
                if spec:
                    specs.append(spec)
            if specs:
                steps[str(i)] = specs
        if steps:
            out[sid] = steps
    return out


def extract_display_names(game_dir):
    """(items, units): id -> texts.name for the two sheets the meter labels
    things from, in one read of the pak."""
    import pak_extract
    data, entries, data_off = pak_extract.load(Path(game_dir) / "res.light.pak")
    e = next(x for x in entries if x.path.endswith("data.cdb"))
    cdb = json.loads(data[data_off + e.pos: data_off + e.pos + e.size])

    def sheet_names(name):
        sheet = next(s for s in cdb["sheets"] if s["name"] == name)
        out = {}
        for ln in sheet["lines"]:
            iid, nm = ln.get("id"), (ln.get("texts") or {}).get("name")
            if isinstance(iid, str) and isinstance(nm, str) and nm:
                out[iid] = nm
        return out

    return sheet_names("item"), sheet_names("unit")


if __name__ == "__main__":
    main()
