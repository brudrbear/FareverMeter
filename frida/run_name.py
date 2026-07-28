import sys, time
from pathlib import Path
import frida
HERE = Path(__file__).resolve().parent
DATA = (HERE.parent / "analysis_out" / "resolver_data.json").read_text(encoding="utf-8")
OFF = (HERE.parent / "analysis_out" / "meter_offsets.json").read_text(encoding="utf-8")
JS = (HERE / "name_probe.js").read_text(encoding="utf-8")
SRC = f"const DATA = {DATA};\nconst OFF = {OFF};\n" + JS
DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 35.0
def om(m, d):
    if m["type"] == "error": print("[ERR]", m.get("description")); return
    p = m.get("payload") or {}
    if p.get("kind") == "log": print(p["msg"])
s = frida.attach("Farever.exe"); sc = s.create_script(SRC); sc.on("message", om); sc.load()
time.sleep(DUR); sc.unload(); s.detach()
