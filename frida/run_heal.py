"""Host for heal_probe.js — can a client see the RAW heal amount?

    py frida\\run_heal.py [seconds] [path\\to\\hlboot.dat]

The meter derives healing from a rise in the target's replicated health, so a
heal on a full-health target counts as zero. This attaches to every heal entry
point in the build and prints which ones actually run client-side and what they
carry, plus a ledger of how many heal events the current rule cannot see.

Findices and offsets are resolved by NAME out of hlboot.dat, not hardcoded.
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

# Every heal entry point in the build, short name -> (class, method). The whole
# pipeline is here on purpose: the previous session's note says the server-side
# half never runs on a client, and this is what re-checks that rather than
# inheriting it.
TARGETS = {
    "Unit.receiveHeal":            ("ent.Unit", "receiveHeal"),
    "Unit.computeHeal":            ("ent.Unit", "computeHeal"),
    "Unit.shouldDisplayHeal":      ("ent.Unit", "shouldDisplayHeal"),
    "Unit.rpcDisplayHeal":         ("ent.Unit", "rpcDisplayHeal"),
    "Unit.rpcDisplayHeal__impl":   ("ent.Unit", "rpcDisplayHeal__impl"),
    "Unit.playHitHealFX":          ("ent.Unit", "playHitHealFX"),
    "BaseSkill.evalHeal":          ("st.skill.BaseSkill", "evalHeal"),
    "BaseSkill.onHealEval":        ("st.skill.BaseSkill", "onHealEval"),
    "BaseSkill.onInflictHealEval": ("st.skill.BaseSkill", "onInflictHealEval"),
    "BaseSkill.onReceiveHealEval": ("st.skill.BaseSkill", "onReceiveHealEval"),
    "SkillScript.applyHeal":       ("script.SkillScript", "applyHeal"),
    "SkillScript.onHeal":          ("script.SkillScript", "onHeal"),
    "SkillScript.onInflictHeal":   ("script.SkillScript", "onInflictHeal"),
    "EffectsFeed.displayHeal":     ("ui.hud.EffectsFeed", "displayHeal"),
    "UnitAttributes.set_health":   ("ent.UnitAttributes", "set_health"),
}


def build_targets():
    code = HLCode(find_hlboot(argv_index=2)).parse()
    assert_resolver_current(code)   # stale analysis_out reads as 'found nothing'
    byname = {t.name: t for t in code.types
              if t.kind in (HOBJ, HSTRUCT) and t.name}

    def proto(cls, meth):
        t = byname.get(cls)
        return next((p.findex for p in t.protos if p.name == meth), None) if t else None

    def offs(cls, *fields):
        t = byname.get(cls)
        if not t:
            return {}
        o = code.field_offsets(t.index)
        return {f: o[f][0] for f in fields if f in o}

    # Classes descending from ent.Unit — ent.Unit.hasMaxHealth is CALLED, and
    # dispatching it on a non-Unit would be calling a method that isn't there.
    unit_ti = byname["ent.Unit"].index
    unit_classes = []
    for t in code.types:
        if t.kind not in (HOBJ, HSTRUCT) or not t.name:
            continue
        i, seen = t.index, set()
        while 0 <= i < len(code.types) and i not in seen:
            seen.add(i)
            if i == unit_ti:
                unit_classes.append(t.name)
                break
            i = code.types[i].super_index

    fn = {name: proto(cls, meth) for name, (cls, meth) in TARGETS.items()}
    b = {
        "fn": fn,
        "fn2": {"hasMaxHealth": proto("ent.Unit", "hasMaxHealth")},
        "HitData": offs("st.skill.HitData", "baseSkill", "amount", "dmgMult",
                        "dmgAdd", "healMult", "block", "critChance",
                        "critDmgMult", "threatMultiplier", "effectKind",
                        "effectiveScalingLevel", "result", "target", "step",
                        "ctx"),
        "UnitAttributes": offs("ent.UnitAttributes", "unit", "health",
                               "maxHealth", "heal"),
        "BaseSkill": offs("st.skill.BaseSkill", "curHit"),
        "unitClasses": unit_classes,
    }
    for need in ("amount", "baseSkill"):
        if need not in b["HitData"]:
            raise SystemExit(f"[!] HitData.{need} not found — the struct "
                             "changed; re-run discovery.")
    if "maxHealth" not in b["UnitAttributes"]:
        raise SystemExit("[!] UnitAttributes.maxHealth not found.")
    return b


ARMED = {"ok": False}


def preflight():
    """Refuse to become a SECOND frida host on this game.

    Stacked sessions chain their inline hooks, and the later teardown writes
    back the earlier session's jump into a freed trampoline — the mechanism
    behind a game crash mid-session on 2026-08-01. One attached host at a time.
    """
    import subprocess
    offenders = []
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | "
             "Where-Object { $_.Name -match 'python|FareverMeter|FareverPortal' } | "
             "ForEach-Object { \"$($_.ProcessId)|$($_.CommandLine)\" }"],
            capture_output=True, text=True, timeout=25).stdout
    except Exception:
        return
    me = str(Path(__file__).name)
    for line in out.splitlines():
        low = line.lower()
        if me.lower() in low:
            continue
        if ("farever_meter.py" in low or "farevermeter.exe" in low
                or "portal.py" in low or "fareverportal.exe" in low):
            offenders.append(line.strip())
    if offenders:
        print("[!] another frida host is attached to Farever:")
        for o in offenders:
            print("      " + o)
        raise SystemExit(
            "[!] close it first (its own UI/Ctrl+C) — never attach alongside.")


def on_message(message, data):
    if message["type"] == "error":
        print("[JS ERROR]", message.get("description"))
        return
    p = message.get("payload") or {}
    if p.get("kind") == "log":
        print(p["msg"], flush=True)
    elif p.get("kind") == "armed":
        ARMED["ok"] = True
        print("[ARMED]", flush=True)


def main():
    preflight()
    b = build_targets()
    missing = [n for n, fi in b["fn"].items() if fi is None]
    print(f"[*] {len(b['fn']) - len(missing)}/{len(b['fn'])} heal targets resolved"
          + (f"   MISSING: {' '.join(missing)}" if missing else ""))
    print(f"[*] HitData.amount@{b['HitData']['amount']}  "
          f"UnitAttributes.maxHealth@{b['UnitAttributes']['maxHealth']}  "
          f"({len(b['unitClasses'])} unit classes)")

    data = (HERE.parent / "analysis_out" / "resolver_data.json").read_text(encoding="utf-8")
    off = (HERE.parent / "analysis_out" / "meter_offsets.json").read_text(encoding="utf-8")
    js = (HERE / "heal_probe.js").read_text(encoding="utf-8")
    src = (f"const DATA = {data};\nconst OFF = {off};\n"
           f"const B = {json.dumps(b)};\n" + js)

    session = frida.attach("Farever.exe")
    script = session.create_script(src)
    script.on("message", on_message)
    # Returns promptly because the agent defers its scan off load().
    script.load()
    print(f"[*] listening for {DURATION:.0f}s. Ctrl+C to stop early.")
    try:
        time.sleep(DURATION)
    except KeyboardInterrupt:
        print("\n[*] stopping early.")
    finally:
        # unload/detach wedges on this game. Killing this process with live
        # hooks is the known game-crasher, so if cleanup hangs we say so and
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
