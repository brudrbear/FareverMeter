import json, sys, time
from pathlib import Path
import frida

HERE = Path(__file__).resolve().parent
DATA = (HERE.parent / "analysis_out" / "resolver_data.json").read_text(encoding="utf-8")
JS = (HERE / "count_probe.js").read_text(encoding="utf-8")
SRC = f"const DATA = {DATA};\n" + JS
DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 40.0

last = {"counts": None, "snap": None}

def on_message(message, data):
    if message["type"] == "error":
        print("[JS ERROR]", message.get("description")); return
    p = message.get("payload", {})
    if p.get("kind") == "log":
        print(p["msg"])
    elif p.get("kind") == "counts":
        last["counts"] = p["nz"]; last["snap"] = p["snap"]
        print("  fired:", ", ".join(p["nz"]) if p["nz"] else "(none yet)")

s = frida.attach("Farever.exe")
sc = s.create_script(SRC)
sc.on("message", on_message)
sc.load()
time.sleep(DURATION)
sc.unload(); s.detach()
print("\n=== FINAL SNAPSHOTS (first-call args per fired function) ===")
print(json.dumps(last["snap"], indent=2))
