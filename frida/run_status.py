"""Host for status_probe.js — the buff/status tracking spike.

    py frida\\run_status.py [seconds] [path\\to\\hlboot.dat]

Attaches to a running Farever and answers what static analysis cannot about
ent.Unit.statuses. The static picture is in status_probe.js's header; the
in-game shopping list is:

Round 1 (2026-08-08, Warrior, ~14 minutes of ordinary play) settled most of it
— see status_probe.js's header. What is LEFT for round 2 is one question, and
it needs about thirty seconds:

  1. Just stand there with any buff up. The [tick] line prints serverNow and
     `now-wall`. If serverNow is the clock startTime lives in, `now-wall` holds
     STILL across the run and `left=` counts down at one second per second. If
     it drifts or reads nonsense, the clock is somewhere else and the tracker
     has to latch expiries against the host's own clock instead.

Round 1's shopping list, kept because a re-run after a patch wants it:

  1. Just stand there. The resting dump prints both status arrays and the
     server clocks, so a passive/aura buff is on record before anything moves.
  2. Cast something that buffs YOU, and stand still while it runs out.
  3. Cast a STACKING buff and build it up. `stacks` on the edge lines shows
     whether one application reads 0 or 1, and whether stacks are one entry
     or several.
  4. Take a debuff — a bleed or a burn off a mob. Confirms the array carries
     what's done TO you, not only what you gave yourself.
  5. Dash, roll, sprint. These raise internal statuses (Dash_Status and
     friends), and the run summary names them so the picker can hide them.
  6. Eat or drink something. Every ItemStatus reports kind "ItemStatus", so
     the <item> tag on its line is the only thing that tells two apart.

Everything is resolved by NAME from hlboot.dat at launch. The probe reads and
logs; nothing is called and nothing is written.
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
        "Unit": offs("ent.Unit", kind="kind", statuses="statuses",
                     instigatedStatuses="instigatedStatuses"),
        "Hero": offs("ent.Hero", layer="layer", name="name"),
        # Status extends BaseSkill, so the inherited fields (kind, startTime,
        # stopTime, duration, owner) resolve off the subclass — which is the
        # only way to be sure the offsets are the ones a Status object really
        # has rather than the ones its parent would.
        "Status": offs("st.skill.Status", kind="kind", stacks="stacks",
                       startTime="startTime", stopTime="stopTime",
                       duration="duration", refreshDuration="refreshDuration",
                       shieldAmount="shieldAmount", auraRange="auraRange",
                       removed="removed", owner="owner", inf="inf",
                       originItem="originItem"),
        # `_time`, NOT `time`: st.GameLayer names the field with the underscore
        # and exposes it as a `get_time` property. Asking for "time" resolves to
        # nothing, silently, and every clock read comes back null — which is
        # exactly what round 1 of this probe did. emit_offsets.py has always
        # read `layer["_time"]` here; this is the probe catching up to it.
        "GameLayer": offs("st.GameLayer", time="_time"),
        "TimeState": offs("TimeState", serverNow="serverNow",
                          serverStart="serverStart"),
        # ItemStatus identity. Measured round 1: `kind` reads the literal string
        # "ItemStatus" for EVERY item-granted status, so a potion and a meal are
        # indistinguishable by kind. st.skill.BaseSkill.originItem is the only
        # thing that can tell them apart.
        "Item": offs("st.Item", kind="kind"),
        "ArrayProxyData": offs("hxbit.ArrayProxyData", array="array"),
        "ArrayDyn": offs("hl.types.ArrayDyn", array="array"),
    }
    # The two that decide whether the probe can say anything at all. Everything
    # else is allowed to be absent and print "-" rather than abort a run the
    # user has already loaded the game for.
    for grp, need in (("Unit", 3), ("Status", 6), ("ArrayProxyData", 1),
                      ("ArrayDyn", 1)):
        if len(p[grp]) < need:
            raise SystemExit(f"[!] offsets for {grp} did not fully resolve "
                             f"({p[grp]}) - aborting rather than guessing.")
    if p["fn"]["postUpdate"] is None:
        raise SystemExit("[!] client.BaseCamera.postUpdate not found - no "
                         "game-thread anchor; aborting.")
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
    print(f"[*] Unit.statuses@{p['Unit']['statuses']}  "
          f"Unit.instigatedStatuses@{p['Unit'].get('instigatedStatuses')}  "
          f"Status.stacks@{p['Status'].get('stacks')}  "
          f"Status.stopTime@{p['Status'].get('stopTime')}", flush=True)

    data = (HERE.parent / "analysis_out" / "resolver_data.json").read_text(encoding="utf-8")
    off = (HERE.parent / "analysis_out" / "meter_offsets.json").read_text(encoding="utf-8")
    js = (HERE / "status_probe.js").read_text(encoding="utf-8")
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
        # The kinds-seen roll-up is the run's headline for question 6, so ask
        # for it before tearing the script down.
        try:
            script.post({"type": "summary"})
            time.sleep(0.6)
        except Exception:
            pass
        # unload/detach can wedge on this game (observed 2026-08-02, and this
        # probe's per-frame postUpdate Interceptor is the shape that does it).
        # Killing this process with live hooks is the known game-crasher, so if
        # cleanup hangs we say so and WAIT — the session dies with the game,
        # never the other way round.
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
            t.join()          # blocks until the frida session dies
    print("[done]")


if __name__ == "__main__":
    main()
