"""Reusable widgets: cards, state badge, DRO, jog pad, overrides, G-code editor, console."""
from __future__ import annotations

import re

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import (QColor, QFont, QPainter, QSyntaxHighlighter, QTextCharFormat, QTextFormat,
                           QKeyEvent)
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
                               QPushButton, QTextEdit, QToolButton, QVBoxLayout, QWidget)

from . import theme
from .theme import icon


class Card(QFrame):
    def __init__(self, title: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(14, 12, 14, 14)
        self._lay.setSpacing(10)
        if title:
            t = QLabel(title.upper())
            t.setObjectName("cardTitle")
            self._lay.addWidget(t)
        self.body = QVBoxLayout()
        self.body.setSpacing(8)
        self._lay.addLayout(self.body)


class StateBadge(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumWidth(110)
        self.set_state("Disconnected")

    def set_state(self, state: str, text: str | None = None) -> None:
        base = state.split(":", 1)[0]
        col = theme.STATE_COLORS.get(base, theme.MUTED)
        labels = {"Disconnected": "Not connected", "Connecting": "Connecting…", "Idle": "Ready", "Run": "Running",
                  "Hold": "Paused", "Jog": "Jog", "Alarm": "ALARM", "Home": "Homing", "Check": "Check mode",
                  "Door": "Door open", "Sleep": "Sleep"}
        self.setText(text or labels.get(base, base))
        c = QColor(col)
        self.setStyleSheet(
            f"background: rgba({c.red()},{c.green()},{c.blue()},38); color: {col}; border: 1px solid {col};"
            f"border-radius: 13px; padding: 5px 14px; font-weight: 700; letter-spacing: 0.5px;")


class AxisRow(QWidget):
    zeroClicked = Signal(str)
    gotoClicked = Signal(str)

    def __init__(self, axis: str, parent=None):
        super().__init__(parent)
        self.axis = axis
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lab = QLabel(axis)
        lab.setFixedWidth(26)
        lab.setAlignment(Qt.AlignCenter)
        lab.setStyleSheet(f"color:{theme.AXIS[axis]}; font-size:20px; font-weight:800;")
        self.work = QLabel("0.000")
        self.work.setObjectName("mono")
        self.work.setStyleSheet(f"font-family:{theme.MONO}; font-size:26px; font-weight:600; color:{theme.TEXT};")
        self.work.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.work.setMinimumWidth(110)
        self.mach = QLabel("0.000")
        self.mach.setObjectName("muted")
        self.mach.setStyleSheet(f"font-family:{theme.MONO}; font-size:11px; color:{theme.MUTED};")
        self.mach.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.mach.setMinimumWidth(40)
        col = QVBoxLayout()
        col.setSpacing(0)
        col.addWidget(self.work)
        col.addWidget(self.mach)
        z = QToolButton()
        z.setText(f"{axis}0")
        z.setToolTip(f"Zero {axis} (set the work origin here)")
        z.setFixedSize(46, 36)
        z.setStyleSheet("padding: 0;")
        z.clicked.connect(lambda: self.zeroClicked.emit(axis))
        g = QToolButton()
        g.setIcon(icon("target"))
        g.setToolTip(f"Go to {axis}=0")
        g.setFixedSize(36, 36)
        g.setStyleSheet("padding: 0;")
        g.clicked.connect(lambda: self.gotoClicked.emit(axis))
        lay.addWidget(lab)
        lay.addLayout(col, 1)
        lay.addWidget(z)
        lay.addWidget(g)

    def set_values(self, w: float, m: float) -> None:
        self.work.setText(f"{w:.3f}")
        self.mach.setText(f"M {m:.3f}")


class JogPad(QWidget):
    """XY cross + Z column. Emits (dx, dy, dz) unit directions on press and `released` on release."""
    pressed = Signal(int, int, int)
    released = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(6)
        self.buttons: list[QToolButton] = []

        def mk(name, dx, dy, dz, r, c, tip):
            b = QToolButton()
            b.setObjectName("jog")
            b.setIcon(icon(name))
            b.setIconSize(QSize(26, 26))
            b.setFixedSize(52, 52)
            b.setAutoRepeat(False)
            b.setToolTip(tip)
            b.pressed.connect(lambda: self.pressed.emit(dx, dy, dz))
            b.released.connect(self.released.emit)
            grid.addWidget(b, r, c)
            self.buttons.append(b)
            return b

        mk("up", 0, 1, 0, 0, 1, "Y+  (↑)")
        mk("left", -1, 0, 0, 1, 0, "X−  (←)")
        mk("right", 1, 0, 0, 1, 2, "X+  (→)")
        mk("down", 0, -1, 0, 2, 1, "Y−  (↓)")
        c = QLabel("XY")
        c.setAlignment(Qt.AlignCenter)
        c.setObjectName("muted")
        grid.addWidget(c, 1, 1)
        mk("up", 0, 0, 1, 0, 4, "Z+  (Pag↑)")
        mk("down", 0, 0, -1, 2, 4, "Z−  (Pag↓)")
        z = QLabel("Z")
        z.setAlignment(Qt.AlignCenter)
        z.setObjectName("muted")
        grid.addWidget(z, 1, 4)
        grid.setColumnMinimumWidth(3, 14)


class OverrideBox(QWidget):
    """Feed/rapid/spindle override with ± and reset."""
    action = Signal(str)  # 'minus10','minus1','reset','plus1','plus10'

    def __init__(self, title: str, coarse=10, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        t = QLabel(title)
        t.setFixedWidth(64)
        self.value = QLabel("100%")
        self.value.setStyleSheet(f"font-family:{theme.MONO}; font-weight:700; color:{theme.ACCENT};")
        self.value.setFixedWidth(46)
        self.value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(t)
        lay.addWidget(self.value)
        for text, act in (("−10", "minus10"), ("−1", "minus1"), ("100", "reset"), ("+1", "plus1"), ("+10", "plus10")):
            b = QToolButton()
            b.setText(text)
            b.setFixedSize(34, 28)
            b.setStyleSheet("padding:0; font-size:11px;")
            b.clicked.connect(lambda _=False, a=act: self.action.emit(a))
            lay.addWidget(b)

    def set_value(self, v: int) -> None:
        self.value.setText(f"{v}%")


class RapidBox(QWidget):
    action = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        t = QLabel("Rapid")
        t.setFixedWidth(64)
        self.value = QLabel("100%")
        self.value.setStyleSheet(f"font-family:{theme.MONO}; font-weight:700; color:{theme.ACCENT};")
        self.value.setFixedWidth(46)
        self.value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(t)
        lay.addWidget(self.value)
        for text, act in (("25%", "r25"), ("50%", "r50"), ("100%", "r100")):
            b = QToolButton()
            b.setText(text)
            b.setFixedSize(54, 28)
            b.setStyleSheet("padding:0; font-size:11px;")
            b.clicked.connect(lambda _=False, a=act: self.action.emit(a))
            lay.addWidget(b)
        lay.addStretch()

    def set_value(self, v: int) -> None:
        self.value.setText(f"{v}%")


# ---------------- G-code editor ----------------

class GcodeHighlighter(QSyntaxHighlighter):
    def __init__(self, doc):
        super().__init__(doc)

        def fmt(color, bold=False, italic=False):
            f = QTextCharFormat()
            f.setForeground(QColor(color))
            if bold:
                f.setFontWeight(QFont.Bold)
            f.setFontItalic(italic)
            return f

        self.f_comment = fmt("#5b6b7d", italic=True)
        self.rules = [
            (re.compile(r"[Gg]\d+(\.\d+)?"), fmt("#c792ea", True)),
            (re.compile(r"[Mm]\d+"), fmt("#ffcb6b", True)),
            (re.compile(r"[Xx]\s*-?[\d.]+"), fmt(theme.AXIS["X"])),
            (re.compile(r"[Yy]\s*-?[\d.]+"), fmt(theme.AXIS["Y"])),
            (re.compile(r"[Zz]\s*-?[\d.]+"), fmt(theme.AXIS["Z"])),
            (re.compile(r"[IiJjKkRr]\s*-?[\d.]+"), fmt("#89ddff")),
            (re.compile(r"[Ff]\s*-?[\d.]+"), fmt("#f78c6c")),
            (re.compile(r"[Ss]\s*-?[\d.]+"), fmt("#c3e88d")),
            (re.compile(r"\$[\w=.+-]*"), fmt(theme.WARN)),
        ]
        self.comment = re.compile(r"\([^)]*\)|;.*$")

    def highlightBlock(self, text: str) -> None:
        for rx, f in self.rules:
            for m in rx.finditer(text):
                self.setFormat(m.start(), m.end() - m.start(), f)
        for m in self.comment.finditer(text):
            self.setFormat(m.start(), m.end() - m.start(), self.f_comment)


class _Gutter(QWidget):
    def __init__(self, editor: "CodeEditor"):
        super().__init__(editor)
        self.ed = editor

    def sizeHint(self):
        return QSize(self.ed.gutter_width(), 0)

    def paintEvent(self, e):
        self.ed.paint_gutter(e)


class CodeEditor(QPlainTextEdit):
    startFromLine = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        f = QFont()
        f.setFamilies(["JetBrains Mono", "Ubuntu Mono", "DejaVu Sans Mono", "Liberation Mono", "monospace"])
        f.setPointSize(10)
        f.setStyleHint(QFont.Monospace)
        self.setFont(f)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.gutter = _Gutter(self)
        self.highlighter = GcodeHighlighter(self.document())
        self.exec_line = -1
        self.sent_line = -1
        self.errors: set[int] = set()
        self.blockCountChanged.connect(lambda _: self.setViewportMargins(self.gutter_width(), 0, 0, 0))
        self.updateRequest.connect(self._update_gutter)
        self.setViewportMargins(self.gutter_width(), 0, 0, 0)

    def gutter_width(self) -> int:
        digits = max(3, len(str(self.blockCount())))
        return 18 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_gutter(self, rect, dy):
        if dy:
            self.gutter.scroll(0, dy)
        else:
            self.gutter.update(0, rect.y(), self.gutter.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self.setViewportMargins(self.gutter_width(), 0, 0, 0)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        cr = self.contentsRect()
        self.gutter.setGeometry(QRect(cr.left(), cr.top(), self.gutter_width(), cr.height()))

    def paint_gutter(self, event):
        p = QPainter(self.gutter)
        p.fillRect(event.rect(), QColor("#0e1319"))
        block = self.firstVisibleBlock()
        n = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                h = self.fontMetrics().height()
                if n == self.exec_line:
                    p.setPen(QColor(theme.ACCENT))
                    p.setFont(self.font())
                    p.drawText(0, top, self.gutter.width() - 6, h, Qt.AlignRight, f"▶{n + 1}")
                else:
                    col = theme.DANGER if n in self.errors else "#4d5b6c"
                    p.setPen(QColor(col))
                    p.drawText(0, top, self.gutter.width() - 6, h, Qt.AlignRight, str(n + 1))
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            n += 1

    def set_exec_line(self, n: int, follow: bool = True) -> None:
        self.exec_line = n
        sels = []
        if 0 <= n < self.blockCount():
            s = QTextEdit.ExtraSelection()
            s.format.setBackground(QColor(45, 212, 191, 40))
            s.format.setProperty(QTextFormat.FullWidthSelection, True)
            c = self.textCursor()
            c.setPosition(self.document().findBlockByNumber(n).position())
            s.cursor = c
            sels.append(s)
            if follow:
                tc = self.textCursor()
                tc.setPosition(self.document().findBlockByNumber(n).position())
                self.setTextCursor(tc)
                self.centerCursor()
        self.setExtraSelections(sels)
        self.gutter.update()

    def contextMenuEvent(self, e):
        menu = self.createStandardContextMenu()
        line = self.cursorForPosition(e.pos()).blockNumber()
        act = menu.addAction(f"Start from line {line + 1}…")
        act.triggered.connect(lambda: self.startFromLine.emit(line))
        menu.exec(e.globalPos())


# ---------------- console ----------------

class Console(QWidget):
    commandEntered = Signal(str)

    COLORS = {"tx": theme.ACCENT, "rx": "#aab6c4", "info": theme.RUN, "error": theme.DANGER,
              "alarm": theme.DANGER, "msg": theme.WARN}

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(3000)
        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("GRBL command or G-code, Enter to send (↑↓ history)")
        self.input.returnPressed.connect(self._submit)
        clear = QPushButton("Clear")
        clear.clicked.connect(self.log.clear)
        row.addWidget(self.input, 1)
        row.addWidget(clear)
        lay.addWidget(self.log, 1)
        lay.addLayout(row)
        self.history: list[str] = []
        self._hi = 0
        self.input.installEventFilter(self)

    def append(self, text: str, kind: str = "rx") -> None:
        col = self.COLORS.get(kind, "#aab6c4")
        prefix = {"tx": "› ", "error": "✖ ", "alarm": "⚠ ", "info": "ℹ ", "msg": "✱ "}.get(kind, "  ")
        esc = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self.log.appendHtml(f'<span style="color:{col}; white-space:pre;">{prefix}{esc}</span>')
        sb = self.log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _submit(self) -> None:
        t = self.input.text().strip()
        if not t:
            return
        self.history.append(t)
        self._hi = len(self.history)
        self.input.clear()
        self.commandEntered.emit(t)

    def eventFilter(self, obj, ev):
        if obj is self.input and isinstance(ev, QKeyEvent) and ev.type() == QKeyEvent.KeyPress:
            if ev.key() == Qt.Key_Up and self.history:
                self._hi = max(0, self._hi - 1)
                self.input.setText(self.history[self._hi])
                return True
            if ev.key() == Qt.Key_Down and self.history:
                self._hi = min(len(self.history), self._hi + 1)
                self.input.setText(self.history[self._hi] if self._hi < len(self.history) else "")
                return True
        return super().eventFilter(obj, ev)
