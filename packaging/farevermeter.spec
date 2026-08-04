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
    (str(ROOT / "analysis_out" / "item_names.json"), "res/analysis_out"),
    (str(ROOT / "analysis_out" / "unit_names.json"), "res/analysis_out"),
    # Without this the installed build cannot size a heal that restored
    # nothing, and healing silently means "health actually restored" again —
    # the bug the OVER column exists to fix.
    (str(ROOT / "analysis_out" / "heal_specs.json"), "res/analysis_out"),
    (str(ROOT / "assets" / "farevermeter.ico"), "res/assets"),
    # Boss-fight cues. Played through Windows' own MCI, so they add two files
    # rather than an audio dependency.
    (str(ROOT / "assets" / "boss_pulled.wav"), "res/assets"),
    (str(ROOT / "assets" / "boss_victory.mp3"), "res/assets"),
    # Listed one by one rather than by glob, so a new cue that is added to
    # SOUND_FILES and forgotten here works from source and is silently mute in
    # the shipped build.
    (str(ROOT / "assets" / "legendary_pickup.mp3"), "res/assets"),
]

# The minimap's world-map backdrops (hltools/build_map_assets.py outputs) —
# globbed as a set, unlike the cues above: image and transform ship in pairs,
# and a world absent from the folder is simply a world without a backdrop.
for f in sorted((ROOT / "assets" / "maps").glob("*")):
    if f.suffix in (".webp", ".json"):
        datas.append((str(f), "res/assets/maps"))

# The self-heal path re-runs these against the running game's hlboot.dat after a
# Farever patch, so they have to ship — without them an installed meter couldn't
# recover from a patch without a new release.
# pak_extract is emit_offsets' import for the item-name table (data.cdb out
# of res.light.pak) — same self-heal argument as the rest.
for tool in ("build_targets.py", "emit_offsets.py", "hlbc_parser.py",
             "gamepath.py", "pak_extract.py"):
    datas.append((str(ROOT / "hltools" / tool), "res/hltools"))

# Pillow is imported inside the parse-image functions rather than at module
# level. PyInstaller does find nested imports, but naming them makes the parse
# screenshots a guaranteed part of the build rather than a lucky one — users
# should never see "pip install pillow" either.
hiddenimports = ["PIL.Image", "PIL.ImageDraw", "PIL.ImageFont",
                 # The map backdrop paints through Tk; ImageTk is its bridge.
                 "PIL.ImageTk"]

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
