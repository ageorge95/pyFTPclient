# VERSION=3
# https://github.com/ageorge95/ag95_repo_base

<#
.SYNOPSIS
    Generic updater: updates the packages of this repository's existing
    Python virtual environment.

.DESCRIPTION
    Copy this file next to install.ps1 (repository root or an '_install' or
    '_install_templates' folder inside it) and keep the CONFIGURATION section
    in sync with install.ps1.

    Steps:
      1. Find uv (PATH or the uv install folder); install it if missing.
      2. Optionally update Git submodules.
      3. Optionally recreate directory/file links.
      4. Copy pip.ini into the virtual environment, upgrade pip and upgrade
         the requirements with uv (using the indexes from pip.ini).
      5. Optionally reinstall the repository itself in editable mode.

    Run install.ps1 first if the virtual environment does not exist yet.

.PARAMETER NoPause
    Do not wait for Enter before exiting (useful for CI / scripting).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File update.ps1
#>
[CmdletBinding()]
param(
    [switch]$NoPause
)

$ErrorActionPreference = 'Stop'

# The scripts can live in the repository root or in an '_install' or
# '_install_templates' sub-folder. In the latter cases the repository root is
# one level up.
$INSTALL_DIR_NAMES = @('_install', '_install_templates')
$REPO_ROOT = $PSScriptRoot
if ($INSTALL_DIR_NAMES -contains (Split-Path -Leaf $PSScriptRoot)) { $REPO_ROOT = Split-Path -Parent $PSScriptRoot }
Set-Location -LiteralPath $REPO_ROOT

# ============================================================
# CONFIGURATION (keep in sync with install.ps1)
# ============================================================

# Name shown in messages. Defaults to the repository folder name.
$APP_NAME = Split-Path -Leaf $REPO_ROOT

# Virtual environment folder, relative to the repository root.
$VENV_DIR = 'venv'

# Allowed Python versions (same value as in install.ps1). Used here to stop if
# the existing virtual environment's Python is outside the range.
#   '>=3.10,<3.14'   any 3.10.x - 3.13.x
#   '3.12'           only 3.12.x
$PYTHON_VERSION = '>=3.14,<3.15'

# Set to $true to install uv automatically when it's not found.
$AUTO_INSTALL_UV = $true

# Where uv is installed when it's missing (shared by all repositories).
# The user PATH is NOT modified; the scripts also look here.
$UV_INSTALL_DIR = Join-Path $HOME '.local\bin'

# Official uv installer. Pin a version with e.g. 'https://astral.sh/uv/0.9.0/install.ps1'.
$UV_INSTALLER_URL = 'https://astral.sh/uv/install.ps1'

# Set to $true to run 'git submodule update --init --recursive --force'.
$UPDATE_SUBMODULES = $false

# Restrict the submodule update to these paths. Empty = all submodules.
# Example: $SUBMODULE_PATHS = @('batmon-ha', 'libs\other')
$SUBMODULE_PATHS = @()

# Links recreated after the submodule update, relative to the repository root.
# Directories get a junction and files get a hard link.
# Examples:
#   $LINKS = @(
#       @{ Link = 'bmslib';         Target = 'batmon-ha\bmslib' }
#       @{ Link = 'workers\module'; Target = 'some\other\module' }
#   )
$LINKS = @()

# Package index configuration, looked up next to this script first and then in
# the repository root. It is copied
# into the venv (so plain 'pip' uses it) and its index-url, extra-index-url and
# trusted-host entries are passed to uv. Add package servers there.
$PIP_CONFIG = 'pip.ini'

# Requirement files upgraded with 'uv pip install --upgrade -r', in order.
# Missing files are skipped with a warning.
$REQUIREMENTS_FILES = @('requirements.txt')

# Set to $true to also re-run 'pip install -e .'.
$INSTALL_EDITABLE = $false

# ============================================================
# HELPERS (no changes needed below this line)
# ============================================================

