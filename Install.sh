#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1

# --- Configuration ---
PYTHON_UTIL_SCRIPT="install_helper.py"
DEFAULT_PYTHON_CMD="python3"

# Determine the Python command to use for running the utility script
# If first argument is --headless skip and continue with the next one
if [ "$1" = "--headless" ]; then
    HEADLESS=true
    HELPER_PYTHON="$2"
else
    HEADLESS=false
    HELPER_PYTHON="$1"
fi
HELPER_PYTHON="${HELPER_PYTHON:-$DEFAULT_PYTHON_CMD}"
echo "Using helper Python: $HELPER_PYTHON"

error_exit() {
    echo ""
    echo "!!!!! INSTALLATION FAILED !!!!!"
    if [ "$HEADLESS" != true ]; then
        read -r -p "Press Enter to continue..."
    fi
    exit 1
}

# Find the highest available Python version using the utility script
echo ""
echo "--- Searching for the highest suitable Python installation ---"
TARGET_PYTHON_EXE=""
while IFS=" " read -r key value; do
    if [ "$key" = "Path:" ]; then
        TARGET_PYTHON_EXE="$value"
        echo "Found recommended Python: $TARGET_PYTHON_EXE"
    fi
done < <("$HELPER_PYTHON" "$PYTHON_UTIL_SCRIPT" find_highest)

# If no specific Python was found, fall back to the helper Python
if [ -z "$TARGET_PYTHON_EXE" ]; then
    echo "No specific Python version found by the script. Falling back to helper Python: $HELPER_PYTHON"
    TARGET_PYTHON_EXE="$HELPER_PYTHON"
fi

# Check if the TARGET_PYTHON_EXE version is valid using the utility script
echo ""
echo "--- Validating Python version ($TARGET_PYTHON_EXE) ---"
if ! "$TARGET_PYTHON_EXE" "$PYTHON_UTIL_SCRIPT" check_current; then
    echo "ERROR: The Python version $TARGET_PYTHON_EXE is invalid."
    echo "Please check the output from $PYTHON_UTIL_SCRIPT above."
    echo "Installation cannot proceed."
    error_exit
fi
echo "Python version $TARGET_PYTHON_EXE is acceptable."
echo "Using Python for venv: $TARGET_PYTHON_EXE"

# installation
"$TARGET_PYTHON_EXE" -m venv venv || error_exit
ln -sf venv/bin/activate activate

# shellcheck disable=SC1091
source ./activate

python -m pip install --upgrade pip
python -m pip install -r requirements.txt || error_exit

echo "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@"
echo
echo "pyFTPclient install complete."
echo
echo "Run 'source activate' to activate pyFTPclient's Python virtual environment and"
echo "'deactivate' to, well, deactivate it."
echo
echo "Note: on some distros Qt needs extra system libs, e.g. on Debian/Ubuntu:"
echo "  sudo apt install libxcb-cursor0 libegl1"
echo
echo "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@"

# skip pause if first argument is --headless
if [ "$HEADLESS" != true ]; then
    read -r -p "Press Enter to continue..."
fi
deactivate
exit 0
