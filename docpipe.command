#!/bin/bash
# Starts docpipe on Mac. Double-click this file.
# The first start sets everything up automatically (needs internet once).
# If Mac says it "cannot be opened", right-click it, choose Open, then Open again.
cd "$(dirname "$0")" || exit 1

finish() { echo; read -r -p " Press Enter to close..."; exit "$1"; }

py_ok() { "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; }

# If the environment was built with a too-old Python, rebuild it.
if [ -x .venv/bin/python ] && ! py_ok .venv/bin/python; then
    echo " The tool's environment was built with an old Python; rebuilding it..."
    rm -rf .venv
fi

if [ ! -x .venv/bin/python ]; then
    # Prefer slightly older Pythons: the newest release is often not yet
    # supported by all packages.
    PYBIN=""
    for cand in python3.13 python3.12 python3.11 python3.10 python3 python3.14 python3.15; do
        if command -v "$cand" >/dev/null 2>&1 && py_ok "$cand"; then
            PYBIN="$cand"
            break
        fi
    done
    if [ -z "$PYBIN" ]; then
        echo " This tool needs Python 3.10 or newer, which was not found on this Mac."
        echo " (The Python that comes with macOS is too old.)"
        echo
        echo "   1. Go to https://www.python.org/downloads/ and download Python."
        echo "   2. Run the installer."
        echo "   3. Double-click this file again."
        finish 1
    fi
    echo " First-time setup: preparing the tool. This needs internet"
    echo " and can take a few minutes..."
    if ! "$PYBIN" -m venv .venv; then
        echo " Could not prepare the tool's environment. Try reinstalling Python."
        finish 1
    fi
fi

if ! .venv/bin/python -c "import click, markitdown, google.genai" >/dev/null 2>&1; then
    echo " Installing the tool's components (needs internet)..."
    .venv/bin/python -m pip install --upgrade pip >/dev/null 2>&1
    if ! .venv/bin/python -m pip install -r requirements.txt; then
        echo
        echo " Installation failed. The two usual causes:"
        echo "   - No internet connection: connect and double-click this file again."
        echo "   - Your Python is so new that some components do not support it yet"
        echo "     (the messages above would mention 'no matching distribution' or"
        echo "     'conflicting dependencies'). Install Python 3.12 or 3.13 from"
        echo "     https://www.python.org/downloads/ , delete the .venv folder"
        echo "     inside this tool's folder, and double-click this file again."
        finish 1
    fi
    echo " Components installed."
    echo
fi

PYTHONPATH="$PWD/src" .venv/bin/python -m docpipe "$@"
finish $?