function Write-Step {
    param([string]$Message)
    Write-Host ''
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Exit-Script {
    param([int]$Code)
    if (-not $NoPause) {
        Write-Host ''
        Read-Host 'Press Enter to exit' | Out-Null
    }
    exit $Code
}

function Stop-Script {
    param([string]$Message)
    Write-Host ''
    Write-Host "ERROR: $Message" -ForegroundColor Red
    Write-Host ''
    Write-Host '!!!!! UPDATE FAILED !!!!!' -ForegroundColor Red
    Exit-Script 1
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed (exit code $LASTEXITCODE): $FilePath $($Arguments -join ' ')"
    }
}

function Get-PythonVersion {
    param([string]$Exe)
    try {
        $out = @(& $Exe -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null)
        if ($LASTEXITCODE -eq 0 -and $out.Count -gt 0 -and "$($out[-1])" -match '^\d+\.\d+\.\d+$') {
            return [version]"$($out[-1])"
        }
    }
    catch { }
    return $null
}

# Asks the venv's Python whether it is inside $PYTHON_VERSION, using the
# 'packaging' copy bundled with pip. A bare version like '3.12' means '3.12.*'.
# Returns $true, $false, or $null when the check can't be done.
$PYTHON_RANGE_CHECK = @'
import re,sys; from pip._vendor.packaging.specifiers import SpecifierSet as S; r=sys.argv[1].strip(); r=('=='+r+'.*') if re.fullmatch(r'\d+(\.\d+)?',r) else ('=='+r) if re.fullmatch(r'\d+\.\d+\.\d+',r) else r; print(S(r).contains('%d.%d.%d' % sys.version_info[:3], prereleases=True))
'@

function Test-PythonInRange {
    param([string]$VenvPython)
    if (-not $PYTHON_VERSION) { return $true }
    try {
        $out = @(& $VenvPython -c $PYTHON_RANGE_CHECK $PYTHON_VERSION 2>$null)
        if ($LASTEXITCODE -eq 0 -and $out.Count -gt 0) {
            if ("$($out[-1])" -eq 'True') { return $true }
            if ("$($out[-1])" -eq 'False') { return $false }
        }
    }
    catch { }
    return $null
}

# Returns the path of an existing uv executable, or $null.
function Find-Uv {
    $cmd = Get-Command uv.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { return $cmd.Source }
    foreach ($dir in @($UV_INSTALL_DIR, (Join-Path $HOME '.cargo\bin'))) {
        $path = Join-Path $dir 'uv.exe'
        if (Test-Path -LiteralPath $path -PathType Leaf) { return $path }
    }
    return $null
}

# Runs the official standalone installer in a child PowerShell, without touching PATH.
function Install-Uv {
    Write-Host "uv was not found, installing it into $UV_INSTALL_DIR..."
    $dir = $UV_INSTALL_DIR.Replace("'", "''")
    $url = $UV_INSTALLER_URL.Replace("'", "''")
    $command = "[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12; " +
        "`$env:UV_INSTALL_DIR = '$dir'; `$env:UV_NO_MODIFY_PATH = '1'; " +
        "Invoke-RestMethod '$url' | Invoke-Expression"
    $shell = (Get-Process -Id $PID).Path
    Invoke-Checked -FilePath $shell -Arguments @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', $command) | Out-Host
}

function Get-Uv {
    Write-Step 'Looking for uv...'
    $uv = Find-Uv
    if (-not $uv) {
        if (-not $AUTO_INSTALL_UV) {
            throw 'uv was not found. Install it (https://docs.astral.sh/uv/getting-started/installation/) or set $AUTO_INSTALL_UV = $true.'
        }
        Install-Uv
        $uv = Join-Path $UV_INSTALL_DIR 'uv.exe'
        if (-not (Test-Path -LiteralPath $uv -PathType Leaf)) { throw "uv was installed but '$uv' was not found." }
    }
    Write-Host "Using $(& $uv --version) : $uv" -ForegroundColor Green
    return $uv
}

