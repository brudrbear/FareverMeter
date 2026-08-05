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
    # Names a boss kill toast and every combat history dataset: a unit's kind
    # is a backend id, not what the game puts on its nameplate.
    (str(ROOT / "analysis_out" / "unit_names.json"), "res/analysis_out"),
    # Without this the installed build cannot size a heal that restored
    # nothing, and healing silently means "health actually restored" again —
    # the bug the OVER column exists to fix.
    (str(ROOT / "analysis_out" / "heal_specs.json"), "res/analysis_out"),
    # The codex rank thresholds and the excluded/elite/big unit lists. Without
    # it every mob is treated as an ordinary foe and none as excluded, so the
    # toast quotes 1/8/20 at an elite that masters in one kill.
    (str(ROOT / "analysis_out" / "codex_units.json"), "res/analysis_out"),
    # Which unit kinds are critters and which carry the Spark flag. Without it
    # critters draw as ordinary red enemies and the sparkly tracker never fires.
    (str(ROOT / "analysis_out" / "unit_traits.json"), "res/analysis_out"),
    (str(ROOT / "assets" / "farevermeter.ico"), "res/assets"),
    # Fixed cues, played through Windows' own MCI so they add files rather than
    # an audio dependency. Listed one by one rather than by glob, so a new cue
    # added to SOUND_FILES and forgotten here fails loudly at build review
    # instead of working from source and being silently mute when shipped.
    (str(ROOT / "assets" / "boss_pulled.wav"), "res/assets"),
    (str(ROOT / "assets" / "legendary_pickup.mp3"), "res/assets"),
]

# The minimap's world-map backdrops (hltools/build_map_assets.py outputs) —
# globbed as a set, unlike the cues above: image and transform ship in pairs,
# and a world absent from the folder is simply a world without a backdrop.
for f in sorted((ROOT / "assets" / "maps").glob("*")):
    if f.suffix in (".webp", ".json"):
        datas.append((str(f), "res/assets/maps"))

# The POOLED cues (SOUND_POOLS): one folder per cue, every playable file in it.
# Globbed on purpose, and for the opposite reason the fixed cues are listed by
# hand — a pool exists to be added to, so "whatever is in the folder at build
# time" is exactly the intent.
#
# This ships the DEFAULTS only. A user's own additions go in
# %LOCALAPPDATA%\FareverMeter\sounds\<cue>, which the meter also reads and
# which survives an update — putting them here would mean losing them on the
# next install.
for pool in ("victory", "codex"):
    d = ROOT / "assets" / pool
    if not d.is_dir():
        raise SystemExit(f"[!] sound pool assets/{pool} is missing — the "
                         f"{pool} cue would ship mute")
    found = [f for f in sorted(d.glob("*"))
             if f.suffix.lower() in (".mp3", ".wav", ".wma", ".m4a", ".aac")]
    if not found:
        raise SystemExit(f"[!] sound pool assets/{pool} is empty — the "
                         f"{pool} cue would ship mute")
    for f in found:
        datas.append((str(f), f"res/assets/{pool}"))

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
