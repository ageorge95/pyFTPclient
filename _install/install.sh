#!/usr/bin/env bash
# VERSION=3
# https://github.com/ageorge95/ag95_repo_base
#
# Generic installer: creates a Python virtual environment for this repository
# with uv and installs its requirements.
#
# Copy this file (together with update.sh and pip.ini) into the root of a
# repository or into an '_install' or '_install_templates' folder inside it,
# and adapt the CONFIGURATION section below. Nothing else should need changing.
#
# The requested Python version does NOT have to be installed on the system:
# uv downloads a standalone Python build when no matching interpreter is found.
#
# Steps:
#   1. Find uv (PATH or the uv install folder); install it if missing.
#   2. Optionally update Git submodules.
#   3. Optionally create symlinks.
#   4. (Re)create the virtual environment with 'uv venv' using the requested
#      Python version (downloaded by uv if needed), copy pip.ini into it and
#      install the requirements with uv (using the indexes from pip.ini).
#   5. Optionally install the repository itself in editable mode.
#
# Usage:
#   ./install.sh [--python <version-or-path>] [--no-pause]
#
#   --python    Python to use instead of PYTHON_VERSION. Anything uv
#               understands: '3.11', '3.12.4', '>=3.10,<3.13' or a path.
#   --no-pause  Do not wait for Enter before exiting (CI / scripting).

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The scripts can live in the repository root or in an '_install' or
# '_install_templates' sub-folder. In the latter cases the repository root is
# one level up.
REPO_ROOT="$SCRIPT_DIR"
case "$(basename "$SCRIPT_DIR" | tr '[:upper:]' '[:lower:]')" in
    _install|_install_templates) REPO_ROOT="$(dirname "$SCRIPT_DIR")" ;;
esac
cd "$REPO_ROOT"

# ============================================================
# CONFIGURATION
# ============================================================

# Name shown in messages. Defaults to the repository folder name.
APP_NAME="$(basename "$REPO_ROOT")"

# Virtual environment folder, relative to the repository root.
VENV_DIR="venv"

# Allowed Python versions for the virtual environment (uv version request).
# uv first uses a matching Python that is already available (uv-managed ones
# first, newest first; then installed ones, first match found). If none
# matches, uv downloads the newest stable version inside the range
# (no root rights needed).
#   ">=3.10,<3.14"   any 3.10.x - 3.13.x
#   ">=3.10,!=3.12.1,<3.14"   same, but never 3.12.1
#   "3.12"           only 3.12.x
#   "3.12.4"         exactly 3.12.4
PYTHON_VERSION=">=3.14,<3.15"

# Which Pythons uv may use:
#   managed       prefer uv-managed (downloaded) Pythons, but also use installed ones
#   only-managed  always use a uv-managed Python (same build on every machine)
#   system        prefer installed Pythons, download only if none matches
#   only-system   never download, only use installed Pythons
PYTHON_PREFERENCE="managed"

# Optional mirror for the Python downloads (for networks without GitHub access).
# Passed to uv as UV_PYTHON_INSTALL_MIRROR. Empty = uv default.
PYTHON_INSTALL_MIRROR=""

# Set to true to install uv automatically when it's not found.
AUTO_INSTALL_UV=true

# Where uv is installed when it's missing (shared by all repositories).
# Shell profiles are NOT modified; the scripts also look here.
UV_INSTALL_DIR="$HOME/.local/bin"

# Official uv installer. Pin a version with e.g. "https://astral.sh/uv/0.9.0/install.sh".
UV_INSTALLER_URL="https://astral.sh/uv/install.sh"

# Set to true to run 'git submodule update --init --recursive --force'.
UPDATE_SUBMODULES=false

# Restrict the submodule update to these paths. Empty = all submodules.
# Example: SUBMODULE_PATHS=("batmon-ha" "libs/other")
SUBMODULE_PATHS=()

# Symlinks created after the submodule update, relative to the repository root.
# Format: "link:target"
# Examples:
#   LINKS=(
#       "bmslib:batmon-ha/bmslib"
#       "workers/module:some/other/module"
#   )
LINKS=()

# Package index configuration, looked up next to this script first and then in
# the repository root. It is copied
# into the venv (so plain 'pip' uses it) and its index-url, extra-index-url and
# trusted-host entries are passed to uv. Add package servers there.
PIP_CONFIG="pip.ini"

