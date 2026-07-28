import json, sys, time
from pathlib import Path
import frida

HERE = Path(__file__).resolve().parent
DATA = (HERE.parent / "analysis_out" / "resolver_data.json").read_text(encoding="utf-8")
JS = (HERE / "hook_probe.js").read_text(encoding="utf-8")
SRC = f"const DATA = {DATA};\n" + JS

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 45.0

def on_message(message, data):
    if message["type"] == "error":
        print("[JS ERROR]", message.get("description"))
        print(message.get("stack", ""))
        return
    p = message.get("payload", {})
    if p.get("kind") == "log":
        print(p["msg"])
    elif p.get("kind") == "ready":
        print(f"[ready ok={p.get('ok')}]  capturing for {DURATION}s")

s = frida.attach("Farever.exe")
sc = s.create_script(SRC)
sc.on("message", on_message)
sc.load()
time.sleep(DURATION)
sc.unload(); s.detach()
print("[done]")
