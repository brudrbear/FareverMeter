"""Locate Farever's hlboot.dat without hardcoding a machine-specific path.

Resolution order:
  1. an explicit command-line argument  (py build_targets.py "D:\\...\\hlboot.dat")
  2. the FAREVER_HLBOOT environment variable
  3. auto-detect across common Steam library locations on every drive
"""
import os
import string
import sys
from pathlib import Path

_SUBPATHS = [
    r"SteamLibrary\steamapps\common\Farever\hlboot.dat",
    r"Program Files (x86)\Steam\steamapps\common\Farever\hlboot.dat",
    r"Program Files\Steam\steamapps\common\Farever\hlboot.dat",
    r"Steam\steamapps\common\Farever\hlboot.dat",
    r"Games\Farever\hlboot.dat",
]


def find_hlboot(argv_index: int = 1) -> str:
    if len(sys.argv) > argv_index:
        p = Path(sys.argv[argv_index])
        if p.is_file():
            return str(p)
        raise SystemExit(f"[!] file not found: {p}")

    env = os.environ.get("FAREVER_HLBOOT")
    if env:
        if Path(env).is_file():
            return env
        raise SystemExit(f"[!] FAREVER_HLBOOT points to a missing file: {env}")

    for drive in string.ascii_uppercase:
        root = f"{drive}:\\"
        if not os.path.exists(root):
            continue
        for sub in _SUBPATHS:
            cand = os.path.join(root, sub)
            if os.path.isfile(cand):
                return cand

    raise SystemExit(
        "[!] Could not find Farever\\hlboot.dat automatically.\n"
        "    Pass the path explicitly, e.g.:\n"
        '      py hltools\\build_targets.py "D:\\Games\\Farever\\hlboot.dat"\n'
        "    (find it under your Steam library: "
        "steamapps\\common\\Farever\\hlboot.dat)")
