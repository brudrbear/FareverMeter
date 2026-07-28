# PyInstaller spec for the Farever+ Meter.
#
#   py -m PyInstaller --clean --noconfirm packaging/farevermeter.spec
#
# Produces dist/FareverMeter/ — a windowed (console-free) build carrying its own
# Python, frida and Pillow, which is what removes "install Python", "tick Add to
# PATH" and "pip install frida" from the user's side entirely.
#
# Run from the project root, not from packaging/.
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent

# Everything the meter READS at runtime. It lands under res/ in the bundle
# (see ROOT in farever_meter.py) so the folder of agent JS we ship as "frida"
# can't be confused with the frida package itself.
datas = [
    (str(ROOT / "frida" / "meter_hook.js"), "res/frida"),
    (str(ROOT / "analysis_out" / "resolver_data.json"), "res/analysis_out"),
    (str(ROOT / "analysis_out" / "meter_offsets.json"), "res/analysis_out"),
    (str(ROOT / "assets" / "farevermeter.ico"), "res/assets"),
]

# The self-heal path re-runs these against the running game's hlboot.dat after a
# Farever patch, so they have to ship — without them an installed meter couldn't
# recover from a patch without a new release.
for tool in ("build_targets.py", "emit_offsets.py", "hlbc_parser.py",
             "gamepath.py"):
    datas.append((str(ROOT / "hltools" / tool), "res/hltools"))

# Pillow is imported inside the parse-image functions rather than at module
# level. PyInstaller does find nested imports, but naming them makes the parse
# screenshots a guaranteed part of the build rather than a lucky one — users
# should never see "pip install pillow" either.
hiddenimports = ["PIL.Image", "PIL.ImageDraw", "PIL.ImageFont"]

a = Analysis(
    [str(ROOT / "meter" / "farever_meter.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Pulled in by Pillow but never used here, and they cost tens of megabytes.
    excludes=["numpy", "scipy", "matplotlib", "pytest", "setuptools", "pydoc",
              "doctest", "unittest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="FareverMeter",
    debug=False,
    strip=False,
    upx=False,          # UPX-packed binaries are a reliable antivirus trigger,
    console=False,      # and this one already injects into a game process
    icon=str(ROOT / "assets" / "farevermeter.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="FareverMeter",
)