# Requirement files installed with 'uv pip install -r', in order.
# Missing files are skipped with a warning.
REQUIREMENTS_FILES=("requirements.txt")

# Set to true to also run 'pip install -e .' (needs setup.py or pyproject.toml).
# This registers the repository in the venv so it can be imported from anywhere,
# and 'git pull's are picked up without reinstalling.
INSTALL_EDITABLE=false

# ============================================================
# HELPERS (no changes needed below this line)
# ============================================================

PYTHON_ARG=""
NO_PAUSE=false

while [ $# -gt 0 ]; do
    case "$1" in
        --python)    PYTHON_ARG="${2:-}"; shift 2 || { echo "ERROR: --python needs a value" >&2; exit 2; } ;;
        --python=*)  PYTHON_ARG="${1#*=}"; shift ;;
        --no-pause)  NO_PAUSE=true; shift ;;
        -h|--help)   sed -n '4,29p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)           echo "ERROR: unknown argument '$1' (see --help)" >&2; exit 2 ;;
    esac
done

if [ -t 1 ]; then
    C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_CYAN=$'\033[36m'; C_OFF=$'\033[0m'
else
    C_RED=""; C_GREEN=""; C_YELLOW=""; C_CYAN=""; C_OFF=""
fi

step() { echo ""; echo "${C_CYAN}==> $*${C_OFF}"; }
warn() { echo "${C_YELLOW}$*${C_OFF}"; }
die()  { echo ""; echo "${C_RED}ERROR: $*${C_OFF}" >&2; exit 1; }

pause() {
    if [ "$NO_PAUSE" != true ] && [ -t 0 ]; then
        echo ""
        read -r -p "Press Enter to exit..." _ || true
    fi
}

on_exit() {
    local code=$?
    if [ $code -ne 0 ]; then
        echo ""
        echo "${C_RED}!!!!! INSTALLATION FAILED !!!!!${C_OFF}"
    fi
    pause
    exit $code
}
trap on_exit EXIT

# Prints "major.minor.micro" of the given interpreter, or nothing.
python_version() {
    "$1" -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>/dev/null | tail -n 1 | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' || true
}

abs_path() {
    # Absolute path of an existing file, symlinks in the directory part resolved.
    echo "$(cd "$(dirname "$1")" && pwd -P)/$(basename "$1")"
}

# Prints the path of an existing uv executable, or nothing.
find_uv() {
    local dir
    if command -v uv >/dev/null 2>&1; then command -v uv; return 0; fi
    for dir in "$UV_INSTALL_DIR" "$HOME/.cargo/bin"; do
        if [ -x "$dir/uv" ]; then echo "$dir/uv"; return 0; fi
        if [ -f "$dir/uv.exe" ]; then echo "$dir/uv.exe"; return 0; fi
    done
}

# Runs the official standalone installer without touching shell profiles.
install_uv() {
    echo "uv was not found, installing it into $UV_INSTALL_DIR..."
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf "$UV_INSTALLER_URL" | env UV_INSTALL_DIR="$UV_INSTALL_DIR" UV_NO_MODIFY_PATH=1 sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- "$UV_INSTALLER_URL" | env UV_INSTALL_DIR="$UV_INSTALL_DIR" UV_NO_MODIFY_PATH=1 sh
    else
        die "Neither curl nor wget was found, cannot install uv. Install uv manually (https://docs.astral.sh/uv/getting-started/installation/)."
    fi
}

# Finds or installs uv and stores its path in UV.
UV=""
get_uv() {
    step "Looking for uv..."
    UV="$(find_uv)"
    if [ -z "$UV" ]; then
        [ "$AUTO_INSTALL_UV" = true ] || die "uv was not found. Install it (https://docs.astral.sh/uv/getting-started/installation/) or set AUTO_INSTALL_UV=true."
        install_uv
        UV="$(find_uv)"
        [ -n "$UV" ] || die "uv was installed but was not found in $UV_INSTALL_DIR."
    fi
    echo "${C_GREEN}Using $("$UV" --version) : $UV${C_OFF}"
}

