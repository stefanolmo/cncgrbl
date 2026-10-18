"""Dark theme and procedurally drawn vector icons."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

BG = "#0e1217"
PANEL = "#141a21"
CARD = "#1a222c"
CARD_HI = "#222c38"
BORDER = "#2a3544"
TEXT = "#d9e1ea"
MUTED = "#7f8d9f"
ACCENT = "#2dd4bf"
ACCENT_DIM = "#14766b"
OK = "#34d399"
RUN = "#4aa8ff"
WARN = "#fbbf24"
DANGER = "#f87171"
AXIS = {"X": "#f87171", "Y": "#4ade80", "Z": "#60a5fa"}
MONO = "'JetBrains Mono','Ubuntu Mono','DejaVu Sans Mono','Liberation Mono',monospace"

STATE_COLORS = {
    "Idle": OK, "Run": RUN, "Jog": RUN, "Home": RUN, "Check": "#a78bfa", "Hold": WARN,
    "Door": WARN, "Alarm": DANGER, "Sleep": MUTED, "Disconnected": MUTED, "Connecting": WARN,
}

STYLESHEET = f"""
* {{ font-size: 13px; }}
QWidget {{ background: {BG}; color: {TEXT}; }}
QMainWindow, QDialog {{ background: {BG}; }}
QToolTip {{ background: {CARD_HI}; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px; }}
QFrame#card {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 10px; }}
QFrame#card QLabel, QFrame#card QCheckBox, QFrame#card QSlider {{ background: transparent; }}
QLabel#cardTitle {{ color: {MUTED}; font-size: 11px; font-weight: 700; letter-spacing: 1.5px; }}
QLabel#muted {{ color: {MUTED}; }}
QLabel#mono {{ font-family: {MONO}; }}
QPushButton, QToolButton {{
    background: {CARD_HI}; border: 1px solid {BORDER}; border-radius: 7px; padding: 6px 12px; color: {TEXT};
}}
QPushButton:hover, QToolButton:hover {{ background: #2c3847; border-color: #3c4b5f; }}
QPushButton:pressed, QToolButton:pressed {{ background: {ACCENT_DIM}; border-color: {ACCENT}; }}
QPushButton:disabled, QToolButton:disabled {{ color: #4d5866; background: #171e26; border-color: #202a36; }}
QPushButton#primary {{ background: {ACCENT}; color: #04211d; border: none; font-weight: 700; }}
QPushButton#primary:hover {{ background: #5eead4; }}
QPushButton#primary:disabled {{ background: #1c3a37; color: #4b7a74; }}
QPushButton#danger {{ background: #5b1f24; border-color: #8f3038; }}
QPushButton#danger:hover {{ background: #7a2a31; }}
QPushButton:checked, QToolButton:checked {{ background: {ACCENT_DIM}; border-color: {ACCENT}; }}
QToolButton#run {{ background: #14532d; border-color: #1f7a45; }}
QToolButton#run:hover {{ background: #1a6b3b; }}
QToolButton#run:disabled {{ background: #17221d; border-color: #1f2d26; }}
QToolButton#stopb {{ background: #5b1f24; border-color: #8f3038; }}
QToolButton#stopb:hover {{ background: #7a2a31; }}
QToolButton#stopb:disabled {{ background: #231a1c; border-color: #31232a; }}
QToolButton#jog {{ padding: 0; border-radius: 9px; background: #232e3b; }}
QToolButton#jog:pressed {{ background: {ACCENT}; }}
QComboBox, QLineEdit, QDoubleSpinBox, QSpinBox {{
    background: #10161d; border: 1px solid {BORDER}; border-radius: 6px; padding: 4px 8px;
    selection-background-color: {ACCENT_DIM};
}}
QComboBox:focus, QLineEdit:focus, QDoubleSpinBox:focus, QSpinBox:focus {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {CARD}; border: 1px solid {BORDER}; selection-background-color: {ACCENT_DIM}; outline: 0; }}
QSlider::groove:horizontal {{ height: 4px; background: {BORDER}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {TEXT}; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; }}
QPlainTextEdit, QTextEdit, QTableWidget {{
    background: #0b0f14; border: 1px solid {BORDER}; border-radius: 8px;
    font-family: {MONO}; font-size: 12px; selection-background-color: {ACCENT_DIM};
}}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 8px; top: -1px; }}
QTabBar::tab {{ background: transparent; color: {MUTED}; padding: 7px 16px; border: none; border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {ACCENT}; }}
QTabBar::tab:hover {{ color: {TEXT}; }}
QHeaderView::section {{ background: {CARD}; color: {MUTED}; border: none; border-bottom: 1px solid {BORDER}; padding: 5px; }}
QTableWidget {{ gridline-color: #1c2530; alternate-background-color: #0e141a; }}
QProgressBar {{ background: #10161d; border: 1px solid {BORDER}; border-radius: 6px; text-align: center; height: 16px; color: {TEXT}; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 5px; }}
QStatusBar {{ background: {PANEL}; border-top: 1px solid {BORDER}; }}
QStatusBar::item {{ border: none; }}
QToolBar {{ background: {PANEL}; border: none; border-bottom: 1px solid {BORDER}; spacing: 6px; padding: 6px 8px; }}
QToolBar QToolButton {{ padding: 6px 10px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: #2c3847; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #3d4d60; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle:horizontal {{ background: #2c3847; border-radius: 5px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid {BORDER}; background: #10161d; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QMenu {{ background: {CARD}; border: 1px solid {BORDER}; padding: 4px; }}
QMenu::item {{ padding: 6px 22px; border-radius: 4px; }}
QMenu::item:selected {{ background: {ACCENT_DIM}; }}
QSplitter::handle {{ background: {BG}; }}
QMessageBox {{ background: {CARD}; }}
"""


def _icon(draw, color: str = TEXT, size: int = 64) -> QIcon:
    icon = QIcon()
    for col, mode in ((color, QIcon.Normal), ("#4d5866", QIcon.Disabled)):
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(col), size / 11, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.scale(size / 24, size / 24)
        pen.setWidthF(2.2)
        p.setPen(pen)
        draw(p, QColor(col))
        p.end()
        icon.addPixmap(pm, mode)
    return icon


def _poly(p, pts, close=False, fill=None):
    path = QPainterPath(QPointF(*pts[0]))
    for pt in pts[1:]:
        path.lineTo(*pt)
    if close:
        path.closeSubpath()
    if fill is not None:
        p.fillPath(path, fill)
    else:
        p.drawPath(path)


def _play(p, c): _poly(p, [(7, 4.5), (19, 12), (7, 19.5)], True, c)
def _pause(p, c): p.fillRect(QRectF(6, 5, 4, 14), c); p.fillRect(QRectF(14, 5, 4, 14), c)
def _stop(p, c): p.fillRect(QRectF(6, 6, 12, 12), c)
def _open(p, c): _poly(p, [(3, 7), (3, 19), (19, 19), (21, 11), (8, 11), (6, 19)]); _poly(p, [(3, 7), (9, 7), (11, 9), (18, 9), (18, 11)])
def _home(p, c): _poly(p, [(3, 12), (12, 4), (21, 12)]); _poly(p, [(6, 10.5), (6, 20), (18, 20), (18, 10.5)]); _poly(p, [(10, 20), (10, 14), (14, 14), (14, 20)])
def _unlock(p, c): p.drawRoundedRect(QRectF(5, 11, 14, 9), 2, 2); p.drawArc(QRectF(8, 3, 8, 10), 0, 180 * 16); _poly(p, [(16, 8), (16, 11)])
def _reset(p, c): p.drawArc(QRectF(5, 5, 14, 14), 40 * 16, 280 * 16); _poly(p, [(17, 2.5), (18.5, 7), (14, 7.5)], True, c)
def _gear(p, c):
    p.drawEllipse(QPointF(12, 12), 3, 3)
    import math
    for i in range(8):
        a = i * math.pi / 4
        p.drawLine(QPointF(12 + 6.5 * math.cos(a), 12 + 6.5 * math.sin(a)), QPointF(12 + 9 * math.cos(a), 12 + 9 * math.sin(a)))
    p.drawEllipse(QPointF(12, 12), 6.5, 6.5)
def _probe(p, c): _poly(p, [(12, 3), (12, 15)]); _poly(p, [(8, 11), (12, 16), (16, 11)]); _poly(p, [(4, 20), (20, 20)])
def _check(p, c): _poly(p, [(4, 12.5), (9.5, 18), (20, 6)])
def _up(p, c): _poly(p, [(12, 19), (12, 6)]); _poly(p, [(6, 11), (12, 5), (18, 11)])
def _down(p, c): _poly(p, [(12, 5), (12, 18)]); _poly(p, [(6, 13), (12, 19), (18, 13)])
def _left(p, c): _poly(p, [(19, 12), (6, 12)]); _poly(p, [(11, 6), (5, 12), (11, 18)])
def _right(p, c): _poly(p, [(5, 12), (18, 12)]); _poly(p, [(13, 6), (19, 12), (13, 18)])
def _target(p, c): p.drawEllipse(QPointF(12, 12), 6, 6); _poly(p, [(12, 2), (12, 7)]); _poly(p, [(12, 17), (12, 22)]); _poly(p, [(2, 12), (7, 12)]); _poly(p, [(17, 12), (22, 12)])
def _spindle(p, c): p.drawEllipse(QPointF(12, 12), 7, 7); p.drawEllipse(QPointF(12, 12), 1.5, 1.5); _poly(p, [(12, 5), (12, 10.5)]); _poly(p, [(18, 15.5), (13.3, 12.8)]); _poly(p, [(6, 15.5), (10.7, 12.8)])
def _save(p, c): _poly(p, [(5, 4), (17, 4), (20, 7), (20, 20), (5, 20)], True); _poly(p, [(8, 4), (8, 9), (16, 9), (16, 4)]); _poly(p, [(8, 20), (8, 14), (16, 14), (16, 20)])
def _reload(p, c): p.drawArc(QRectF(4, 4, 16, 16), 30 * 16, 300 * 16); _poly(p, [(17.5, 1.5), (20, 7), (14.5, 7.5)], True, c)
def _hold(p, c): _pause(p, c)
def _skip(p, c): _poly(p, [(5, 5), (13, 12), (5, 19)], True, c); p.fillRect(QRectF(15, 5, 3.5, 14), c)
def _cube(p, c): _poly(p, [(12, 3), (20, 7.5), (20, 16.5), (12, 21), (4, 16.5), (4, 7.5)], True); _poly(p, [(4, 7.5), (12, 12), (20, 7.5)]); _poly(p, [(12, 12), (12, 21)])
def _fit(p, c): _poly(p, [(4, 9), (4, 4), (9, 4)]); _poly(p, [(15, 4), (20, 4), (20, 9)]); _poly(p, [(20, 15), (20, 20), (15, 20)]); _poly(p, [(9, 20), (4, 20), (4, 15)])


_FUNCS = {n[1:]: f for n, f in globals().items() if n.startswith("_") and callable(f) and n[1:] in (
    "play pause stop open home unlock reset gear probe check up down left right target spindle save reload skip cube fit hold").split()}


def icon(name: str, color: str = TEXT) -> QIcon:
    return _icon(_FUNCS[name], color)
