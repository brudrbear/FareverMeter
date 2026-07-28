"""Inject resolver_data.json into resolve_functions.js and run it against Farever."""
import json, sys, time
from pathlib import Path
import frida

HERE = Path(__file__).resolve().parent
DATA = (HERE.parent / "analysis_out" / "resolver_data.json").read_text(encoding="utf-8")
JS = (HERE / "resolve_functions.js").read_text(encoding="utf-8")
SRC = f"const DATA = {DATA};\n" + JS

done = {"v": False}

def on_message(message, data):
    if message["type"] == "error":
        print("[JS ERROR]", message.get("description"))
        print(message.get("stack", ""))
        done["v"] = True
        return
    p = message.get("payload", {})
    if p.get("kind") == "log":
        print("  ", p["msg"])
    elif p.get("kind") == "result":
        print("\n=== RESULT ===")
        print(json.dumps(p, indent=2))
        done["v"] = True

s = frida.attach("Farever.exe")
sc = s.create_script(SRC)
sc.on("message", on_message)
sc.load()
for _ in range(60):
    if done["v"]:
        break
    time.sleep(0.5)
sc.unload(); s.detach()
