#!/bin/bash
# Starts docpipe on Linux: run  ./docpipe.sh
# The first start sets everything up automatically (needs internet once).
cd "$(dirname "$0")" || exit 1

py_ok() { "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; }

if [ -x .venv/bin/python ] && ! py_ok .venv/bin/python; then
    echo "The tool's environment was built with an old Python; rebuilding it..."
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
        echo "This tool needs Python 3.10 or newer. Install it with your package"
        echo "manager, e.g.  sudo apt install python3 python3-venv  , then run this again."
        exit 1
    fi
    echo "First-time setup: preparing the tool (needs internet, a few minutes)..."
    if ! "$PYBIN" -m venv .venv; then
        echo "Could not create a virtual environment. On Debian/Ubuntu run:"
        echo "  sudo apt install python3-venv"
        exit 1
    fi
fi

if ! .venv/bin/python -c "import click, markitdown, google.genai" >/dev/null 2>&1; then
    echo "Installing the tool's components (needs internet)..."
    .venv/bin/python -m pip install --upgrade pip >/dev/null 2>&1
    .venv/bin/python -m pip install -r requirements.txt || {
        echo "Installation failed: either no internet connection, or your Python"
        echo "is too new for some components ('no matching distribution' above)."
        echo "In the latter case install Python 3.12/3.13, delete .venv, and rerun."
        exit 1
    }
fi

PYTHONPATH="$PWD/src" exec .venv/bin/python -m docpipe "$@"
