#!/bin/bash
# Double-click to start AutoQSL. Close this window to stop it.
cd "$(dirname "$0")"

# First run: create a private Python environment with the needed libraries
if [ ! -x ".venv/bin/python" ]; then
    echo "First run: setting up Python environment (takes about a minute)..."
    python3 -m venv .venv || { echo "Could not create environment"; read -n 1 -s; exit 1; }
    .venv/bin/python -m pip install --quiet --upgrade pip
    .venv/bin/python -m pip install --quiet pillow requests || {
        rm -rf .venv; echo "Library install failed (check internet)"; read -n 1 -s; exit 1; }
    echo "Done."
    echo
fi

echo "AutoQSL is running. Keep this window open while you work on the air."
echo "To stop: close the window or press Ctrl+C"
echo
.venv/bin/python autoqsl.py "$@"
echo
read -n 1 -s -r -p "AutoQSL stopped. Press any key to close..."
