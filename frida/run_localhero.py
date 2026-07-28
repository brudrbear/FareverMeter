import sys, time
from pathlib import Path
import frida

HERE = Path(__file__).resolve().parent
DATA = (HERE.parent / "analysis_out" / "resolver_data.json").read_text(encoding="utf-8")
OFF = (HERE.parent / "analysis_out" / "meter_offsets.json").read_text(encoding="utf-8")
JS = (HERE / "localhero_probe.js").read_text(encoding="utf-8")
SRC = f"const DATA = {DATA};\nconst OFF = {OFF};\n" + JS
DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0

def on_message(message, data):
    if message["type"] == "error":
        print("[JS ERROR]", message.get("description")); return
    p = message.get("payload", {})
    if p.get("kind") == "log":
        print(p["msg"])

s = frida.attach("Farever.exe")
sc = s.create_script(SRC)
sc.on("message", on_message)
sc.load()
time.sleep(DURATION)
sc.unload(); s.detach()
print("[done]")
