# Build the Farever+ Meter release: icon -> executable -> installer.
#
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1
#
# Produces:
#   dist\FareverMeter\                     the windowed build (runnable as-is)
#   dist\FareverMeter-<version>-Setup.exe  the installer to attach to a release
#
# Run from the project root. One-time prerequisites:
#   py -m pip install pyinstaller pillow frida
#   winget install JRSoftware.InnoSetup

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# One source of truth for the version: the constant the update check compares
# against. Reading it here means the installer, its filename and the running
# app can never disagree about which release this is.
$meter = Get-Content "meter\farever_meter.py" -Raw
if ($meter -notmatch '(?m)^VERSION\s*=\s*"([^"]+)"') {
    throw "Couldn't find VERSION in meter\farever_meter.py"
}
$version = $Matches[1]
Write-Host "==> Building Farever+ Meter $version" -ForegroundColor Cyan

# --- 1. Icon ---------------------------------------------------------------
# Regenerated rather than assumed: it's the tray, executable and installer icon,
# and it's cheap to redraw.
Write-Host "==> Icon" -ForegroundColor Cyan
python packaging\make_icon.py

# --- 2. Executable ---------------------------------------------------------
Write-Host "==> PyInstaller" -ForegroundColor Cyan
python -m PyInstaller --clean --noconfirm --distpath dist --workpath build `
    packaging\farevermeter.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$exe = "dist\FareverMeter\FareverMeter.exe"
if (-not (Test-Path $exe)) { throw "Expected $exe, but it wasn't produced" }

# --- 3. Installer ----------------------------------------------------------
# winget puts Inno Setup under LOCALAPPDATA for a per-user install and under
# Program Files for a machine-wide one, so both are worth looking in.
$isccCandidates = @(
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
)
$iscc = $isccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
    Write-Warning ("Inno Setup not found, so no installer was built. " +
                   "The build in dist\FareverMeter still runs. " +
                   "Install it with: winget install JRSoftware.InnoSetup")
    exit 0
}

Write-Host "==> Inno Setup" -ForegroundColor Cyan
& $iscc "/DAppVersion=$version" "packaging\FareverMeter.iss"
if ($LASTEXITCODE -ne 0) { throw "ISCC failed" }

$setup = "dist\FareverMeter-$version-Setup.exe"
$mb = [math]::Round((Get-Item $setup).Length / 1MB, 1)
Write-Host ""
Write-Host "Done." -ForegroundColor Green
Write-Host "  $setup  ($mb MB)"
Write-Host ""
Write-Host "Before publishing: tag the repo $version to match VERSION, or the"
Write-Host "update check will tell everyone they're out of date."
