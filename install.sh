#!/usr/bin/env bash
# Installs CNC Deck for the current user on Ubuntu/Debian (venv in ~/.local/share/cncdeck).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PREFIX="$HOME/.local/share/cncdeck"

echo "==> System dependencies (needs sudo)"
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip \
    libxcb-cursor0 libxkbcommon-x11-0 libegl1 libgl1 libdbus-1-3 libfontconfig1

echo "==> Virtual environment and package"
python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --upgrade pip
"$PREFIX/venv/bin/pip" install "$HERE"

echo "==> Launcher and icon"
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications" "$HOME/.local/share/icons/hicolor/scalable/apps"
ln -sf "$PREFIX/venv/bin/cncdeck" "$HOME/.local/bin/cncdeck"
cp "$HERE/packaging/cncdeck.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/cncdeck.svg"
sed -e "s|@EXEC@|$PREFIX/venv/bin/cncdeck|" -e "s|@ICON@|cncdeck|" "$HERE/packaging/cncdeck.desktop" \
    > "$HOME/.local/share/applications/cncdeck.desktop"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true

echo "==> Serial port access"
if ! id -nG "$USER" | tr ' ' '\n' | grep -qx dialout; then
    sudo usermod -aG dialout "$USER"
    echo "   Added '$USER' to the dialout group: LOG OUT AND BACK IN to activate it."
fi
if dpkg -s brltty >/dev/null 2>&1; then
    echo "   WARNING: 'brltty' is installed and steals boards with a CH340 chip (/dev/ttyUSB0 disappears)."
    read -r -p "   Remove it now? [y/N] " a
    [[ "${a:-n}" =~ ^[yY]$ ]] && sudo apt-get remove -y brltty
fi
echo "Done. Start 'cncdeck' from the applications menu or from a terminal (try: cncdeck --sim)."