function Update-Submodules {
    if (-not $UPDATE_SUBMODULES) { return }
    Write-Step 'Updating Git submodules...'
    if (-not (Test-Path -LiteralPath '.gitmodules')) {
        Write-Host 'No .gitmodules file found, skipping.' -ForegroundColor Yellow
        return
    }
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw 'git was not found in PATH.'
    }
    $gitArgs = @('submodule', 'update', '--progress', '--init', '--recursive', '--force')
    if (@($SUBMODULE_PATHS).Count -gt 0) { $gitArgs += @('--') + @($SUBMODULE_PATHS) }
    Invoke-Checked -FilePath 'git' -Arguments $gitArgs
}

function New-RepoLink {
    param([string]$Link, [string]$Target)

    $linkPath = Join-Path $REPO_ROOT $Link
    $targetPath = Join-Path $REPO_ROOT $Target

    if (-not (Test-Path -LiteralPath $targetPath)) {
        throw "Link target '$Target' was not found. Is the submodule missing? (set `$UPDATE_SUBMODULES = `$true)"
    }

    # Remove an existing link, but never a real directory.
    $existing = Get-Item -LiteralPath $linkPath -Force -ErrorAction SilentlyContinue
    if ($existing) {
        $isReparse = ($existing.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
        if ($existing.PSIsContainer) {
            if (-not $isReparse) {
                throw "'$Link' exists and is a real directory, refusing to replace it. Remove it manually."
            }
            # Deletes only the junction itself, not the contents of its target.
            [IO.Directory]::Delete($linkPath)
        }
        else {
            Remove-Item -LiteralPath $linkPath -Force
        }
    }

    $parent = Split-Path -Parent $linkPath
    if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent | Out-Null }

    Write-Host "Linking $Link -> $Target"
    if (Test-Path -LiteralPath $targetPath -PathType Container) {
        New-Item -ItemType Junction -Path $linkPath -Target $targetPath | Out-Null
    }
    else {
        New-Item -ItemType HardLink -Path $linkPath -Target $targetPath | Out-Null
    }
}

function New-RepoLinks {
    if (@($LINKS).Count -eq 0) { return }
    Write-Step 'Recreating links...'
    foreach ($entry in $LINKS) { New-RepoLink -Link $entry.Link -Target $entry.Target }
}

function Get-PipConfigPath {
    # Next to this script first, then in the repository root.
    foreach ($dir in @($PSScriptRoot, $REPO_ROOT)) {
        $path = Join-Path $dir $PIP_CONFIG
        if (Test-Path -LiteralPath $path -PathType Leaf) { return $path }
    }
    throw "Pip configuration '$PIP_CONFIG' was not found. Copy the default pip.ini next to this script."
}

# Reads index-url, extra-index-url and trusted-host from a pip.ini file
# (any section, multi-line values supported) and returns the matching uv options.
function Get-UvIndexArgs {
    param([string]$ConfigPath)

    $values = @{ 'index-url' = @(); 'extra-index-url' = @(); 'trusted-host' = @() }
    $key = $null
    foreach ($line in Get-Content -LiteralPath $ConfigPath) {
        if ($line -match '^\s*([#;].*)?$') { continue }                  # blank line or comment
        if ($line -match '^\s*\[.*\]\s*$') { $key = $null; continue }    # [section]
        if ($line -match '^\s+(\S.*)$') {                                 # continuation line
            if ($key) { $values[$key] += @($Matches[1].Trim() -split '\s+') }
            continue
        }
        if ($line -match '^([^=:]+?)\s*[=:]\s*(.*)$') {                   # key = value
            $name = $Matches[1].Trim().ToLower().Replace('_', '-')
            $key = if ($values.ContainsKey($name)) { $name } else { $null }
            if ($key -and $Matches[2].Trim()) { $values[$key] += @($Matches[2].Trim() -split '\s+') }
        }
    }

    $uvArgs = @()
    if ($values['index-url'].Count -gt 0) { $uvArgs += @('--index-url', $values['index-url'][0]) }
    foreach ($url in $values['extra-index-url']) { $uvArgs += @('--extra-index-url', $url) }
    foreach ($h in $values['trusted-host']) { $uvArgs += @('--allow-insecure-host', $h) }
    # Like pip: pick the best matching version across all indexes.
    if ($values['extra-index-url'].Count -gt 0) { $uvArgs += @('--index-strategy', 'unsafe-best-match') }
    return , $uvArgs
}

