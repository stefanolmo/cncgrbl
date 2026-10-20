#!/usr/bin/env bash
# Builds a Debian/Ubuntu package that uses the distribution's Python libraries (PySide6, numpy, pyserial, pyclipper).
# Only svgelements (tested version) is bundled, in a private directory. Output: dist/cncdeck_<version>_all.deb
#
#   packaging/build-deb.sh            # needs: bash, dpkg-deb, python3 with pip (internet, to fetch svgelements)
#   sudo apt install ./dist/cncdeck_<version>_all.deb
#   sudo apt remove cncdeck           # (or Synaptic) removes everything
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
VERSION="$(python3 - <<'PY'
import re
print(re.search(r'"([^"]+)"', open("cncdeck/__init__.py").read()).group(1))
PY
)"
SVGELEMENTS="1.9.6"
PKG="cncdeck_${VERSION}_all"
STAGE="$ROOT/build/deb/$PKG"
MAINT="Stefano Ciurleo <105057757+stefanolmo@users.noreply.github.com>"

rm -rf "$STAGE"
install -d "$STAGE/DEBIAN" "$STAGE/usr/bin" "$STAGE/usr/lib/python3/dist-packages" \
    "$STAGE/usr/share/cncdeck/vendor" "$STAGE/usr/share/applications" \
    "$STAGE/usr/share/icons/hicolor/scalable/apps" "$STAGE/usr/share/doc/cncdeck/examples" "$STAGE/usr/share/man/man1"

# application code (pure Python)
cp -r cncdeck "$STAGE/usr/lib/python3/dist-packages/cncdeck"
find "$STAGE" -name '__pycache__' -type d -prune -exec rm -rf {} +

# bundled svgelements (pure Python, MIT) in a private directory, put on sys.path by the launcher
python3 -m pip install --quiet --no-deps --no-compile --target "$STAGE/usr/share/cncdeck/vendor" "svgelements==$SVGELEMENTS"
rm -rf "$STAGE/usr/share/cncdeck/vendor/bin"
SVG_LICENSE="$(find "$STAGE/usr/share/cncdeck/vendor" -iname 'LICENSE*' | head -1 || true)"

cat > "$STAGE/usr/bin/cncdeck" <<'PY'
#!/usr/bin/python3
import sys

sys.path.insert(0, "/usr/share/cncdeck/vendor")
from cncdeck.__main__ import main

sys.exit(main())
PY
chmod 755 "$STAGE/usr/bin/cncdeck"

sed -e 's|^Exec=.*|Exec=cncdeck %f|' -e 's|^Icon=.*|Icon=cncdeck|' packaging/cncdeck.desktop > "$STAGE/usr/share/applications/cncdeck.desktop"
cp packaging/cncdeck.svg "$STAGE/usr/share/icons/hicolor/scalable/apps/cncdeck.svg"
cp examples/demo.nc examples/demo.svg examples/ellipse_cam.gcode "$STAGE/usr/share/doc/cncdeck/examples/"
cp README.md "$STAGE/usr/share/doc/cncdeck/README.md"
gzip -9n -c packaging/cncdeck.1 > "$STAGE/usr/share/man/man1/cncdeck.1.gz"

cat > "$STAGE/usr/share/doc/cncdeck/README.Debian" <<'TXT'
CNC Deck per Debian/Ubuntu
==========================

Porta seriale
  L'utente deve far parte del gruppo "dialout" per usare /dev/ttyUSB* e /dev/ttyACM*:
      sudo usermod -aG dialout $USER      (poi uscire e rientrare dalla sessione)
  Se la scheda con chip CH340 (CNC 1610 Pro) scompare subito dopo averla collegata, il servizio
  "brltty" la sta catturando:  sudo apt remove brltty

Prova senza macchina:  cncdeck --sim
Esempi: /usr/share/doc/cncdeck/examples/

