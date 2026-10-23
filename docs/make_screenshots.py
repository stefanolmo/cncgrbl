"""Regenerates the README images in docs/ (runs offscreen, no machine needed):

    QT_QPA_PLATFORM=offscreen python3 docs/make_screenshots.py
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from PySide6.QtGui import QColor, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from cncdeck import __version__, cam, simulator, theme  # noqa: E402
from cncdeck.mainwindow import MainWindow  # noqa: E402
from cncdeck.splash import SplashScreen  # noqa: E402

app = QApplication([])
app.setStyleSheet(theme.STYLESHEET)


def pump(seconds):
    t = time.time()
    while time.time() - t < seconds:
        app.processEvents()
        time.sleep(0.01)


# 1) splash (frozen at the moment the whole toolpath is drawn), transparent corners
s = SplashScreen(__version__)
s.show()
s.set_time(2300)
pm = QPixmap(s.size())
pm.fill(QColor(0, 0, 0, 0))
s.render(pm)
pm.save(os.path.join(HERE, "splash.png"))
s.close()

# 2) sender: the demo job running against the built-in GRBL simulator
orig = simulator.SimulatedSerial.__init__
simulator.SimulatedSerial.__init__ = lambda self, speed=14.0, boot_delay=0.1: orig(self, speed=speed, boot_delay=boot_delay)
w = MainWindow()
w.resize(1500, 1060)
w.show()
w.load_file(os.path.join(ROOT, "examples", "demo.nc"))
w.port_combo.setCurrentIndex(w.port_combo.findData("simulator"))
w.toggle_connection()
t0 = time.time()
while not (w.connected and w.status.state == "Idle") and time.time() - t0 < 10:
    pump(0.05)
w.run_program()
pump(8.0)
w.grab().save(os.path.join(HERE, "sender.png"))
w._running = False
w.close()

# 3) converter: SVG -> G-code with four operations, the last one with holding tabs
w = MainWindow()
w.resize(1500, 1060)
w.show()
w.set_mode(1)
c = w.converter
c.ops = [
    cam.Operation(name="Hole", kind="pocket", shapes={"hole"}, cut_depth=2, pass_depth=0.7),
    cam.Operation(name="Star", kind="pocket", shapes={"star"}, cut_depth=1.0, pass_depth=0.5),
    cam.Operation(name="Wave", kind="engrave", shapes={"wave"}, cut_depth=0.4, pass_depth=0.4),
    cam.Operation(name="Cut out part", kind="outside", shapes={"outline"}, cut_depth=3, pass_depth=1.5,
                  tab_mode="auto", tab_count=6, tab_width=5, tab_height=1.0),
]
c._refresh_ops_list(3)
c._load_op_widgets()
c.load_svg(os.path.join(ROOT, "examples", "demo.svg"))
pump(3.5)
w.grab().save(os.path.join(HERE, "converter.png"))
w.close()
print("ok")
