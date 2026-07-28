"""Headless test of meter_hook.js: print per-player damage tallies."""
import sys, time
from collections import defaultdict
from pathlib import Path
import frida

HERE = Path(__file__).resolve().parent
DATA = (HERE.parent / "analysis_out" / "resolver_data.json").read_text(encoding="utf-8")
OFF = (HERE.parent / "analysis_out" / "meter_offsets.json").read_text(encoding="utf-8")
JS = (HERE / "meter_hook.js").read_text(encoding="utf-8")
SRC = f"const DATA = {DATA};\nconst OFF = {OFF};\n" + JS
DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0

tally = defaultdict(lambda: [0, 0.0, False, False])  # player -> [hits,total,is_me,in_party]

def on_message(message, data):
    if message["type"] == "error":
        print("[JS ERR]", message.get("description")); return
    p = message.get("payload") or {}
    k = p.get("kind")
    if k == "hit":
        t = tally[p["player"]]
        t[0] += 1; t[1] += p["amount"]
        t[2] = t[2] or bool(p["is_me"]); t[3] = t[3] or bool(p.get("in_party"))
    elif k in ("log", "hero", "ready"):
        print("[hook]", p)

s = frida.attach("Farever.exe")
sc = s.create_script(SRC); sc.on("message", on_message); sc.load()
start = time.time()
while time.time() - start < DUR:
    time.sleep(3)
    print("\n-- players (P=in party, *=me) --")
    for name, (h, tot, me, party) in sorted(tally.items(), key=lambda kv: -kv[1][1]):
        flag = ("*" if me else ("P" if party else " "))
        print(f"   {flag} {name:<14} {int(tot):>8}  {h} hits  in_party={party}")
sc.unload(); s.detach()
print("\n=== FINAL ===")
for name, (h, tot, me, party) in sorted(tally.items(), key=lambda kv: -kv[1][1]):
    flag = ("*" if me else ("P" if party else " "))
    print(f"   {flag} {name:<14} total={int(tot):>8} hits={h}  in_party={party}")
