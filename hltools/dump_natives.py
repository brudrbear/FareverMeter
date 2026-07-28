"""Dump natives grouped by lib, and export a JSON of anchor candidates +
target functions for the Frida resolver."""
import json, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from hlbc_parser import HLCode

HLBOOT = r"E:\SteamLibrary\steamapps\common\Farever\hlboot.dat"
OUT = Path(__file__).resolve().parent.parent / "analysis_out"

code = HLCode(HLBOOT).parse()
by_lib = defaultdict(list)
for n in code.natives:
    by_lib[n.lib].append(n)

print("natives by lib:")
for lib, ns in sorted(by_lib.items(), key=lambda kv: -len(kv[1])):
    sample = ", ".join(n.name for n in ns[:6])
    print(f"  {lib:<10} {len(ns):>4}   e.g. {sample}")

# Anchor candidates: natives from loaded hdll modules (clean lib_name exports).
HDLL_LIBS = {"ssl", "fmt", "uv", "ui", "sdl", "directx", "dx12", "openal",
             "heaps", "steam", "video", "hlfmod", "mysql", "dlss"}
anchors = []
for n in code.natives:
    if n.lib in HDLL_LIBS:
        anchors.append({"lib": n.lib, "name": n.name, "findex": n.findex,
                        "symbol": f"{n.lib}_{n.name}", "module": f"{n.lib}.hdll"})

# Target functions to resolve/hook.
names = code.findex_names()
targets = {
    "ent.Unit.applyDamage": None,
    "ent.Hero.applyDamage": None,
    "script.SkillScript.applyDamage": None,
}
for findex, nm in names.items():
    if nm in targets:
        targets[nm] = findex

payload = {
    "nfunctions": code.counts["nfunctions"],
    "nnatives": code.counts["nnatives"],
    "anchors": anchors[:40],
    "targets": targets,
}
(OUT / "resolver_data.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
print(f"\nanchors chosen: {len(payload['anchors'])} (showing 8):")
for a in payload["anchors"][:8]:
    print(f"    findex={a['findex']:<6} {a['module']}!{a['symbol']}")
print("\ntargets:", json.dumps(targets, indent=2))
print(f"\n[written] {OUT / 'resolver_data.json'}")
