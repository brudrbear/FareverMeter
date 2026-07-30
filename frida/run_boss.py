"""Host for boss_probe.js — the boss-healthbar spike.

    py frida\\run_boss.py [seconds] [path\\to\\hlboot.dat]

Attaches to a running Farever, hooks ui.hud.BossesInfo.fetchBosses and reports
what the boss-bar HUD is actually doing: how often it refreshes, whether the
slot pool is fixed, and what the boss/miniboss/elite predicates say about the
units that get a bar. Walk to a boss and pull it; the transitions print as
BAR UP / BAR DOWN.

Prints a SUMMARY at the end answering the three open questions.

Unlike world_probe.js, the findices and offsets are NOT hardcoded here — they
are resolved by NAME out of hlboot.dat at launch. A stale findex would not fail
loudly, it would hook whatever function moved into that slot, which is a crash
in the user's game rather than a bad reading.
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

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 180.0


def build_targets():
    """Resolve every findex and offset the probe needs, by name."""
    # argv[1] is the duration, so the optional explicit-path override that
    # gamepath supports moves to argv[2]. Left at its default, find_hlboot
    # would read the duration as a filename and abort.
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
        "fetchBosses": proto("ui.hud.BossesInfo", "fetchBosses"),
        "init": proto("ui.hud.BossesInfo", "init"),
        "isBoss": proto("ent.Unit", "isBoss"),
        "isMiniboss": proto("ent.Unit", "isMiniboss"),
        "isElite": proto("ent.Unit", "isElite"),
        "hasBossInfo": proto("ent.Foe", "hasBossInfo"),
        "shouldShowBossInfo": proto("ent.Foe", "shouldShowBossInfo"),
    }
    b = {
        "fn": fn,
        "BossesInfo": offs("ui.hud.BossesInfo", "bossInfos"),
        "BossInfo": offs("ui.hud.BossInfo", "active", "unit", "bossName", "phase"),
        "UIElement": offs("ui.UIElement", "visible", "alpha", "removed"),
        "Unit": offs("ent.Unit", "kind", "inf", "attr"),
        "UnitAttributes": offs("ent.UnitAttributes", "health", "maxHealth"),
    }

    missing = [k for k, v in fn.items() if v is None]
    if fn["fetchBosses"] is None:
        raise SystemExit("[!] ui.hud.BossesInfo.fetchBosses not found in this "
                         "build - the boss HUD was renamed; re-run discovery.")
    for grp in ("BossesInfo", "BossInfo", "UIElement", "Unit", "UnitAttributes"):
        if not b[grp]:
            raise SystemExit(f"[!] no offsets resolved for {grp} - aborting "
                             "rather than reading a guessed address.")
    if missing:
        # Not fatal: the probe degrades to "?" for whichever predicate is gone.
        print(f"[!] not found (probe will show '?' for these): {', '.join(missing)}")
    return b


def on_message(message, data):
    if message["type"] == "error":
        print("[JS ERROR]", message.get("description"))
        return
    p = message.get("payload") or {}
    if p.get("kind") == "log":
        print(p["msg"], flush=True)


def main():
    b = build_targets()
    print(f"[*] fetchBosses findex={b['fn']['fetchBosses']}  "
          f"bossInfos@{b['BossesInfo']['bossInfos']}  "
          f"BossInfo.active@{b['BossInfo']['active']} unit@{b['BossInfo']['unit']}")

    data = (HERE.parent / "analysis_out" / "resolver_data.json").read_text(encoding="utf-8")
    off = (HERE.parent / "analysis_out" / "meter_offsets.json").read_text(encoding="utf-8")
    js = (HERE / "boss_probe.js").read_text(encoding="utf-8")
    src = (f"const DATA = {data};\nconst OFF = {off};\n"
           f"const B = {json.dumps(b)};\n" + js)

    session = frida.attach("Farever.exe")
    script = session.create_script(src)
    script.on("message", on_message)
    script.load()
    # Plain ASCII on purpose: this lands in whatever code page the console
    # happens to use, and an em-dash turns into mojibake on cp1252.
    print(f"[*] probing for {DURATION:.0f}s - walk to a boss and pull it. "
          "Ctrl+C to stop early and still get the summary.")
    try:
        time.sleep(DURATION)
    except KeyboardInterrupt:
        print("\n[*] stopping early.")
    finally:
        try:
            # Ask the agent for its summary before tearing anything down.
            script.post({"type": "summary"})
            time.sleep(0.6)
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
