# VERSION=3
# https://github.com/ageorge95/ag95_repo_base

<#
.SYNOPSIS
    Generic installer: creates a Python virtual environment for this repository
    with uv and installs its requirements.

.DESCRIPTION
    Copy this file (together with update.ps1 and pip.ini) into the root of a
    repository or into an '_install' or '_install_templates' folder inside it,
    and adapt the CONFIGURATION section below. Nothing else should need
    changing.

    The requested Python version does NOT have to be installed on the system:
    uv downloads a standalone Python build when no matching interpreter is found.

    Steps:
      1. Find uv (PATH or the uv install folder); install it if missing.
      2. Optionally update Git submodules.
      3. Optionally create directory/file links.
      4. (Re)create the virtual environment with 'uv venv' using the requested
         Python version (downloaded by uv if needed), copy pip.ini into it and
         install the requirements with uv (using the indexes from pip.ini).
      5. Optionally install the repository itself in editable mode.

.PARAMETER Python
    Python to use instead of $PYTHON_VERSION. Anything uv understands:
    a version ('3.11', '3.12.4', '>=3.10,<3.13') or an interpreter path.

.PARAMETER NoPause
    Do not wait for Enter before exiting (useful for CI / scripting).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1
    powershell -ExecutionPolicy Bypass -File install.ps1 -Python 3.11 -NoPause
    powershell -ExecutionPolicy Bypass -File install.ps1 -Python C:\Python311\python.exe
