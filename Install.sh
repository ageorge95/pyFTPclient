#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

PYTHON="${1:-python3}"

"$PYTHON" -m venv venv
ln -sf venv/bin/activate activate

source ./activate

python install_helper.py

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

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

deactivate
