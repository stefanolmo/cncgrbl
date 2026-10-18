#!/usr/bin/env bash
# Installa CNC Deck per l'utente corrente su Ubuntu/Debian (venv in ~/.local/share/cncdeck).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PREFIX="$HOME/.local/share/cncdeck"

echo "==> Dipendenze di sistema (richiede sudo)"
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip \
    libxcb-cursor0 libxkbcommon-x11-0 libegl1 libgl1 libdbus-1-3 libfontconfig1

echo "==> Ambiente virtuale e pacchetto"
python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --upgrade pip
"$PREFIX/venv/bin/pip" install "$HERE"

echo "==> Launcher e icona"
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications" "$HOME/.local/share/icons/hicolor/scalable/apps"
ln -sf "$PREFIX/venv/bin/cncdeck" "$HOME/.local/bin/cncdeck"
cp "$HERE/packaging/cncdeck.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/cncdeck.svg"
sed -e "s|@EXEC@|$PREFIX/venv/bin/cncdeck|" -e "s|@ICON@|cncdeck|" "$HERE/packaging/cncdeck.desktop" \
    > "$HOME/.local/share/applications/cncdeck.desktop"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true

echo "==> Accesso alla porta seriale"
if ! id -nG "$USER" | tr ' ' '\n' | grep -qx dialout; then
    sudo usermod -aG dialout "$USER"
    echo "   Aggiunto '$USER' al gruppo dialout: ESCI E RIENTRA dalla sessione per attivarlo."
fi
if dpkg -s brltty >/dev/null 2>&1; then
    echo "   ATTENZIONE: 'brltty' è installato e ruba le schede con chip CH340 (la porta /dev/ttyUSB0 sparisce)."
    read -r -p "   Rimuoverlo ora? [s/N] " a
    [[ "${a:-n}" =~ ^[sS]$ ]] && sudo apt-get remove -y brltty
fi
echo "Fatto. Avvia 'cncdeck' dal menu applicazioni o dal terminale (prova: cncdeck --sim)."