#>
[CmdletBinding()]
param(
    [string]$Python = '',
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
# CONFIGURATION
# ============================================================

# Name shown in messages. Defaults to the repository folder name.
$APP_NAME = Split-Path -Leaf $REPO_ROOT

# Virtual environment folder, relative to the repository root.
$VENV_DIR = 'venv'

# Allowed Python versions for the virtual environment (uv version request).
# uv first uses a matching Python that is already available (uv-managed ones
# first, newest first; then installed ones, first match found). If none
# matches, uv downloads the newest stable version inside the range
# (no admin rights needed).
#   '>=3.10,<3.14'   any 3.10.x - 3.13.x
#   '>=3.10,!=3.12.1,<3.14'   same, but never 3.12.1
#   '3.12'           only 3.12.x
#   '3.12.4'         exactly 3.12.4
$PYTHON_VERSION = '>=3.14,<3.15'

# Which Pythons uv may use:
#   'managed'       prefer uv-managed (downloaded) Pythons, but also use installed ones
#   'only-managed'  always use a uv-managed Python (same build on every machine)
#   'system'        prefer installed Pythons, download only if none matches
#   'only-system'   never download, only use installed Pythons
$PYTHON_PREFERENCE = 'managed'

# Optional mirror for the Python downloads (for networks without GitHub access).
# Passed to uv as UV_PYTHON_INSTALL_MIRROR. Empty = uv default.
$PYTHON_INSTALL_MIRROR = ''

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

# Links created after the submodule update, relative to the repository root.
# Windows doesn't allow symlinks without special privileges, so directories
# get a junction and files get a hard link.
#   Link   - path of the link to create
#   Target - path the link points to
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

# Requirement files installed with 'uv pip install -r', in order.
# Missing files are skipped with a warning.
$REQUIREMENTS_FILES = @('requirements.txt')

# Set to $true to also run 'pip install -e .' (needs setup.py or pyproject.toml).
# This registers the repository in the venv so it can be imported from anywhere,
# and 'git pull's are picked up without reinstalling.
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
    Write-Host '!!!!! INSTALLATION FAILED !!!!!' -ForegroundColor Red
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

function Test-InsideFolder {
    param([string]$Path, [string]$Folder)
    if (-not $Folder) { return $false }
    $f = [IO.Path]::GetFullPath($Folder).TrimEnd('\') + '\'
    return [IO.Path]::GetFullPath($Path).StartsWith($f, [StringComparison]::OrdinalIgnoreCase)
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
    Write-Step 'Creating links...'
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

# Creates the venv with the requested Python; uv downloads it if it's not installed.
# --seed puts pip into the venv, so plain 'pip' / 'python -m pip' keep working.
function New-Venv {
    param([string]$Uv)
    $request = if ($Python) { $Python } else { $PYTHON_VERSION }
    $label = if ($request) { "Python $request" } else { 'the default Python' }
    Write-Step "Creating the virtual environment ($VENV_DIR) with $label..."
    Write-Host 'uv downloads this Python if no matching installation is found.'

    $venvArgs = @('venv', $VENV_DIR, '--seed', '--python-preference', $PYTHON_PREFERENCE)
    if ($request) { $venvArgs += @('--python', $request) }
    $venvArgs += Get-UvIndexArgs (Get-PipConfigPath)
    Invoke-Checked -FilePath $Uv -Arguments $venvArgs
}

function Invoke-UvPip {
    param([string]$Uv, [string]$VenvPython, [string[]]$Arguments)
    $uvArgs = @('pip', 'install', '--python', $VenvPython) + $Arguments +
        (Get-UvIndexArgs (Get-PipConfigPath))
    Invoke-Checked -FilePath $Uv -Arguments $uvArgs
}

function Install-Requirements {
    param([string]$Uv, [string]$VenvPython, [switch]$Upgrade)
    foreach ($req in $REQUIREMENTS_FILES) {
        if (-not (Test-Path -LiteralPath $req -PathType Leaf)) {
            Write-Host "Requirements file '$req' not found, skipping." -ForegroundColor Yellow
            continue
        }
        Write-Step "Installing $req..."
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
    Write-Step "Installing $APP_NAME in editable mode..."
    Invoke-UvPip -Uv $Uv -VenvPython $VenvPython -Arguments @('-e', '.')
}

# ============================================================
# MAIN
# ============================================================

try {
    Write-Host "Installing $APP_NAME" -ForegroundColor Green
    $null = Get-PipConfigPath   # fail early, before anything is changed
    if ($PYTHON_INSTALL_MIRROR) { $env:UV_PYTHON_INSTALL_MIRROR = $PYTHON_INSTALL_MIRROR }

    # --- uv ---
    $uv = Get-Uv

    # --- Submodules and links ---
    Update-Submodules
    New-RepoLinks

    # --- Virtual environment ---
    $venvPath = Join-Path $REPO_ROOT $VENV_DIR
    if (Test-Path -LiteralPath $venvPath) {
        Write-Step "Removing the existing virtual environment ($VENV_DIR)..."
        if ($env:VIRTUAL_ENV -and (Test-InsideFolder $env:VIRTUAL_ENV $venvPath)) {
            throw "The virtual environment is active in this shell. Run 'deactivate' first."
        }
        Remove-Item -LiteralPath $venvPath -Recurse -Force
    }

    New-Venv -Uv $uv

    $venvPython = Join-Path $venvPath 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) { throw "Virtual environment Python not found: $venvPython" }
    $version = Get-PythonVersion $venvPython
    if (-not $version) { throw "The virtual environment's Python is not working: $venvPython" }
    Write-Host "Virtual environment Python $version" -ForegroundColor Green

    # activate.bat / activate.ps1 in the repository root: small wrappers that
    # call the real scripts, so they keep working when the venv is recreated.
    $activateBat = Join-Path $REPO_ROOT 'activate.bat'
    if (Get-Item -LiteralPath $activateBat -Force -ErrorAction SilentlyContinue) {
        Remove-Item -LiteralPath $activateBat -Force
    }
    Set-Content -LiteralPath $activateBat -Encoding ascii -Value @(
        '@REM Activates the virtual environment created by install.ps1.',
        "@call `"%~dp0$VENV_DIR\Scripts\activate.bat`""
    )

    $activatePs1 = Join-Path $REPO_ROOT 'activate.ps1'
    if (Get-Item -LiteralPath $activatePs1 -Force -ErrorAction SilentlyContinue) {
        Remove-Item -LiteralPath $activatePs1 -Force
    }
    Set-Content -LiteralPath $activatePs1 -Encoding utf8 -Value @(
        '# Activates the virtual environment created by install.ps1.',
        ". (Join-Path `$PSScriptRoot '$VENV_DIR\Scripts\Activate.ps1')"
    )

    # --- Packages ---
    Copy-PipConfig -VenvPath $venvPath
    Install-Requirements -Uv $uv -VenvPython $venvPython
    Install-Editable -Uv $uv -VenvPython $venvPython
}
catch {
    Stop-Script $_.Exception.Message
}

# ============================================================
# POST-INSTALLATION
# ============================================================

Write-Host ''
Write-Host '@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@'
Write-Host ''
Write-Host "$APP_NAME installation complete." -ForegroundColor Green
Write-Host ''
Write-Host "Activate $APP_NAME's Python virtual environment with:"
Write-Host "  cmd        : activate"
Write-Host "  PowerShell : .\activate.ps1"
Write-Host "and run 'deactivate' to leave it."
Write-Host ''
Write-Host '@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@'

Exit-Script 0
