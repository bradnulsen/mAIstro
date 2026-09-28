#requires -Version 5.1
<#
    mAistro end-to-end installer build.

    Steps:
      1. npm run build        (frontend/dist/)
      2. PyInstaller           (dist/maistro/)
      3. Inno Setup ISCC       (dist/installer/maistro-setup-<version>.exe)

    Usage:
        ./installer/build.ps1                  # full build
        ./installer/build.ps1 -SkipFrontend    # reuse existing frontend/dist
        ./installer/build.ps1 -SkipBundle      # reuse existing dist/maistro
        ./installer/build.ps1 -Version 0.2.0   # stamp the installer

    Requirements:
      - Python with the repo's requirements.txt installed
      - pip install pyinstaller
      - Node + npm
      - Inno Setup 6 (ISCC.exe on PATH or at the default install path)
#>

[CmdletBinding()]
param(
    [string]$Version = "0.1.0",
    [switch]$SkipFrontend,
    [switch]$SkipBundle,
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

function Remove-DistFolder {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path $Path)) { return }

    # First attempt: plain Remove-Item.
    try {
        Remove-Item -Recurse -Force $Path -ErrorAction Stop
        return
    } catch {
        $firstLine = $_.Exception.Message.Split([Environment]::NewLine)[0]
        Write-Host "    initial delete failed ($firstLine) - retrying via rename" -ForegroundColor Yellow
    }

    # Fallback: rename the folder out of the way so the build can proceed even
    # if a stray handle (typically Defender real-time scan, or a still-running
    # maistro.exe from a previous bundle) is holding one of the bundled DLLs.
    $stamp = Get-Date -Format 'yyyyMMddHHmmss'
    $stash = "$Path.old.$stamp"
    try {
        Rename-Item -Path $Path -NewName (Split-Path $stash -Leaf) -ErrorAction Stop
    } catch {
        $lines = @(
            "Cannot clear ${Path}: a file in it is locked.",
            "",
            "Common causes:",
            "  * A previously launched maistro.exe (dist\maistro or Program Files\mAistro) is still running.",
            "    Kill it from Task Manager and re-run.",
            "  * Windows Defender is real-time scanning the bundle. Add the dist folder as an exclusion",
            "    (Virus and threat protection > Manage settings > Exclusions > Add folder).",
            "  * Another process has the .pyd memory-mapped. Reboot if nothing else works.",
            "",
            "Original error: $($_.Exception.Message)"
        )
        throw ($lines -join [Environment]::NewLine)
    }

    # Best-effort cleanup of the stashed folder. If it still can't be deleted,
    # leave it - the new build proceeds either way.
    for ($i = 0; $i -lt 5; $i++) {
        try { Remove-Item -Recurse -Force $stash -ErrorAction Stop; return } catch { Start-Sleep -Seconds 2 }
    }
    Write-Host "    note: could not delete stashed $stash - leaving in place" -ForegroundColor Yellow
}

function Resolve-Iscc {
    $candidates = @(
        "ISCC.exe",
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    )
    foreach ($c in $candidates) {
        $cmd = Get-Command $c -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
        if (Test-Path $c) { return $c }
    }
    return $null
}

if (-not $SkipFrontend) {
    Write-Host "==> Building frontend (npm run build)" -ForegroundColor Cyan
    Push-Location frontend
    try {
        if (-not (Test-Path node_modules)) {
            npm install
            if ($LASTEXITCODE -ne 0) { throw "npm install failed" }
        }
        npm run build
        if ($LASTEXITCODE -ne 0) { throw "npm run build failed" }
    } finally {
        Pop-Location
    }
}

if (-not $SkipBundle) {
    Write-Host "==> Building PyInstaller bundle" -ForegroundColor Cyan
    Remove-DistFolder -Path 'dist/maistro'
    if (Test-Path build) { Remove-Item -Recurse -Force build }
    python -m PyInstaller maistro.spec --noconfirm
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }
}

if (-not $SkipInstaller) {
    $iscc = Resolve-Iscc
    if (-not $iscc) {
        throw "ISCC.exe not found. Install Inno Setup 6 from https://jrsoftware.org/isdl.php"
    }
    Write-Host "==> Building Windows installer (Inno Setup, version $Version)" -ForegroundColor Cyan
    & $iscc "/DAppVersion=$Version" "installer/maistro.iss"
    if ($LASTEXITCODE -ne 0) { throw "ISCC failed" }
    Write-Host "==> Done. Installer at dist/installer/maistro-setup-$Version.exe" -ForegroundColor Green
}