Se hai installato CNC Deck anche con install.sh (in ~/.local), rimuovilo per evitare due copie:
  rm -rf ~/.local/share/cncdeck ~/.local/bin/cncdeck ~/.local/share/applications/cncdeck.desktop
TXT

cat > "$STAGE/usr/share/doc/cncdeck/copyright" <<TXT
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: cncdeck
Source: https://github.com/stefanolmo/cncgrbl

Files: *
Copyright: 2026 Stefano Ciurleo
License: MIT

Files: usr/share/cncdeck/vendor/svgelements/*
Copyright: Tatarize (svgelements)
License: MIT

License: MIT
 Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
 associated documentation files (the "Software"), to deal in the Software without restriction,
 including without limitation the rights to use, copy, modify, merge, publish, distribute,
 sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is
 furnished to do so, subject to the following conditions:
 .
 The above copyright notice and this permission notice shall be included in all copies or
 substantial portions of the Software.
 .
 THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT
 NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
 NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
 DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
TXT
[ -n "$SVG_LICENSE" ] && cp "$SVG_LICENSE" "$STAGE/usr/share/doc/cncdeck/LICENSE.svgelements"

{
  echo "cncdeck ($VERSION) stable; urgency=medium"
  echo
  echo "  * Pacchetto Debian/Ubuntu di CNC Deck $VERSION (dipendenze dalla distribuzione)."
  echo
  echo " -- $MAINT  $(date -R)"
} | gzip -9n > "$STAGE/usr/share/doc/cncdeck/changelog.gz"

cat > "$STAGE/DEBIAN/control" <<TXT
Package: cncdeck
Version: $VERSION
Section: electronics
Priority: optional
Architecture: all
Depends: python3 (>= 3.10), python3-pyside6.qtcore, python3-pyside6.qtgui, python3-pyside6.qtwidgets, python3-numpy, python3-serial, python3-pyclipper (>= 1.3)
Recommends: libxcb-cursor0, libxkbcommon-x11-0
Maintainer: $MAINT
Homepage: https://github.com/stefanolmo/cncgrbl
Installed-Size: $(du -sk "$STAGE" | cut -f1)
Description: sender G-code per CNC GRBL 1.1 con convertitore SVG
 CNC Deck invia file G-code a una CNC con GRBL 1.1 (pensato per la 1610 Pro)
 via seriale, con anteprima 3D, DRO, jog, probe Z, override e impostazioni
 della scheda.
 .
 Il convertitore integrato trasforma file SVG in G-code (operazioni esterno,
 interno, tasca, incisione e linguette di fissaggio, sul modello di jscut)
 e lo passa al sender con un clic.
TXT

cat > "$STAGE/DEBIAN/postinst" <<'SH'
#!/bin/sh
set -e
if command -v py3compile >/dev/null 2>&1; then py3compile -p cncdeck || true; fi
if command -v update-desktop-database >/dev/null 2>&1; then update-desktop-database -q /usr/share/applications || true; fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true; fi
exit 0
SH
cat > "$STAGE/DEBIAN/prerm" <<'SH'
#!/bin/sh
set -e
if command -v py3clean >/dev/null 2>&1; then py3clean -p cncdeck || true; fi
exit 0
SH
cat > "$STAGE/DEBIAN/postrm" <<'SH'
#!/bin/sh
set -e
if command -v update-desktop-database >/dev/null 2>&1; then update-desktop-database -q /usr/share/applications || true; fi
exit 0
SH
chmod 755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/prerm" "$STAGE/DEBIAN/postrm"

find "$STAGE" -type d -exec chmod 755 {} +
find "$STAGE" -type f ! -perm -111 -exec chmod 644 {} +
chmod 755 "$STAGE/usr/bin/cncdeck"

mkdir -p dist
dpkg-deb --root-owner-group --build "$STAGE" "dist/$PKG.deb" >/dev/null
echo "Creato dist/$PKG.deb ($(du -h "dist/$PKG.deb" | cut -f1))"
