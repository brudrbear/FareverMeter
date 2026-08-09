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
    # Every status the game defines, named and categorised. Without it the buff
    # picker lists raw ids and — worse — cannot tell a real buff from the
    # game's internal plumbing, since "the cdb never named it" is exactly the
    # test that hides Dash_Status and friends.
    (str(ROOT / "analysis_out" / "status_meta.json"), "res/analysis_out"),
    (str(ROOT / "assets" / "farevermeter.ico"), "res/assets"),
    # The settings panel's markup. Three files rather than one string constant
    # in menu_host.py, which reads and inlines them into a single document at
    # startup — so they stay editable, and there is no file:// origin to get
    # wrong. Required, not optional: without them the panel opens blank.
    (str(ROOT / "meter" / "web" / "menu.html"), "res/web"),
    (str(ROOT / "meter" / "web" / "menu.css"), "res/web"),
    (str(ROOT / "meter" / "web" / "menu.js"), "res/web"),
]

# The Help tab's articles. Globbed rather than listed, because a help topic
# exists to be added to — a new .md file should ship by being written.
_help = sorted((ROOT / "meter" / "web" / "help").glob("*.md"))
if not _help:
    raise SystemExit("[!] meter/web/help holds no articles — the Help tab "
                     "would ship empty")
for f in _help:
    datas.append((str(f), "res/web/help"))

# The fundraiser logo on the Help tab. Optional on purpose — the button falls
# back to a wordmark when it is absent, so a missing image is a slightly
# plainer button rather than a broken build.
_logo = ROOT / "assets" / "gofundme.png"
if _logo.is_file():
    datas.append((str(_logo), "res/assets"))
else:
    print("[i] assets/gofundme.png not found — the Help tab's support button "
          "will use its text fallback.")

datas += [
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

# The buff trays' icon sheet (hltools/build_status_icons.py output). Sheet and
# index ship as a pair like the map backdrops, and for the same reason: the
# index addresses cells in that exact sheet, so shipping one without the other
# is worse than shipping neither. Required, not optional — without it every
# tracked buff falls back to a three-letter coloured tile, which is a feature
# that technically works and nobody would want.
_status_dir = ROOT / "assets" / "status"
_status_files = [_status_dir / "icons.webp", _status_dir / "icons.json"]
_missing = [f.name for f in _status_files if not f.is_file()]
if _missing:
    raise SystemExit(
        f"[!] assets/status is missing {', '.join(_missing)} — the buff trays "
        "would ship with no icons. Run: python hltools/build_status_icons.py")
for f in _status_files:
    datas.append((str(f), "res/assets/status"))

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
                 "PIL.ImageTk",
                 # The settings panel's process. FareverMeter.exe re-enters
                 # itself with --menu-host and imports this, so nothing in the
                 # import graph points at it and PyInstaller cannot find it on
                 # its own.
                 "menu_host",
                 # pywebview picks its backend at runtime by importing it, so
                 # the WinForms/EdgeChromium one is invisible to the analysis
                 # too. clr is pythonnet's entry point into .NET.
                 "webview.platforms.winforms", "clr"]

a = Analysis(
    [str(ROOT / "meter" / "farever_meter.py")],
    # meter/ as well as the root, so the "menu_host" hidden import above
    # resolves — it is a sibling module, not a package member.
    pathex=[str(ROOT), str(ROOT / "meter")],
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
