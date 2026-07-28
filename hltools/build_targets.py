"""Build resolver_data.json with anchors + a curated candidate map of combat
functions to hook-and-count, so we can discover which one actually fires."""
import json, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from hlbc_parser import HLCode, HOBJ, HSTRUCT
from gamepath import find_hlboot

HLBOOT = find_hlboot()
OUT = Path(__file__).resolve().parent.parent / "analysis_out"
print(f"[*] parsing {HLBOOT}")

code = HLCode(HLBOOT).parse()

# anchors from loaded hdlls
HDLL_LIBS = {"ssl", "fmt", "uv", "ui", "sdl", "directx", "dx12", "openal",
             "heaps", "steam", "video", "hlfmod", "mysql", "dlss"}
anchors = [{"lib": n.lib, "name": n.name, "findex": n.findex,
            "symbol": f"{n.lib}_{n.name}", "module": f"{n.lib}.hdll"}
           for n in code.natives if n.lib in HDLL_LIBS][:40]

# Curated combat classes (base classes, not per-skill leaves) + method filter.
COMBAT_CLASSES = {
    "ent.Unit", "ent.Hero", "ent.Foe", "ent.GameObject", "ent.Projectile",
    "ent.UnitAttributes",
    "st.skill.Skill", "st.skill.BaseSkill", "st.skill.SkillStep",
    "st.skill.SkillContext", "script.SkillScript", "script.UnitScript",
}
METHOD_RX = re.compile(
    r"(applyDamage|dealDamage|takeDamage|doDamage|onDamage|computeDamage|"
    r"inflict|hurt|onHit|applyHit|dealHit|receiveHit|^hit$|damage|onKill|"
    r"onDeath|die|kill|heal)", re.IGNORECASE)

candidates = {}
for t in code.types:
    if t.kind not in (HOBJ, HSTRUCT) or t.name not in COMBAT_CLASSES:
        continue
    for p in t.protos:
        if METHOD_RX.search(p.name):
            candidates[f"{t.name}.{p.name}"] = p.findex

# Also keep the specific primary targets even if filtered
names = code.findex_names()
for findex, nm in names.items():
    if nm in ("ent.Unit.applyDamage", "ent.Hero.applyDamage",
              "script.SkillScript.applyDamage", "script.UnitScript.onDamage"):
        candidates[nm] = findex

# High-signal shortlist to hook-and-count (avoid hooking hot getters).
SHORTLIST = [
    "ent.Unit.applyDamage", "ent.Unit.computeDamage", "ent.Unit.receiveDamage",
    "ent.Unit.onInflictDamage", "ent.Unit.onReceiveDamage",
    "ent.Unit.rpcReceiveDamage__impl", "ent.Unit.clientReceiveHit",
    "ent.Unit.playHitDamage", "ent.Unit.recordsDamage", "ent.Unit.doReceiveHit",
    "ent.GameObject.logDamage", "ent.GameObject.clientReceiveHit",
    "ent.GameObject.doReceiveHit", "ent.GameObject.receiveHit",
    "ent.Hero.applyDamage", "ent.Hero.receiveDamage", "ent.Hero.onReceiveDamage",
    "script.SkillScript.onInflictDamage", "script.SkillScript.onDamage",
    "script.SkillScript.onHit", "st.skill.BaseSkill.calcDamageAmount",
    "st.skill.BaseSkill.evalDamage",
    # Heals: the server-side path (receiveHeal/computeHeal/*HealEval) never
    # runs client-side, and rpcDisplayHeal is rare. What IS replicated: the
    # heal FX on the target (carries the healing skill + its owner) and the
    # unit's health attribute (carries the effective amount).
    "ent.Unit.playHitHealFX", "ent.UnitAttributes.set_health",
]
count_targets = {nm: candidates[nm] for nm in SHORTLIST if nm in candidates}

# Singleton/static accessor functions (name -> findex) for local-player ID.
SINGLETON_FNS = ["GameApp.getCameraHero", "ui.Console.getMyHero",
                 "$GameApp.getMyHero", "$GameApp.get"]
funcs = {nm.lstrip("$"): fi for fi, nm in names.items() if nm in SINGLETON_FNS}

# Current-map accessor — called only from the damage hook (game thread) for
# zone-change detection. ()->String, no args.
map_fn = next((fi for fi, nm in names.items() if nm == "$Main.getMapId"), None)

# ---- the game's own window manager (native UI awareness) ----
# ui.BaseUI.displayWindow(ui, win) / removeWindow(ui, win) are called for EVERY
# game window — escape menu, inventory, map, group, ... — with the window
# instance as the second argument, so hooking the pair gives the overlay a live
# "which game windows are open" feed and it can follow the game's UI instead of
# a hotkey. ui.win.BaseWindow.onRemove(win) is the safety net for windows torn
# down without passing through removeWindow.
UI_TARGETS = ["ui.BaseUI.displayWindow", "ui.BaseUI.removeWindow",
              "ui.win.BaseWindow.onRemove"]
ui_targets = {nm: fi for fi, nm in names.items() if nm in UI_TARGETS}

payload = {
    "nfunctions": code.counts["nfunctions"],
    "nnatives": code.counts["nnatives"],
    "anchors": anchors,
    "candidates": candidates,
    "count_targets": count_targets,
    "ui_targets": ui_targets,
    "funcs": funcs,
    "map_fn": map_fn,
}
(OUT / "resolver_data.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
print(f"anchors={len(anchors)}  candidates={len(candidates)}  "
      f"ui_targets={len(ui_targets)}/{len(UI_TARGETS)}")
for nm in UI_TARGETS:
    if nm not in ui_targets:
        print(f"    [!] UI target not found in this build: {nm}")
for nm, fi in sorted(candidates.items()):
    print(f"    {nm:<45} findex={fi}")
print(f"[written] {OUT / 'resolver_data.json'}")