update_submodules() {
    [ "$UPDATE_SUBMODULES" = true ] || return 0
    step "Updating Git submodules..."
    if [ ! -f .gitmodules ]; then
        warn "No .gitmodules file found, skipping."
        return 0
    fi
    command -v git >/dev/null 2>&1 || die "git was not found in PATH."
    if [ ${#SUBMODULE_PATHS[@]} -gt 0 ]; then
        git submodule update --progress --init --recursive --force -- "${SUBMODULE_PATHS[@]}"
    else
        git submodule update --progress --init --recursive --force
    fi
}

create_links() {
    [ ${#LINKS[@]} -gt 0 ] || return 0
    step "Creating links..."
    local entry link target link_dir rel
    for entry in "${LINKS[@]}"; do
        link="${entry%%:*}"
        target="${entry#*:}"
        [ -n "$link" ] && [ -n "$target" ] && [ "$link" != "$entry" ] || die "Invalid LINKS entry '$entry' (expected 'link:target')."
        [ -e "$target" ] || die "Link target '$target' was not found. Is the submodule missing? (set UPDATE_SUBMODULES=true)"

        # Remove an existing link, but never a real directory or file.
        if [ -L "$link" ]; then
            rm -f "$link"
        elif [ -e "$link" ]; then
            die "'$link' exists and is not a symlink, refusing to replace it. Remove it manually."
        fi

        link_dir="$(dirname "$link")"
        mkdir -p "$link_dir"

        # Use a relative symlink so the repository can be moved.
        if rel="$(realpath --relative-to="$link_dir" "$target" 2>/dev/null)"; then :; else
            rel="$REPO_ROOT/$target"
        fi
        echo "Linking $link -> $rel"
        ln -s "$rel" "$link"
    done
}

# Finds the pip configuration (next to this script first, then in the
# repository root) and stores its path in PIP_CONFIG_PATH.
PIP_CONFIG_PATH=""
check_pip_config() {
    local dir
    for dir in "$SCRIPT_DIR" "$REPO_ROOT"; do
        if [ -f "$dir/$PIP_CONFIG" ]; then PIP_CONFIG_PATH="$dir/$PIP_CONFIG"; return 0; fi
    done
    die "Pip configuration '$PIP_CONFIG' was not found. Copy the default pip.ini next to this script."
}

# Reads index-url, extra-index-url and trusted-host from the pip.ini file
# (any section, multi-line values supported) and fills the UV_INDEX_ARGS array.
UV_INDEX_ARGS=()
read_uv_index_args() {
    local kind value has_extra=false
    UV_INDEX_ARGS=()
    while read -r kind value; do
        case "$kind" in
            index-url)       UV_INDEX_ARGS+=(--index-url "$value") ;;
            extra-index-url) UV_INDEX_ARGS+=(--extra-index-url "$value"); has_extra=true ;;
            trusted-host)    UV_INDEX_ARGS+=(--allow-insecure-host "$value") ;;
        esac
    done < <(tr -d '\r' < "$PIP_CONFIG_PATH" | awk '
        /^[ \t]*([#;].*)?$/   { next }                       # blank line or comment
        /^[ \t]*\[.*\][ \t]*$/ { key = ""; next }            # [section]
        /^[ \t]/              { if (key != "") for (i = 1; i <= NF; i++) print key, $i; next }
        {
            pos = match($0, /[=:]/); if (!pos) { key = ""; next }
            name = tolower(substr($0, 1, pos - 1)); gsub(/[ \t]/, "", name); gsub(/_/, "-", name)
            if (name != "index-url" && name != "extra-index-url" && name != "trusted-host") { key = ""; next }
            key = name; n = split(substr($0, pos + 1), parts, /[ \t]+/)
            for (i = 1; i <= n; i++) if (parts[i] != "") { print key, parts[i]; if (key == "index-url") key = "" }
        }')
    # Like pip: pick the best matching version across all indexes.
    if [ "$has_extra" = true ]; then UV_INDEX_ARGS+=(--index-strategy unsafe-best-match); fi
}

# Copies pip.ini into the venv, so plain 'pip' inside the venv uses the same indexes.
# pip reads pip.conf on Linux/macOS and pip.ini on Windows (Git Bash).
copy_pip_config() {
    local venv="$1"
    if [ -d "$venv/Scripts" ]; then cp -f "$PIP_CONFIG_PATH" "$venv/pip.ini"; else cp -f "$PIP_CONFIG_PATH" "$venv/pip.conf"; fi
}

# Creates the venv with the requested Python; uv downloads it if it's not installed.
# --seed puts pip into the venv, so plain 'pip' / 'python -m pip' keep working.
create_venv() {
    local request="${PYTHON_ARG:-$PYTHON_VERSION}" label="the default Python"
    [ -n "$request" ] && label="Python $request"
    step "Creating the virtual environment ($VENV_DIR) with $label..."
    echo "uv downloads this Python if no matching installation is found."

    local args=(venv "$VENV_DIR" --seed --python-preference "$PYTHON_PREFERENCE")
    [ -n "$request" ] && args+=(--python "$request")
    read_uv_index_args
    "$UV" "${args[@]}" ${UV_INDEX_ARGS[@]+"${UV_INDEX_ARGS[@]}"}
}

uv_pip_install() {
    local venv_python="$1"; shift
    read_uv_index_args
    "$UV" pip install --python "$venv_python" "$@" ${UV_INDEX_ARGS[@]+"${UV_INDEX_ARGS[@]}"}
}

install_requirements() {
    local venv_python="$1" upgrade="$2" req
    for req in ${REQUIREMENTS_FILES[@]+"${REQUIREMENTS_FILES[@]}"}; do
        if [ ! -f "$req" ]; then
            warn "Requirements file '$req' not found, skipping."
            continue
        fi
        step "Installing $req..."
        if [ "$upgrade" = true ]; then
            uv_pip_install "$venv_python" --upgrade -r "$req"
        else
            uv_pip_install "$venv_python" -r "$req"
        fi
    done
}

install_editable() {
    local venv_python="$1"
    [ "$INSTALL_EDITABLE" = true ] || return 0
    [ -f setup.py ] || [ -f pyproject.toml ] || die "INSTALL_EDITABLE is enabled but neither setup.py nor pyproject.toml was found."
    step "Installing $APP_NAME in editable mode..."
    uv_pip_install "$venv_python" -e .
}

# ============================================================
# MAIN
# ============================================================

echo "${C_GREEN}Installing $APP_NAME${C_OFF}"
check_pip_config   # fail early, before anything is changed
if [ -n "$PYTHON_INSTALL_MIRROR" ]; then export UV_PYTHON_INSTALL_MIRROR="$PYTHON_INSTALL_MIRROR"; fi

# --- uv ---
get_uv

# --- Submodules and links ---
update_submodules
create_links

# --- Virtual environment ---
if [ -e "$VENV_DIR" ]; then
    step "Removing the existing virtual environment ($VENV_DIR)..."
    if [ -n "${VIRTUAL_ENV:-}" ] && [ "$(abs_path "$VIRTUAL_ENV")" = "$(abs_path "$VENV_DIR")" ]; then
        die "The virtual environment is active in this shell. Run 'deactivate' first."
    fi
    rm -rf "$VENV_DIR"
fi

create_venv || die "Could not create the virtual environment."

# Linux/macOS use bin/, Python on Windows (Git Bash) uses Scripts/.
if [ -x "$VENV_DIR/bin/python" ]; then
    VENV_BIN="$VENV_DIR/bin"
    VENV_PYTHON="$VENV_BIN/python"
elif [ -f "$VENV_DIR/Scripts/python.exe" ]; then
    VENV_BIN="$VENV_DIR/Scripts"
    VENV_PYTHON="$VENV_BIN/python.exe"
else
    die "Virtual environment Python not found in $VENV_DIR."
fi
PY_VERSION="$(python_version "$VENV_PYTHON")"
[ -n "$PY_VERSION" ] || die "The virtual environment's Python is not working: $VENV_PYTHON"
echo "${C_GREEN}Virtual environment Python $PY_VERSION${C_OFF}"

# activate.sh in the repository root (symlink to the venv's activate script).
# The activate script stores the venv path itself, so sourcing the symlink works.
rm -f activate.sh
ln -s "$VENV_BIN/activate" activate.sh 2>/dev/null || cp "$VENV_BIN/activate" activate.sh

# --- Packages ---
copy_pip_config "$VENV_DIR"
install_requirements "$VENV_PYTHON" false
install_editable "$VENV_PYTHON"

# ============================================================
# POST-INSTALLATION
# ============================================================

echo ""
echo "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@"
echo ""
echo "${C_GREEN}$APP_NAME installation complete.${C_OFF}"
echo ""
echo "Run 'source activate.sh' to activate $APP_NAME's Python virtual environment"
echo "and 'deactivate' to leave it."
echo ""
echo "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@"

exit 0
