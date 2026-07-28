"""Host for world_probe.js — the minimap spike.

    py frida\\run_world.py [seconds]

Attaches to a running Farever, sweeps the world entity arrays every 2 seconds
and prints what it finds. Walk around, loot a chest, stand in a rift; the point
is to see how the numbers move before any minimap code gets written.

Detaches cleanly on its own, or on Ctrl+C.
"""
import sys
import time
from pathlib import Path

import frida

HERE = Path(__file__).resolve().parent
DATA = (HERE.parent / "analysis_out" / "resolver_data.json").read_text(encoding="utf-8")
OFF = (HERE.parent / "analysis_out" / "meter_offsets.json").read_text(encoding="utf-8")
JS = (HERE / "world_probe.js").read_text(encoding="utf-8")
SRC = f"const DATA = {DATA};\nconst OFF = {OFF};\n" + JS

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0


def on_message(message, data):
    if message["type"] == "error":
        print("[JS ERROR]", message.get("description"))
        return
    p = message.get("payload") or {}
    if p.get("kind") == "log":
        print(p["msg"], flush=True)


def main():
    session = frida.attach("Farever.exe")
    script = session.create_script(SRC)
    script.on("message", on_message)
    script.load()
    # Plain ASCII: this lands in whatever code page the console happens to use,
    # and an em-dash turns into mojibake on a default cp1252 terminal.
    print(f"[*] sweeping for {DURATION:.0f}s - walk around, loot something, "
          "then read the deltas. Ctrl+C to stop early.")
    try:
        time.sleep(DURATION)
    except KeyboardInterrupt:
        print("\n[*] stopping early.")
    finally:
        # Same discipline as the meter: unload and detach rather than dropping
        # a half-attached agent in the game.
        try:
            script.unload()
            session.detach()
        except Exception:
            pass
    print("[done]")


if __name__ == "__main__":
    main()