# Copies pip.ini into the venv, so plain 'pip' inside the venv uses the same indexes.
function Copy-PipConfig {
    param([string]$VenvPath)
    Copy-Item -LiteralPath (Get-PipConfigPath) -Destination (Join-Path $VenvPath 'pip.ini') -Force
}

function Invoke-UvPip {
    param([string]$Uv, [string]$VenvPython, [string[]]$Arguments)
    $uvArgs = @('pip', 'install', '--python', $VenvPython) + $Arguments +
        (Get-UvIndexArgs (Get-PipConfigPath))
    Invoke-Checked -FilePath $Uv -Arguments $uvArgs
}

# Keeps the venv's own pip current, so plain 'pip' inside the venv keeps working.
function Update-Pip {
    param([string]$Uv, [string]$VenvPython)
    Write-Step 'Upgrading pip...'
    Invoke-UvPip -Uv $Uv -VenvPython $VenvPython -Arguments @('--upgrade', 'pip')
}

function Install-Requirements {
    param([string]$Uv, [string]$VenvPython, [switch]$Upgrade)
    foreach ($req in $REQUIREMENTS_FILES) {
        if (-not (Test-Path -LiteralPath $req -PathType Leaf)) {
            Write-Host "Requirements file '$req' not found, skipping." -ForegroundColor Yellow
            continue
        }
        Write-Step "Upgrading $req..."
        $reqArgs = @()
        if ($Upgrade) { $reqArgs += '--upgrade' }
        Invoke-UvPip -Uv $Uv -VenvPython $VenvPython -Arguments ($reqArgs + @('-r', $req))
    }
}

function Install-Editable {
    param([string]$Uv, [string]$VenvPython)
    if (-not $INSTALL_EDITABLE) { return }
    if (-not ((Test-Path -LiteralPath 'setup.py') -or (Test-Path -LiteralPath 'pyproject.toml'))) {
        throw 'INSTALL_EDITABLE is enabled but neither setup.py nor pyproject.toml was found.'
    }
    Write-Step "Reinstalling $APP_NAME in editable mode..."
    Invoke-UvPip -Uv $Uv -VenvPython $VenvPython -Arguments @('-e', '.')
}

# ============================================================
# MAIN
# ============================================================

try {
    Write-Host "Updating $APP_NAME" -ForegroundColor Green

    $venvPython = Join-Path $REPO_ROOT (Join-Path $VENV_DIR 'Scripts\python.exe')
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        throw "Virtual environment not found ($VENV_DIR). Run install.ps1 first."
    }

    $version = Get-PythonVersion $venvPython
    if (-not $version) {
        throw "The virtual environment's Python is not working. Run install.ps1 to recreate it."
    }
    Write-Host "Virtual environment Python $version"
    $inRange = Test-PythonInRange $venvPython
    if ($inRange -eq $false) {
        throw "Python $version is outside the allowed range '$PYTHON_VERSION' for $APP_NAME. Run install.ps1 to recreate the virtual environment."
    }
    if ($null -eq $inRange) {
        Write-Host "WARNING: could not check Python $version against '$PYTHON_VERSION' (pip missing from the venv?)." -ForegroundColor Yellow
    }
    $null = Get-PipConfigPath   # fail early, before anything is changed

    $uv = Get-Uv

    Update-Submodules
    New-RepoLinks

    Copy-PipConfig -VenvPath (Join-Path $REPO_ROOT $VENV_DIR)
    Update-Pip -Uv $uv -VenvPython $venvPython
    Install-Requirements -Uv $uv -VenvPython $venvPython -Upgrade
    Install-Editable -Uv $uv -VenvPython $venvPython
}
catch {
    Stop-Script $_.Exception.Message
}

Write-Host ''
Write-Host "$APP_NAME update complete." -ForegroundColor Green

Exit-Script 0
