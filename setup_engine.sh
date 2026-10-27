#!/usr/bin/env bash
# Create the OCCT 8 engine environment used by the BigStep addon.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
VENV="${1:-$HERE/.venv-occt8}"
OCP_VERSION="8.0.1.1.0"          # cadquery-ocp wheel = OCCT 8.0.1
python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q "cadquery-ocp==$OCP_VERSION"
"$VENV/bin/python" -E -s "$HERE/BigStep/engine/bigstep_engine.py" check
