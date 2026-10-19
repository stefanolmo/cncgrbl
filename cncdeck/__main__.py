import argparse
import os
import signal
import sys

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from . import __version__, theme
from .mainwindow import APP_NAME, MainWindow
from .splash import SplashScreen


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cncdeck", description="Sender G-code per CNC GRBL 1.1")
    ap.add_argument("file", nargs="?", help="file G-code da aprire")
    ap.add_argument("--sim", action="store_true", help="collegati al simulatore GRBL interno")
    ap.add_argument("--screenshot", metavar="PNG", help="(sviluppo) salva uno screenshot ed esci")
    ap.add_argument("--no-splash", action="store_true", help="non mostrare la schermata iniziale")
    args = ap.parse_args(argv)

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName("cncdeck")
    app.setStyleSheet(theme.STYLESHEET)
    f = QFont(app.font())
    f.setPointSize(10)
    app.setFont(f)
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    use_splash = not (args.no_splash or args.screenshot or os.environ.get("CNCDECK_NO_SPLASH"))
    splash = None
    if use_splash:
        splash = SplashScreen(__version__)     # 3 s; shown first, the main window is prepared behind it
        splash.start()
        app.processEvents()
    w = MainWindow()
    if splash is None:
        w.show()
    else:
        splash.finished.connect(w.show)
        splash.finished.connect(w.raise_)
    if args.file:
        w.load_file(os.path.abspath(args.file))
    if args.sim or args.screenshot:
        i = w.port_combo.findData("simulator")
        w.port_combo.setCurrentIndex(i)
        w.toggle_connection()
    if args.screenshot:
        QTimer.singleShot(int(os.environ.get("CNCDECK_SHOT_DELAY", "2500")), lambda: (w.grab().save(args.screenshot), setattr(w, '_running', False), w.close(), app.quit()))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
