#!/usr/bin/env bash
# Link this repository into FreeCAD's user Mod folder the way the Addon Manager
# installs it: the repo root goes into Mod/, package.xml points at ./BigStep.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
MOD="${FREECAD_MOD_DIR:-$HOME/.local/share/FreeCAD/v1-1/Mod}"
mkdir -p "$MOD"
ln -sfn "$HERE" "$MOD/BigStep"
echo "linked $MOD/BigStep -> $HERE"
