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

function Resolve-Iscc {
    $candidates = @(
        "ISCC.exe",
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe"
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
    if (Test-Path dist/maistro) { Remove-Item -Recurse -Force dist/maistro }
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
