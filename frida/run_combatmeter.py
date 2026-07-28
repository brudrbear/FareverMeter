"""run_combatmeter.py — spike runner: switch on Farever's own ui.hud.CombatMeter.

Proves whether we can call into the game's HL UI from a hook (the prerequisite
for ever rendering Farever+ with the game's native widgets instead of a Tk
overlay). Reads every findex and field offset out of hlboot.dat BY NAME, so it
keeps working across patches.

    py frida\\run_combatmeter.py              # enable for this session only
    py frida\\run_combatmeter.py --persist    # ...and Options.save() to options.ini

Farever must already be running.
"""
import json
import sys
import time
from pathlib import Path

import frida

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "hltools"))

from hlbc_parser import HLCode          # noqa: E402
from gamepath import find_hlboot        # noqa: E402

PERSIST = "--persist" in sys.argv
HLBOOT = find_hlboot(argv_index=99)

# (class, method) -> the name the JS asks for. Statics ($-prefixed classes) are
# bindings on the class's statics object; instance methods are protos.
WANTED_FNS = {
    "GameApp.update": ("GameApp", "update", False),
    "ui.Hud.updateVisibility": ("ui.Hud", "updateVisibility", False),
    "ui.Hud.rebuild": ("ui.Hud", "rebuild", False),
    "$Options.set_displayCombatMeter": ("$Options", "set_displayCombatMeter", True),
    "$Options.save": ("$Options", "save", True),
}

# name -> (class, field); byte offsets are computed from the runtime layout.
WANTED_OFFSETS = {
    ("GameApp", "gui"): ("GameApp", "gui"),
    ("GameUI", "gameRoot"): ("ui.GameUI", "gameRoot"),
    ("GameUiRoot", "hud"): ("ui.GameUiRoot", "hud"),
    ("Hud", "combatMeter"): ("ui.Hud", "combatMeter"),
    ("Object", "visible"): ("h2d.Object", "visible"),
    ("Object", "alpha"): ("h2d.Object", "alpha"),
}


def build_tables():
    print(f"[*] parsing {HLBOOT}", file=sys.stderr)
    code = HLCode(HLBOOT).parse()
    by_name = {t.name: t for t in code.obj_types()}

    def static_findex(cls, method):
        """Statics are bindings on the $Class type; the binding's field id
        indexes the RUNTIME field table, which starts with the super's fields."""
        t = by_name.get(cls)
        if not t:
            return None
        chain = []
        for st in code._super_chain(t.index):
            chain.extend(f.name for f in st.fields)
        for fid, findex in t.bindings:
            nm = chain[fid] if 0 <= fid < len(chain) else None
            if nm == method:
                return findex
        return None

    fns = {}
    for key, (cls, method, is_static) in WANTED_FNS.items():
        if is_static:
            fns[key] = static_findex(cls, method)
        else:
            t = by_name.get(cls)
            fns[key] = next((p.findex for p in (t.protos if t else [])
                             if p.name == method), None)

    offs = {}
    for (grp, fld), (cls, field) in WANTED_OFFSETS.items():
        t = by_name.get(cls)
        val = None
        if t:
            got = code.field_offsets(t.index).get(field)
            val = got[0] if got else None
        offs.setdefault(grp, {})[fld] = val

    missing = ([k for k, v in fns.items() if v is None]
               + [f"{g}.{f}" for g, d in offs.items()
                  for f, v in d.items() if v is None])
    if missing:
        sys.exit("[!] not found in this build (did the game patch?): "
                 + ", ".join(missing))
    print("[*] resolved:", json.dumps(fns), file=sys.stderr)
    return fns, offs


def main():
    fns, offs = build_tables()
    data = (ROOT / "analysis_out" / "resolver_data.json").read_text(encoding="utf-8")
    js = (HERE / "combatmeter_probe.js").read_text(encoding="utf-8")
    src = (f"const DATA = {data};\nconst FN = {json.dumps(fns)};\n"
           f"const OFF = {json.dumps(offs)};\n"
           f"const PERSIST = {'true' if PERSIST else 'false'};\n" + js)

    finished = {"v": False}

    def on_message(message, _data):
        if message["type"] == "error":
            print("[JS ERROR]", message.get("description"), file=sys.stderr)
            print(message.get("stack", ""), file=sys.stderr)
            finished["v"] = True
            return
        p = message.get("payload") or {}
        if p.get("kind") == "log":
            print("[hook]", p["msg"], flush=True)
        elif p.get("kind") == "report":
            print("\n---- object walk ----")
            for s in p.get("steps", []):
                print("   ", s)
            if p.get("error"):
                print("\n[!] error:", p["error"])
            print("\n---- combat meter ----")
            for k in ("visible_before", "alpha_before", "visible_after",
                      "alpha_after", "visible_forced"):
                if k in p:
                    print(f"    {k:<16} {p[k]}")
            print(f"\n[{'ok' if p.get('ok') else 'FAILED'}]")
            finished["v"] = True

    print("[*] attaching to Farever.exe ...", file=sys.stderr)
    session = frida.attach("Farever.exe")
    script = session.create_script(src)
    script.on("message", on_message)
    script.load()

    deadline = time.time() + 60
    while not finished["v"] and time.time() < deadline:
        time.sleep(0.2)
    if not finished["v"]:
        print("[!] no report within 60s — GameApp.update never fired?",
              file=sys.stderr)
    # Leave the change in place; unloading the script doesn't revert it.
    script.unload()
    session.detach()


if __name__ == "__main__":
    main()
