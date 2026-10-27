#!/usr/bin/env bash
# Install the official FreeCAD AppImage (extracted) with a desktop launcher.
# The snap build is strictly confined and cannot start the external engine.
set -euo pipefail
VER="${1:-1.1.4}"
NAME="FreeCAD_${VER}-Linux-x86_64-py311.AppImage"
APPS="$HOME/Applications"
DL="$APPS/dl-freecad-$VER"
mkdir -p "$DL"
cd "$DL"
base="https://github.com/FreeCAD/FreeCAD/releases/download/$VER"
[ -f "$NAME" ] || curl -fSL -O "$base/$NAME"
curl -fsSL -O "$base/$NAME-SHA256.txt"
sha256sum -c "$NAME-SHA256.txt"
chmod +x "$NAME"
rm -rf squashfs-root "$APPS/FreeCAD-$VER"
"./$NAME" --appimage-extract >/dev/null
mv squashfs-root "$APPS/FreeCAD-$VER"
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications"
ln -sfn "$APPS/FreeCAD-$VER/AppRun" "$HOME/.local/bin/freecad-$VER"
cat > "$HOME/.local/share/applications/freecad-$VER.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=FreeCAD $VER
Comment=FreeCAD $VER (AppImage, with BigStep addon)
Exec=$APPS/FreeCAD-$VER/AppRun freecad %F
Icon=$APPS/FreeCAD-$VER/org.freecad.FreeCAD.svg
Terminal=false
Categories=Graphics;Engineering;
MimeType=application/x-extension-fcstd;model/step;
DESK
echo "FreeCAD $VER installed: $HOME/.local/bin/freecad-$VER"
