"""Main window: wires the controller, viewer and panels together."""
from __future__ import annotations

import os
import time

import numpy as np
from PySide6.QtCore import QEvent, QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFrame,
                               QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMainWindow, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSlider, QSpinBox,
                               QSplitter, QStackedWidget, QTabWidget, QToolBar, QToolButton, QVBoxLayout, QWidget)
from serial.tools import list_ports

from . import __version__, grbl, theme
from .gcode import Program, format_duration, parse_program, preamble_for
from .grbl import SIM_PORT, GrblConnection, Status
from .heightmap import HeightMap, HeightMapError, correct_program
from .probing import HeightMapProbe, ProbeParams
from .convertpage import ConverterPage
from .settings_dialog import GrblSettingsDialog
from .theme import icon
from .viewer import ToolpathView
from .widgets import (AxisRow, Card, CodeEditor, Console, JogPad, OverrideBox, RapidBox, StateBadge)

STEPS = [("0.01 mm", 0.01), ("0.1 mm", 0.1), ("1 mm", 1.0), ("5 mm", 5.0), ("10 mm", 10.0), ("50 mm", 50.0), ("Continuous", None)]
BAUDS = [115200, 57600, 38400, 19200, 9600, 250000]
APP_NAME = "CNC Deck"
TITLE = f"CNC Deck {__version__}"


class ParseThread(QThread):
    parsed = Signal(object, int)

    def __init__(self, text: str, rates, serial_no: int, parent=None):
        super().__init__(parent)
        self.text, self.rates, self.serial_no = text, rates, serial_no

    def run(self) -> None:
        self.parsed.emit(parse_program(self.text, self.rates), self.serial_no)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(TITLE)
        self.setAcceptDrops(True)
        self.qs = QSettings("cncdeck", "cncdeck")
        self.conn: GrblConnection | None = None
        self.status = Status()
        self.program: Program | None = None
        self.file_path: str | None = None
        self._display_name = ""
        self._run_cmds: list[tuple[int, str]] = []
        self._acked_idx = -1
        self._running = False
        self._paused = False
        self._t0 = 0.0
        self._paused_total = 0.0
        self._paused_at = 0.0
        self._continuous = False
        self._probe_thickness = float(self.qs.value("probe_thickness", 15.0))
        self._probe_pending = False
        self.heightmap: HeightMap | None = None     # surface in machine coordinates
        self.prober: HeightMapProbe | None = None
        self._probing = False
        self._probe_ok = False          # contact test passed since the connection was opened
        self._probe_test_until = 0.0
        self._hm_wco: tuple | None = None
        self._parse_serial = 0
        self._parse_thread: ParseThread | None = None
        self._loading_text = False
        self._settings_dlg: GrblSettingsDialog | None = None
        self._alarm_text = ""
        self._last_error = ""
        self._first_error = ""
        self._error_count = 0
        self._ports_sig: list[str] = []

        self.mode = 0
        self._build_toolbar()
        self._build_ui()
        self._build_statusbar()
        self._sender_actions += [self.act_open, self.act_reload, self.act_save, self.act_run, self.act_pause,
                                 self.act_stop, self.act_home, self.act_unlock, self.act_reset, self.act_check,
                                 self.act_probe]
        self.set_mode(0)
        self._restore()
        self._refresh_ports()
        self._update_enabled()

        self._ui_timer = QTimer(self, interval=80)
        self._ui_timer.timeout.connect(self._ui_tick)
        self._ui_timer.start()
        self._port_timer = QTimer(self, interval=3000)
        self._port_timer.timeout.connect(self._auto_refresh_ports)
        self._port_timer.start()
        self._reparse_timer = QTimer(self, singleShot=True, interval=700)
        self._reparse_timer.timeout.connect(self._reparse_editor)
        QApplication.instance().installEventFilter(self)

    # ------------------------------------------------------------------ UI construction
    @staticmethod
    def _spin(lo: float, hi: float, val: float, decimals: int, suffix: str) -> QDoubleSpinBox:
        w = QDoubleSpinBox()
        w.setRange(lo, hi)
        w.setDecimals(decimals)
        w.setValue(val)
        w.setSuffix(suffix)
        return w

    def _act(self, name: str, text: str, tip: str, slot, shortcut: str | None = None, checkable=False) -> QAction:
        a = QAction(icon(name), text, self)
        a.setToolTip(f"{tip}" + (f"  ({shortcut})" if shortcut else ""))
        a.setCheckable(checkable)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        a.triggered.connect(slot)
        return a

    def _build_toolbar(self) -> None:
        tb = QToolBar("Main")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.addToolBar(tb)
        seg = QWidget()
        seg.setStyleSheet("background: transparent;")
        sl = QHBoxLayout(seg)
        sl.setContentsMargins(0, 0, 8, 0)
        sl.setSpacing(0)
        self.btn_mode = []
        for i, text in enumerate(("CNC Sender", "SVG Converter")):
            b = QToolButton()
            b.setText(text)
            b.setCheckable(True)
            b.setToolTip("Switch to " + text)
            b.setStyleSheet("padding: 7px 16px; font-weight: 600; border-radius: 0;"
                            + ("border-top-left-radius:8px; border-bottom-left-radius:8px;" if i == 0 else
                               "border-top-right-radius:8px; border-bottom-right-radius:8px; border-left: none;"))
            b.clicked.connect(lambda _=False, n=i: self.set_mode(n))
            sl.addWidget(b)
            self.btn_mode.append(b)
        tb.addWidget(seg)
        self._sender_actions: list[QAction] = []
        self.act_open = self._act("open", "Open", "Open a G-code file", self.open_file, "Ctrl+O")
        self.act_reload = self._act("reload", "Reload", "Reload the file from disk", self.reload_file, "Ctrl+Shift+R")
        self.act_save = self._act("save", "Save", "Save the changes to the file", self.save_file, "Ctrl+S")
        self.act_run = self._act("play", "Run", "Run the program", self.run_program, "Ctrl+R")
        self.act_pause = self._act("pause", "Pause", "Feed hold / resume", self.toggle_pause, "Ctrl+P")
        self.act_stop = self._act("stop", "Stop", "Abort (hold + reset)", self.stop_program, "Ctrl+.")
        self.act_home = self._act("home", "Home", "Homing cycle ($H)", lambda: self.send("$H"))
        self.act_unlock = self._act("unlock", "Unlock", "Clear the alarm ($X)", lambda: self.send("$X"))
        self.act_reset = self._act("reset", "Reset", "Soft reset (Ctrl-X)", self.soft_reset)
        self.act_check = self._act("check", "Check", "Check mode: simulate without moving ($C)", lambda: self.send("$C"), checkable=True)
        self.act_probe = self._act("probe", "Probe Z", "Zero Z with a touch plate (G38.2)", self.probe_z)
        self.act_settings = self._act("gear", "GRBL", "Board settings ($$)", self.show_settings)
        for a in (self.act_open, self.act_reload, self.act_save):
            tb.addAction(a)
        self._sender_actions += [tb.addSeparator()]
        for a in (self.act_run, self.act_pause, self.act_stop):
            tb.addAction(a)
        for a, name in ((self.act_run, "run"), (self.act_stop, "stopb")):
            tb.widgetForAction(a).setObjectName(name)
        self._sender_actions += [tb.addSeparator()]
        for a in (self.act_home, self.act_unlock, self.act_reset, self.act_check, self.act_probe):
            tb.addAction(a)
        spacer = QWidget()
        spacer.setStyleSheet(f"background: {theme.PANEL};")
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        tb.addAction(self.act_settings)

    def _build_ui(self) -> None:
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        self.stack = QStackedWidget()
        self.stack.addWidget(split)                    # page 0: Sender CNC (unchanged)
        self.converter = ConverterPage()               # page 1: SVG converter
        self.converter.sendRequested.connect(self._send_from_converter)
        self.stack.addWidget(self.converter)
        self.setCentralWidget(self.stack)

        # ---- left column
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(10, 10, 6, 10)
        lv.setSpacing(10)

        c = Card("Connection")
        row = QHBoxLayout()
        self.port_combo = QComboBox()
        self.port_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.port_combo.setMinimumContentsLength(12)
        self.baud_combo = QComboBox()
        for b in BAUDS:
            self.baud_combo.addItem(str(b), b)
        self.baud_combo.setFixedWidth(96)
        row.addWidget(self.port_combo, 1)
        row.addWidget(self.baud_combo)
        c.body.addLayout(row)
        row = QHBoxLayout()
        self.btn_connect = QPushButton("Connect")
        self.btn_connect.setObjectName("primary")
        self.btn_connect.clicked.connect(self.toggle_connection)
        self.badge = StateBadge()
        row.addWidget(self.btn_connect, 1)
        row.addWidget(self.badge)
        c.body.addLayout(row)
        lv.addWidget(c)

        c = Card("Position")
        self.axes = {a: AxisRow(a) for a in "XYZ"}
        for a, r in self.axes.items():
            r.zeroClicked.connect(lambda ax: self.send(f"G10L20P0{ax}0"))
            r.gotoClicked.connect(lambda ax: self.send(f"G90G0{ax}0"))
            c.body.addWidget(r)
        row = QHBoxLayout()
        b = QPushButton("Zero all")
        b.clicked.connect(lambda: self.send("G10L20P0X0Y0Z0"))
        b2 = QPushButton("Go to XY 0")
        b2.clicked.connect(lambda: self.send("G90G0X0Y0"))
        b3 = QPushButton("Safe Z")
        b3.setToolTip("Move Z to the safe height (work coordinates)")
        b3.clicked.connect(self.goto_safe_z)
        for x in (b, b2, b3):
            row.addWidget(x)
        c.body.addLayout(row)
        self.live = QLabel("Feed 0   Spindle 0")
        self.live.setObjectName("muted")
        c.body.addWidget(self.live)
        lv.addWidget(c)

        c = Card("Jog")
        pad_row = QHBoxLayout()
        self.pad = JogPad()
        self.pad.pressed.connect(self.on_jog_press)
        self.pad.released.connect(self.on_jog_release)
        pad_row.addStretch()
        pad_row.addWidget(self.pad)
        pad_row.addStretch()
        c.body.addLayout(pad_row)
        g = QHBoxLayout()
        g.addWidget(QLabel("Step"))
        self.step_combo = QComboBox()
        for label, v in STEPS:
            self.step_combo.addItem(label, v)
        self.step_combo.setCurrentIndex(2)
        g.addWidget(self.step_combo, 1)
        g.addWidget(QLabel("Speed"))
        self.jog_feed = QSpinBox()
        self.jog_feed.setRange(1, 20000)
        self.jog_feed.setValue(800)
        self.jog_feed.setSuffix(" mm/min")
        self.jog_feed.setSingleStep(50)
        g.addWidget(self.jog_feed, 1)
        c.body.addLayout(g)
        g = QHBoxLayout()
        self.chk_keys = QCheckBox("Keyboard: arrows XY, PgUp/PgDn Z")
        g.addWidget(self.chk_keys)
        g.addStretch()
        c.body.addLayout(g)
        g = QHBoxLayout()
        g.addStretch()
        g.addWidget(QLabel("Safe Z"))
        self.safe_z = QDoubleSpinBox()
        self.safe_z.setRange(0, 200)
        self.safe_z.setValue(5.0)
        self.safe_z.setSuffix(" mm")
        g.addWidget(self.safe_z)
        c.body.addLayout(g)
        lv.addWidget(c)

        c = Card("Spindle")
        row = QHBoxLayout()
        self.btn_spindle = QPushButton("  On / Off")
        self.btn_spindle.setIcon(icon("spindle"))
        self.btn_spindle.setCheckable(True)
        self.btn_spindle.clicked.connect(self.toggle_spindle)
        self.spindle_val = QSpinBox()
        self.spindle_val.setRange(0, 30000)
        self.spindle_val.setValue(1000)
        self.spindle_val.setPrefix("S ")
        row.addWidget(self.btn_spindle, 1)
        row.addWidget(self.spindle_val)
        c.body.addLayout(row)
        self.spindle_slider = QSlider(Qt.Horizontal)
        self.spindle_slider.setRange(0, 1000)
        self.spindle_slider.setValue(1000)
        self.spindle_slider.valueChanged.connect(self.spindle_val.setValue)
        self.spindle_val.valueChanged.connect(lambda v: self.spindle_slider.setValue(min(v, self.spindle_slider.maximum())))
        self.spindle_slider.sliderReleased.connect(self._spindle_speed_changed)
        self.spindle_val.editingFinished.connect(self._spindle_speed_changed)
        c.body.addWidget(self.spindle_slider)
        lv.addWidget(c)

        c = Card("Height map (leveling)")
        self.hm_step = self._spin(1, 100, 10.0, 1, " mm")
        self.hm_margin = self._spin(0, 50, 2.0, 1, " mm")
        self.hm_depth = self._spin(0.5, 20, 3.0, 1, " mm")
        self.hm_feed = self._spin(1, 300, 25.0, 0, " mm/min")
        self.hm_clear = self._spin(0.3, 5, 1.0, 1, " mm")
        self.hm_thick = self._spin(0, 50, 0.0, 2, " mm")
        self.hm_step.setToolTip("Distance between grid points. Finer = more accurate and slower.")
        self.hm_margin.setToolTip("How much to enlarge the area beyond the extent of the job.")
        self.hm_depth.setToolTip("Maximum descent below zero: if there is no contact within this depth, probing stops.")
        self.hm_feed.setToolTip("Descent speed while probing (slower = more accurate).")
        self.hm_clear.setToolTip("After the first point the tool descends quickly down to this distance above the last contact.")
        self.hm_thick.setToolTip("Thickness of a touch plate on top of the workpiece (0 if the tool touches the material directly).")
        grid = QGridLayout()
        grid.setSpacing(6)
        for r, (a, wa, b, wb) in enumerate((("Step", self.hm_step, "Margin", self.hm_margin),
                                            ("Max depth", self.hm_depth, "Speed", self.hm_feed),
                                            ("Approach", self.hm_clear, "Plate", self.hm_thick))):
            for col, (lab, w) in enumerate(((a, wa), (b, wb))):
                grid.addWidget(QLabel(lab), r, col * 2)
                grid.addWidget(w, r, col * 2 + 1)
        c.body.addLayout(grid)
        row = QHBoxLayout()
        self.btn_hm_probe = QPushButton("Probe the job area")
        self.btn_hm_probe.setObjectName("primary")
        self.btn_hm_probe.setIcon(icon("probe"))
        self.btn_hm_probe.setToolTip("Measure the surface with the probe on a grid covering the extent of the G-code")
        self.btn_hm_probe.clicked.connect(self.start_heightmap)
        self.btn_hm_test = QPushButton("Test probe")
        self.btn_hm_test.setToolTip("Touch the tool to the material (or plate) when asked: confirms that GRBL sees the probe pin")
        self.btn_hm_test.clicked.connect(self.start_probe_test)
        self.btn_hm_cancel = QPushButton("Cancel")
        self.btn_hm_cancel.setObjectName("danger")
        self.btn_hm_cancel.clicked.connect(lambda: self.prober and self.prober.abort())
        self.btn_hm_cancel.hide()
        row.addWidget(self.btn_hm_probe, 1)
        row.addWidget(self.btn_hm_test)
        row.addWidget(self.btn_hm_cancel)
        c.body.addLayout(row)
        self.hm_bar = QProgressBar()
        self.hm_bar.setTextVisible(True)
        self.hm_bar.hide()
        c.body.addWidget(self.hm_bar)
        self.hm_label = QLabel("No map")
        self.hm_label.setObjectName("muted")
        self.hm_label.setWordWrap(True)
        c.body.addWidget(self.hm_label)
        self.chk_hm = QCheckBox("Apply the map when I run the program")
        self.chk_hm.setEnabled(False)
        self.chk_hm.toggled.connect(lambda _on: self._refresh_heightmap_ui())
        c.body.addWidget(self.chk_hm)
        row = QHBoxLayout()
        self.btn_hm_save = QPushButton("Save…")
        self.btn_hm_save.clicked.connect(self.save_heightmap)
        self.btn_hm_load = QPushButton("Load…")
        self.btn_hm_load.clicked.connect(self.load_heightmap)
        self.btn_hm_clear = QPushButton("Delete")
        self.btn_hm_clear.clicked.connect(self.clear_heightmap)
        for b in (self.btn_hm_save, self.btn_hm_load, self.btn_hm_clear):
            row.addWidget(b)
        c.body.addLayout(row)
        lv.addWidget(c)

        c = Card("Macros  (right-click to edit)")
        self.macro_buttons: list[QPushButton] = []
        grid = QGridLayout()
        grid.setSpacing(6)
        for i in range(6):
            b = QPushButton()
            b.setContextMenuPolicy(Qt.CustomContextMenu)
            b.clicked.connect(lambda _=False, n=i: self.run_macro(n))
            b.customContextMenuRequested.connect(lambda _pos, n=i: self.edit_macro(n))
            grid.addWidget(b, i // 3, i % 3)
            self.macro_buttons.append(b)
        c.body.addLayout(grid)
        lv.addWidget(c)
        self._refresh_macros()

        c = Card("Override")
        self.ov_feed = OverrideBox("Feed")
        self.ov_spin = OverrideBox("Spindle")
        self.ov_rapid = RapidBox()
        self.ov_feed.action.connect(self._ov_feed_action)
        self.ov_spin.action.connect(self._ov_spin_action)
        self.ov_rapid.action.connect(self._ov_rapid_action)
        for w in (self.ov_feed, self.ov_rapid, self.ov_spin):
            c.body.addWidget(w)
        lv.addWidget(c)
        lv.addStretch()

        scroll = QScrollArea()
        scroll.setWidget(left)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(410)
        scroll.setMaximumWidth(440)
        split.addWidget(scroll)

        # ---- right column
        right = QSplitter(Qt.Vertical)
        right.setChildrenCollapsible(False)
        top = QWidget()
        tv = QVBoxLayout(top)
        tv.setContentsMargins(0, 10, 10, 0)
        tv.setSpacing(8)
        self.alarm_bar = QFrame()
        self.alarm_bar.setStyleSheet(f"QFrame{{background:#3b1519; border:1px solid {theme.DANGER}; border-radius:8px;}}"
                                     f"QLabel{{background:transparent; color:#fecaca; font-weight:600;}}")
        ab = QHBoxLayout(self.alarm_bar)
        ab.setContentsMargins(12, 8, 12, 8)
        self.alarm_label = QLabel()
        self.alarm_label.setWordWrap(True)
        bu = QPushButton("Unlock ($X)")
        bu.clicked.connect(lambda: self.send("$X"))
        bh = QPushButton("Homing ($H)")
        bh.clicked.connect(lambda: self.send("$H"))
        ab.addWidget(self.alarm_label, 1)
        ab.addWidget(bu)
        ab.addWidget(bh)
        self.alarm_bar.hide()
        tv.addWidget(self.alarm_bar)
        self.error_bar = QFrame()
        self.error_bar.setStyleSheet(f"QFrame{{background:#3a2d0c; border:1px solid {theme.WARN}; border-radius:8px;}}"
                                     f"QLabel{{background:transparent; color:#fde68a; font-weight:600;}}")
        eb = QHBoxLayout(self.error_bar)
        eb.setContentsMargins(12, 8, 12, 8)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.btn_err_resume = QPushButton("Resume")
        self.btn_err_resume.clicked.connect(self._error_resume)
        self.btn_err_stop = QPushButton("Abort")
        self.btn_err_stop.setObjectName("danger")
        self.btn_err_stop.clicked.connect(self.stop_program)
        eb.addWidget(self.error_label, 1)
        eb.addWidget(self.btn_err_resume)
        eb.addWidget(self.btn_err_stop)
        self.error_bar.hide()
        tv.addWidget(self.error_bar)
        self.view = ToolpathView()
        self.view.setStyleSheet("border: 1px solid %s; border-radius: 10px;" % theme.BORDER)
        tv.addWidget(self.view, 1)
        self._build_view_buttons()
        right.addWidget(top)

        bottom = QWidget()
        bv = QVBoxLayout(bottom)
        bv.setContentsMargins(0, 0, 10, 10)
        tabs = QTabWidget()
        self.editor = CodeEditor()
        self.editor.textChanged.connect(self._editor_changed)
        self.editor.startFromLine.connect(self.run_from_line)
        self.console = Console()
        self.console.commandEntered.connect(self.send)
        prog_page = QWidget()
        pv = QVBoxLayout(prog_page)
        pv.setContentsMargins(8, 8, 8, 8)
        self.job_label = QLabel("No file loaded")
        self.job_label.setObjectName("muted")
        pv.addWidget(self.job_label)
        pv.addWidget(self.editor, 1)
        tabs.addTab(prog_page, "Program")
        tabs.addTab(self.console, "Console")
        self.tabs = tabs
        bv.addWidget(tabs)
        right.addWidget(bottom)
        right.setStretchFactor(0, 3)
        right.setStretchFactor(1, 2)
        split.addWidget(right)
        split.setStretchFactor(1, 1)

    # ------------------------------------------------------------------ modes
    def set_mode(self, n: int) -> None:
        """0 = Sender CNC, 1 = SVG converter. The sender keeps running (streaming, status) in either mode."""
        self.mode = n
        self.stack.setCurrentIndex(n)
        for i, b in enumerate(self.btn_mode):
            b.setChecked(i == n)
        for a in self._sender_actions:
            a.setVisible(n == 0)
        if n == 0:
            self._update_enabled()

    def _send_from_converter(self, text: str, name: str) -> None:
        """'Send to CNC': hand the generated G-code to the Sender in memory (no file round-trip)."""
        if self.load_gcode_text(text, name):
            self.set_mode(0)
            self.sb_state.setText("G-code from the converter loaded into the Sender: check the work zero, then press Run")

    def load_gcode_text(self, text: str, name: str) -> bool:
        if self._running:
            QMessageBox.information(self, APP_NAME, "You cannot load a program while one is running.")
            return False
        self.file_path = None
        self._display_name = name
        self.setWindowTitle(f"{name} — {TITLE}")
        self._loading_text = True
        self.editor.setPlainText(text)
        self._loading_text = False
        self._parse(text)
        self.tabs.setCurrentIndex(0)
        self._update_enabled()
        return True

    def _build_view_buttons(self) -> None:
        bar = QWidget(self.view)
        bar.setStyleSheet("background: transparent;")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        def mk(text, tip, slot, checkable=False, checked=False):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.setCheckable(checkable)
            b.setChecked(checked)
            b.setStyleSheet("padding:4px 9px; font-size:11px; background: rgba(20,26,33,200);")
            b.clicked.connect(slot)
            lay.addWidget(b)
            return b

        mk("3D", "Isometric view", lambda: self.view.set_view("iso"))
        mk("Top", "Top view", lambda: self.view.set_view("top"))
        mk("Front", "Front view", lambda: self.view.set_view("front"))
        mk("Side", "Side view", lambda: self.view.set_view("right"))
        mk("Fit", "Fit everything (double-click)", self.view.fit)
        mk("Rapids", "Show rapid moves", self._toggle_rapids, True, True)
        mk("Machine area", "Show the machine travel limits ($130-132)", self._toggle_envelope, True, False)
        bar.move(10, 10)
        bar.adjustSize()

    def _build_statusbar(self) -> None:
        sb = self.statusBar()
        self.sb_state = QLabel("Not connected")
        self.sb_buf = QLabel("")
        self.sb_buf.setObjectName("muted")
        self.sb_time = QLabel("")
        self.sb_time.setStyleSheet(f"font-family:{theme.MONO};")
        self.progress = QProgressBar()
        self.progress.setFixedWidth(240)
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        self.progress.setTextVisible(False)
        sb.addWidget(self.sb_state, 1)
        sb.addPermanentWidget(self.sb_buf)
        sb.addPermanentWidget(self.sb_time)
        sb.addPermanentWidget(self.progress)

    # ------------------------------------------------------------------ persistence
    def _restore(self) -> None:
        g = self.qs.value("geometry")
        if g:
            self.restoreGeometry(g)
        else:
            self.resize(1480, 940)
        self.jog_feed.setValue(int(self.qs.value("jog_feed", 800)))
        self.step_combo.setCurrentIndex(int(self.qs.value("jog_step", 2)))
        self.safe_z.setValue(float(self.qs.value("safe_z", 5.0)))
        self.spindle_val.setValue(int(self.qs.value("spindle_s", 1000)))
        self.chk_keys.setChecked(self.qs.value("keys", "false") == "true")
        for name, w in self._hm_fields():
            w.setValue(float(self.qs.value(f"hm_{name}", w.value())))
        i = self.baud_combo.findData(int(self.qs.value("baud", 115200)))
        self.baud_combo.setCurrentIndex(max(0, i))

    def _hm_fields(self):
        return (("step", self.hm_step), ("margin", self.hm_margin), ("depth", self.hm_depth),
                ("feed", self.hm_feed), ("clear", self.hm_clear), ("thick", self.hm_thick))

    def closeEvent(self, e) -> None:
        if self._running:
            if QMessageBox.question(self, APP_NAME, "A program is running. Close anyway?") != QMessageBox.Yes:
                e.ignore()
                return
        QApplication.instance().removeEventFilter(self)
        self.qs.setValue("geometry", self.saveGeometry())
        self.qs.setValue("jog_feed", self.jog_feed.value())
        self.qs.setValue("jog_step", self.step_combo.currentIndex())
        self.qs.setValue("safe_z", self.safe_z.value())
        self.qs.setValue("spindle_s", self.spindle_val.value())
        self.qs.setValue("keys", "true" if self.chk_keys.isChecked() else "false")
        for name, w in self._hm_fields():
            self.qs.setValue(f"hm_{name}", w.value())
        self.qs.setValue("baud", self.baud_combo.currentData())
        self.qs.setValue("port", self.port_combo.currentData())
        if self.conn and self.conn.isRunning():
            self.conn.disconnect_port()
            if not self.conn.wait(3000):    # never let a stuck serial thread keep the app alive
                self.conn.terminate()
                self.conn.wait(500)
        super().closeEvent(e)

    # ------------------------------------------------------------------ ports / connection
    def _list_ports(self):
        items = []
        for p in list_ports.comports():
            desc = p.description if p.description and p.description != "n/a" else "Serial port"
            items.append((p.device, f"{p.device}  —  {desc}"))
        items.sort(key=lambda t: (0 if ("ttyUSB" in t[0] or "ttyACM" in t[0]) else 1, t[0]))
        items.append((SIM_PORT, "GRBL simulator (demo)"))
        return items

    def _refresh_ports(self) -> None:
        cur = self.port_combo.currentData() or self.qs.value("port")
        items = self._list_ports()
        self._ports_sig = [d for d, _ in items]
        self.port_combo.clear()
        for dev, label in items:
            self.port_combo.addItem(label, dev)
        i = self.port_combo.findData(cur)
        self.port_combo.setCurrentIndex(i if i >= 0 else 0)

    def _auto_refresh_ports(self) -> None:
        if self.conn and self.conn.isRunning():
            return
        if [d for d, _ in self._list_ports()] != self._ports_sig:
            self._refresh_ports()

    @property
    def connected(self) -> bool:
        return (self.conn is not None and self.conn.isRunning()
                and self.status.state not in ("Disconnected", "Connecting"))

    def toggle_connection(self) -> None:
        if self.conn and self.conn.isRunning():
            self.conn.disconnect_port()
            return
        port = self.port_combo.currentData()
        if not port:
            return
        self.conn = GrblConnection(port, int(self.baud_combo.currentData()))
        c = self.conn
        c.connectionChanged.connect(self.on_connection)
        c.statusChanged.connect(self.on_status)
        c.console.connect(self.console.append)
        c.lineAcked.connect(self.on_line_acked)
        c.programState.connect(self.on_program_state)
        c.settingsChanged.connect(self.on_settings)
        c.alarmRaised.connect(self.on_alarm)
        c.probeResult.connect(self.on_probe)
        if self.prober:
            self.prober.deleteLater()
        self.prober = HeightMapProbe(c, self)
        self.prober.progress.connect(self._on_hm_progress)
        self.prober.finished.connect(self._on_hm_finished)
        self.prober.failed.connect(self._on_hm_failed)
        self.status = Status(state="Connecting")
        self.badge.set_state("Connecting")
        self.btn_connect.setText("Connecting…")
        self.btn_connect.setEnabled(False)
        self.port_combo.setEnabled(False)
        self.baud_combo.setEnabled(False)
        c.start()

    def on_connection(self, ok: bool, msg: str) -> None:
        self.sb_state.setText(msg)
        self.btn_connect.setEnabled(True)
        if ok:
            self.btn_connect.setText("Disconnect")
            self.console.append(msg, "info")
        else:
            self._probe_ok = False
            self._probe_test_until = 0.0
            self.btn_connect.setText("Connect")
            self.port_combo.setEnabled(True)
            self.baud_combo.setEnabled(True)
            self.status = Status()
            self.badge.set_state("Disconnected")
            self.alarm_bar.hide()
            self.console.append(msg, "error" if "Cannot open" in msg or "lost" in msg or "No response" in msg else "info")
            if "Errno 13" in msg or "ermission" in msg:
                QMessageBox.warning(self, APP_NAME,
                                    f"{msg}\n\nOn Ubuntu add your user to the 'dialout' group:\n"
                                    "  sudo usermod -aG dialout $USER\n(then log out and back in).\n\n"
                                    "If a board with a CH340 chip disappears right after plugging it in, "
                                    "remove brltty:  sudo apt remove brltty")
            elif "Cannot open" in msg or "No response" in msg:
                QMessageBox.warning(self, APP_NAME, msg)
            self._finish_run_ui()
        self._update_enabled()

    # ------------------------------------------------------------------ status handling
    def on_status(self, st: Status) -> None:
        self.status = st
        for i, a in enumerate("XYZ"):
            self.axes[a].set_values(st.wpos[i], st.mpos[i])
        self.view.set_tool(st.wpos)
        self._probe_test_tick(st)
        self.badge.set_state(st.state)
        self.act_check.setChecked(st.base_state == "Check")
        self.live.setText(f"Feed {st.feed:.0f} mm/min    Spindle {st.spindle:.0f}")
        self.ov_feed.set_value(st.ov_feed)
        self.ov_rapid.set_value(st.ov_rapid)
        self.ov_spin.set_value(st.ov_spindle)
        port = "Simulator" if self.port_combo.currentData() == SIM_PORT else self.port_combo.currentData()
        self.sb_state.setText(f"{port}  •  {st.state}" + (f"  •  Pin: {st.pins}" if st.pins else ""))
        self.sb_buf.setText(f"Buffer: planner {st.planner_free}  rx {st.rx_free}" if st.planner_free else "")
        if st.base_state == "Alarm":
            self.alarm_label.setText(self._alarm_text or "Machine in ALARM. Check the position, then unlock ($X) or run homing.")
            self.alarm_bar.show()
        else:
            self.alarm_bar.hide()
            self._alarm_text = ""
        spindle_on = st.spindle > 0
        if self.btn_spindle.isChecked() != spindle_on and not self._running:
            self.btn_spindle.setChecked(spindle_on)
        self._update_envelope()
        if self.heightmap is not None and self._hm_wco != tuple(st.wco):
            self._refresh_heightmap_ui()
        self._update_enabled()

    def on_alarm(self, code: int, msg: str) -> None:
        self._alarm_text = f"ALARM {code}: {msg}"
        self.alarm_label.setText(self._alarm_text)
        self.alarm_bar.show()

    def on_settings(self, d: dict) -> None:
        if (v := d.get(30)):
            self.spindle_slider.setMaximum(int(v))
            self.spindle_val.setMaximum(max(int(v), 1))
        if (v := d.get(110)):
            self.jog_feed.setMaximum(int(max(d.get(110, 0), d.get(111, 0))))
        self._update_envelope()
        if self._settings_dlg is not None and self._settings_dlg.isVisible():
            self._settings_dlg.load(d)
        if self.program is not None:
            self._check_travel()

    def show_settings(self) -> None:
        if not self.connected:
            return
        dlg = GrblSettingsDialog(self.conn.settings, self)
        dlg.applyRequested.connect(self._write_settings)
        dlg.refreshRequested.connect(lambda: self.send("$$"))
        self._settings_dlg = dlg
        self.send("$$")
        dlg.show()

    def _write_settings(self, cmds: list) -> None:
        if self._running:
            return
        if QMessageBox.question(self, "Write settings", "Write to the EEPROM:\n\n" + "\n".join(cmds)) != QMessageBox.Yes:
            return
        for c in cmds:
            self.send(c)

    def _update_envelope(self) -> None:
        s = self.conn.settings if self.conn else {}
        if all(k in s for k in (130, 131, 132)):
            hi = -np.array(self.status.wco)
            lo = hi - np.array([s[130], s[131], s[132]])
            self.view.set_envelope(lo, hi)  # machine box [-travel,0] expressed in work coordinates
        else:
            self.view.set_envelope(None, None)

    def _toggle_rapids(self, on: bool) -> None:
        self.view.show_rapids = on
        self.view._invalidate()
        self.view.update()

    def _toggle_envelope(self, on: bool) -> None:
        self.view.show_envelope = on
        self.view.update()

    def _update_enabled(self) -> None:
        st = self.status.base_state
        conn = self.connected
        idle = conn and st in ("Idle", "Check") and not self._running
        idle = idle and not self._probing
        self.act_run.setEnabled(bool(idle and self.program and self.program.commands))
        has_hm = self.heightmap is not None
        self.btn_hm_probe.setEnabled(bool(idle and st == "Idle" and self.program and len(self.program.path)))
        self.btn_hm_probe.setVisible(not self._probing)
        self.btn_hm_test.setVisible(not self._probing)
        self.btn_hm_test.setEnabled(bool(idle and st == "Idle"))
        self.btn_hm_cancel.setVisible(self._probing)
        self.chk_hm.setEnabled(has_hm and not self._probing)
        self.btn_hm_save.setEnabled(has_hm and not self._probing)
        self.btn_hm_clear.setEnabled(has_hm and not self._probing)
        self.btn_hm_load.setEnabled(not self._probing)
        for w in (self.hm_step, self.hm_margin, self.hm_depth, self.hm_feed, self.hm_clear, self.hm_thick):
            w.setEnabled(not self._probing)
        self.act_pause.setEnabled(conn and (self._running or st in ("Run", "Hold")))
        self.act_stop.setEnabled(conn and (self._running or st in ("Run", "Hold", "Jog")))
        self.act_pause.setIcon(icon("play" if self._paused or st == "Hold" else "pause"))
        self.act_pause.setText("Resume" if self._paused or st == "Hold" else "Pause")
        self.act_home.setEnabled(bool(conn and st in ("Idle", "Alarm")))
        self.act_unlock.setEnabled(conn and st == "Alarm")
        self.act_reset.setEnabled(conn)
        self.act_check.setEnabled(bool(idle))
        self.act_probe.setEnabled(bool(idle) and st == "Idle")
        self.act_settings.setEnabled(conn)
        self.act_reload.setEnabled(self.file_path is not None and not self._running)
        self.pad.setEnabled(bool(conn and st in ("Idle", "Jog") and not self._running and not self._probing))
        for r in self.axes.values():
            r.setEnabled(bool(idle))
        self.btn_spindle.setEnabled(bool(conn and st in ("Idle", "Run", "Hold") and not self._running))
        for w in (self.ov_feed, self.ov_rapid, self.ov_spin):
            w.setEnabled(conn)
        self.editor.setReadOnly(self._running)

    # ------------------------------------------------------------------ sending
    def send(self, text: str) -> None:
        if not self.connected:
            self.console.append("Not connected", "error")
            return
        self.conn.send_line(text)

    # ------------------------------------------------------------------ macros
    def _macro(self, n: int) -> tuple[str, str]:
        return (str(self.qs.value(f"macro{n}_name", "")), str(self.qs.value(f"macro{n}_cmd", "")))

    def _refresh_macros(self) -> None:
        for i, b in enumerate(self.macro_buttons):
            name, cmd = self._macro(i)
            b.setText(name or "＋")
            b.setToolTip(cmd.replace(";", "\n") if cmd else "Right-click to define a macro")

    def run_macro(self, n: int) -> None:
        name, cmd = self._macro(n)
        if not cmd:
            self.edit_macro(n)
            return
        if self._running:
            return
        for line in cmd.replace(";", "\n").splitlines():
            if line.strip():
                self.send(line.strip())

    def edit_macro(self, n: int) -> None:
        name, cmd = self._macro(n)
        new_name, ok = QInputDialog.getText(self, "Macro", "Button name (empty to delete):", text=name)
        if not ok:
            return
        if not new_name.strip():
            self.qs.setValue(f"macro{n}_name", "")
            self.qs.setValue(f"macro{n}_cmd", "")
        else:
            new_cmd, ok = QInputDialog.getMultiLineText(self, "Macro", "Commands (one per line):", cmd.replace(";", "\n"))
            if not ok:
                return
            self.qs.setValue(f"macro{n}_name", new_name.strip())
            self.qs.setValue(f"macro{n}_cmd", ";".join(l.strip() for l in new_cmd.splitlines() if l.strip()))
        self._refresh_macros()

    def soft_reset(self) -> None:
        if self.conn:
            self.conn.reset()
            self._finish_run_ui()

    def goto_safe_z(self) -> None:
        self.send(f"G90G0Z{self.safe_z.value():g}")

    def toggle_spindle(self, on: bool) -> None:
        self.send(f"M3S{self.spindle_val.value()}" if on else "M5")

    def _spindle_speed_changed(self) -> None:
        if self.btn_spindle.isChecked() and self.connected:
            self.send(f"M3S{self.spindle_val.value()}")

    def _ov_feed_action(self, a: str) -> None:
        m = {"minus10": grbl.RT_FEED_MINUS10, "minus1": grbl.RT_FEED_MINUS1, "reset": grbl.RT_FEED_RESET,
             "plus1": grbl.RT_FEED_PLUS1, "plus10": grbl.RT_FEED_PLUS10}
        self._rt(m[a])

    def _ov_spin_action(self, a: str) -> None:
        m = {"minus10": grbl.RT_SPINDLE_MINUS10, "minus1": grbl.RT_SPINDLE_MINUS1, "reset": grbl.RT_SPINDLE_RESET,
             "plus1": grbl.RT_SPINDLE_PLUS1, "plus10": grbl.RT_SPINDLE_PLUS10}
        self._rt(m[a])

    def _ov_rapid_action(self, a: str) -> None:
        self._rt({"r25": grbl.RT_RAPID_25, "r50": grbl.RT_RAPID_50, "r100": grbl.RT_RAPID_100}[a])

    def _rt(self, b: bytes) -> None:
        if self.connected:
            self.conn.send_realtime(b)

    # ------------------------------------------------------------------ jog
    def _max_travel(self, axis: int) -> float:
        s = self.conn.settings if self.conn else {}
        return float(s.get(130 + axis, 0) or 0) or (45.0 if axis == 2 else 150.0)

    def on_jog_press(self, dx: int, dy: int, dz: int) -> None:
        if not self.connected or self._running or self.status.base_state not in ("Idle", "Jog"):
            return
        step = self.step_combo.currentData()
        feed = self.jog_feed.value()
        s = self.conn.settings
        words = []
        d = (dx, dy, dz)
        if dz:
            feed = min(feed, int(s.get(112, feed) or feed))
        soft_limits = bool(s.get(20)) and all(k in s for k in (130, 131, 132))
        for i, a in enumerate("XYZ"):
            if d[i]:
                if step is None and soft_limits:   # machine space is [-travel, 0]; stop just inside the limit
                    target = -1.0 if d[i] > 0 else -(s[130 + i] - 1.0)
                    words.append(f"{a}{target:.3f}")
                else:
                    words.append(f"{a}{d[i] * (step if step is not None else self._max_travel(i)):.3f}")
        if not words:
            return
        if step is None:
            self._continuous = True
        mode = "G53G21G90" if (step is None and soft_limits) else "G91G21"
        self.conn.send_line(f"$J={mode}{''.join(words)}F{feed}", silent=True)

    def on_jog_release(self) -> None:
        if self._continuous:
            self._continuous = False
            self.conn.send_realtime(grbl.RT_JOG_CANCEL)

    def eventFilter(self, obj, ev) -> bool:
        if (self.mode == 0 and self.chk_keys.isChecked() and ev.type() in (QEvent.KeyPress, QEvent.KeyRelease)
                and self.isActiveWindow()):
            fw = QApplication.focusWidget()
            if not isinstance(fw, (QLineEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox)):
                if not ev.isAutoRepeat():
                    d = {Qt.Key_Left: (-1, 0, 0), Qt.Key_Right: (1, 0, 0), Qt.Key_Up: (0, 1, 0), Qt.Key_Down: (0, -1, 0),
                         Qt.Key_PageUp: (0, 0, 1), Qt.Key_PageDown: (0, 0, -1)}.get(ev.key())
                    if d:
                        if ev.type() == QEvent.KeyPress:
                            self.on_jog_press(*d)
                        else:
                            self.on_jog_release()
                        return True
                elif ev.key() in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down, Qt.Key_PageUp, Qt.Key_PageDown):
                    return True
        return super().eventFilter(obj, ev)

    # ------------------------------------------------------------------ probing
    def probe_z(self) -> None:
        v, ok = QInputDialog.getDouble(self, "Probe Z", "Touch plate thickness (mm):", self._probe_thickness, 0, 100, 2)
        if not ok:
            return
        self._probe_thickness = v
        self.qs.setValue("probe_thickness", v)
        if QMessageBox.question(self, "Probe Z", "Connect the probe clip to the tool, put the plate under the tip "
                                "and check that the contact works (console: Pn:P). Start the slow descent?") != QMessageBox.Yes:
            return
        self._probe_pending = True
        self.send("G91G38.2Z-30F25")
        self.send("G90")

    def on_probe(self, x: float, y: float, z: float, ok: bool) -> None:
        if not self._probe_pending:
            return
        self._probe_pending = False
        if ok:
            self.send(f"G10L20P0Z{self._probe_thickness:g}")
            self.send(f"G91G0Z{max(5.0, self.safe_z.value()):g}")
            self.send("G90")
            self.console.append(f"Z zeroed on the plate (thickness {self._probe_thickness:g} mm)", "info")
        else:
            self.console.append("Probe failed: no contact", "error")

    # ------------------------------------------------------------------ height map (auto-leveling)
    def _hm_params(self) -> ProbeParams | None:
        if not (self.program and len(self.program.path)):
            return None
        m = self.hm_margin.value()
        lo, hi = self.program.bounds_min, self.program.bounds_max
        return ProbeParams(area=(lo[0] - m, lo[1] - m, hi[0] + m, hi[1] + m), step=self.hm_step.value(),
                           safe_z=max(self.safe_z.value(), self.hm_clear.value() + 1.0), depth=self.hm_depth.value(),
                           clearance=self.hm_clear.value(), feed=self.hm_feed.value(), thickness=self.hm_thick.value())

    def start_probe_test(self) -> None:
        if "P" in self.status.pins:
            QMessageBox.warning(self, "Test probe", "The probe already reads closed (Pn:P): release the contact first "
                                "(or check $6 'Invert probe pin' and the wiring), then test again.")
            return
        self._probe_ok = False
        self._probe_test_until = time.monotonic() + 30.0
        self.console.append("Probe test: touch the tool to the material (or plate) now…", "info")
        self.hm_label.setText("<b>Probe test:</b> touch the tool to the material or plate now (30 s)…")

    def _probe_test_tick(self, st) -> None:
        if not self._probe_test_until:
            return
        if "P" in st.pins:
            self._probe_test_until = 0.0
            self._probe_ok = True
            self.console.append("Probe test passed: GRBL sees the contact (Pn:P)", "info")
            self._refresh_heightmap_ui()
        elif time.monotonic() > self._probe_test_until:
            self._probe_test_until = 0.0
            self.console.append("Probe test failed: no contact seen in 30 s. Check the clip, the wiring and $6.", "error")
            self._refresh_heightmap_ui()

    def start_heightmap(self) -> None:
        p = self._hm_params()
        if not (p and self.prober and self.connected):
            return
        if not self._probe_ok:
            QMessageBox.warning(self, "Height map", "Run «Test probe» first: GRBL must see the probe contact before the "
                                "tool is allowed to descend onto the material. If the tool touches and does not stop, "
                                "the machine would keep plunging.")
            return
        err = p.validate()
        if err:
            QMessageBox.warning(self, "Height map", err)
            return
        from .heightmap import grid_axes
        nx, ny = len(grid_axes(p.area[0], p.area[2], p.step)), len(grid_axes(p.area[1], p.area[3], p.step))
        secs = (p.safe_z + p.depth) / p.feed * 60 + (nx * ny - 1) * ((p.clearance + 0.5) / p.feed * 60 + 3.0)
        x0, y0, x1, y1 = p.area
        if QMessageBox.question(
                self, "Height map",
                f"{nx * ny} points ({nx} × {ny}) will be probed over the area X {x0:.1f}…{x1:.1f}, Y {y0:.1f}…{y1:.1f} mm "
                f"(≈ {format_duration(secs)}).\n\n"
                "Before you continue:\n"
                "• spindle OFF;\n"
                "• probe clip connected to the tool and to the material (or a plate on the workpiece);\n"
                "• Z0 zeroed on the surface at the job origin and the tool raised above the workpiece;\n"
                f"• the first descent starts at Z {p.safe_z:g} and can go down to Z -{p.depth:g}.\n\n"
                "The map is only valid while the workpiece is not moved. Continue?") != QMessageBox.Yes:
            return
        self._probing = True
        err = self.prober.start(p)
        if err:
            self._probing = False
            QMessageBox.warning(self, "Height map", err)
        else:
            self.hm_bar.setRange(0, nx * ny)
            self.hm_bar.setValue(0)
            self.hm_bar.setFormat("%v / %m points")
            self.hm_bar.show()
            self.tabs.setCurrentIndex(1)
            self.console.append(f"Probing {nx * ny} points…", "info")
        self._update_enabled()

    def _on_hm_progress(self, done: int, total: int) -> None:
        self.hm_bar.setRange(0, total)
        self.hm_bar.setValue(done)

    def _on_hm_finished(self, hm: HeightMap) -> None:
        self._probing = False
        self.hm_bar.hide()
        self.heightmap = hm
        self.chk_hm.setChecked(True)
        self.console.append(f"Height map complete: {hm.shape[1]}×{hm.shape[0]} points, height difference {hm.spread:.3f} mm", "info")
        self._refresh_heightmap_ui()
        self._update_enabled()

    def _on_hm_failed(self, msg: str) -> None:
        self._probing = False
        self.hm_bar.hide()
        self.console.append(msg, "error")
        self.sb_state.setText("Probing aborted")
        self._update_enabled()
        QTimer.singleShot(0, lambda: QMessageBox.warning(self, "Height map", msg))

    def _refresh_heightmap_ui(self) -> None:
        hm = self.heightmap
        tested = " • probe test OK" if self._probe_ok else " • probe not tested yet"
        if hm is None:
            self.hm_label.setText("No map" + tested)
            self.view.set_heightmap(None)
            return
        x0, y0, x1, y1 = hm.area
        on = self.chk_hm.isChecked()
        self.hm_label.setText(f"Map {hm.shape[1]}×{hm.shape[0]} points • height difference {hm.spread:.3f} mm • {hm.created}<br>"
                              f"Area (machine) X {x0:.1f}…{x1:.1f}, Y {y0:.1f}…{y1:.1f} • "
                              + ("<b>active</b>" if on else "inactive") + tested)
        self.view.set_heightmap(hm.shifted(self.status.wco))
        self._hm_wco = tuple(self.status.wco)

    def save_heightmap(self) -> None:
        if not self.heightmap:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save height map", "height-map.json", "Height map (*.json)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.heightmap.to_json())
            self.console.append(f"Map saved to {path}", "info")

    def load_heightmap(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load height map", "", "Height map (*.json)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                hm = HeightMap.from_json(f.read())
        except (OSError, HeightMapError) as e:
            QMessageBox.warning(self, "Height map", str(e))
            return
        if not hm.complete:
            QMessageBox.warning(self, "Height map", "The map is incomplete (probing aborted): it cannot be used.")
            return
        self.heightmap = hm
        self.chk_hm.setChecked(False)
        QMessageBox.information(self, "Height map", "Map loaded but NOT active. It is only valid if the workpiece sits "
                                "in the same position where it was measured: check, then tick «Apply the map».")
        self._refresh_heightmap_ui()
        self._update_enabled()

    def clear_heightmap(self) -> None:
        self.heightmap = None
        self.chk_hm.setChecked(False)
        self._refresh_heightmap_ui()
        self._update_enabled()

    def _leveled(self, cmds: list[tuple[int, str]]) -> list[tuple[int, str]] | None:
        """Apply the active height map to the commands about to be streamed; None = do not start."""
        if not (self.heightmap and self.chk_hm.isChecked()):
            return cmds
        work = self.heightmap.shifted(self.status.wco)
        zlo, zhi = float(work.z.min()), float(work.z.max())
        depth, safe = self.heightmap.limits or (3.0, 5.0)
        self.console.append(f"Height map: surface at Z {zlo:.2f}…{zhi:.2f} mm in work coordinates "
                            f"(work zero now at machine {tuple(round(v, 2) for v in self.status.wco)}"
                            + (f", at probing time {tuple(round(v, 2) for v in self.heightmap.wco)}" if self.heightmap.wco else "")
                            + ")", "info")
        if zlo < -depth - 1.0 or zhi > safe + 1.0:
            # a map probed with a consistent Z0 stays within [-depth, safe]: anything else means Z0 or the machine position moved
            ref = work.z_at(0.0, 0.0)
            if QMessageBox.question(
                    self, "Height map",
                    f"The map does not match the current work zero: the surface would be at Z {zlo:.2f}…{zhi:.2f} mm "
                    f"(probing was limited to Z {-depth:g}…{safe:g} mm). The tool would plunge about {abs(ref):.1f} mm "
                    "off the intended depth.\n\nCommon causes: Z0 was changed after probing, or GRBL was reset/reconnected "
                    "(machine coordinates restart from zero).\n\n"
                    f"Apply the map RELATIVE to the surface height at the work origin ({ref:.2f} mm)? This assumes that "
                    "Z0 is currently on the surface there. Choose No to cancel and probe again.") != QMessageBox.Yes:
                return None
            work = HeightMap(work.xs, work.ys, work.z - ref, work.created, work.note, work.wco, work.limits)
        if self.program and len(self.program.path) and not work.covers(self.program.bounds_min[:2], self.program.bounds_max[:2]):
            x0, y0, x1, y1 = work.area
            if QMessageBox.warning(
                    self, "Height map",
                    "The job extends beyond the mapped area (X %.1f…%.1f, Y %.1f…%.1f in work coordinates): outside it the "
                    "edge height is used, so the depth is not compensated. Run anyway?" % (x0, x1, y0, y1),
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return None
        try:
            out = correct_program(cmds, work, start=self.status.wpos)
        except HeightMapError as e:
            QMessageBox.warning(self, "Height map", str(e))
            return None
        self.console.append(f"Height map applied: {len(cmds)} → {len(out)} commands", "info")
        return out

    # ------------------------------------------------------------------ files
    def dragEnterEvent(self, e) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:
        for u in e.mimeData().urls():
            if u.isLocalFile():
                self.load_file(u.toLocalFile())
                break

    def open_file(self) -> None:
        d = self.qs.value("last_dir", os.path.expanduser("~"))
        path, _ = QFileDialog.getOpenFileName(self, "Open G-code", d,
                                              "G-code (*.nc *.gcode *.ngc *.tap *.gc *.cnc *.txt);;All files (*)")
        if path:
            self.load_file(path)

    def reload_file(self) -> None:
        if self.file_path:
            self.load_file(self.file_path)

    def load_file(self, path: str) -> None:
        if self._running:
            QMessageBox.information(self, APP_NAME, "You cannot load a file while one is running.")
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            QMessageBox.critical(self, APP_NAME, str(e))
            return
        self.file_path = path
        self._display_name = os.path.basename(path)
        self.qs.setValue("last_dir", os.path.dirname(path))
        self.setWindowTitle(f"{os.path.basename(path)} — {TITLE}")
        self._loading_text = True
        self.editor.setPlainText(text)
        self._loading_text = False
        self._parse(text)
        self.tabs.setCurrentIndex(0)

    def save_file(self) -> None:
        path = self.file_path
        if not path or not os.access(os.path.dirname(path) or ".", os.W_OK):
            path, _ = QFileDialog.getSaveFileName(self, "Save G-code", self.qs.value("last_dir", os.path.expanduser("~")), "G-code (*.nc *.gcode)")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.editor.toPlainText())
        self.file_path = path
        self.setWindowTitle(f"{os.path.basename(path)} — {TITLE}")
        self.sb_state.setText(f"Saved {path}")

    def _rates(self):
        s = self.conn.settings if self.conn else {}
        return (s.get(110, 2000.0), s.get(111, 2000.0), s.get(112, 800.0))

    def _parse(self, text: str) -> None:
        self._parse_serial += 1
        self.job_label.setText("Analysing the file…")
        t = ParseThread(text, self._rates(), self._parse_serial, self)
        t.parsed.connect(self._on_parsed)
        t.finished.connect(t.deleteLater)
        self._parse_thread = t
        t.start()

    def _on_parsed(self, prog: Program, serial_no: int) -> None:
        if serial_no != self._parse_serial:
            return
        self.program = prog
        self.view.set_program(prog)
        self._acked_idx = -1
        self.editor.set_exec_line(-1)
        self.editor.errors = {int(w.split(":")[0].split()[1]) - 1 for w in prog.warnings if w.startswith("Line ")}
        self.editor.gutter.update()
        sz = prog.size
        n_cmd = len(prog.commands)
        name = self._display_name or "(unnamed)"
        self.job_label.setText(
            f"<b>{name}</b> &nbsp;•&nbsp; {n_cmd} commands &nbsp;•&nbsp; size "
            f"{sz[0]:.1f} × {sz[1]:.1f} × {sz[2]:.1f} mm &nbsp;•&nbsp; cutting {prog.feed_length:.0f} mm "
            f"&nbsp;•&nbsp; estimated time ≈ {format_duration(prog.total_time)}"
            + (f" &nbsp;•&nbsp; <span style='color:{theme.WARN}'>{len(prog.warnings)} warnings</span>" if prog.warnings else ""))
        self.sb_time.setText(f"≈ {format_duration(prog.total_time)}")
        self.progress.setValue(0)
        for w in prog.warnings[:30]:
            self.console.append(w, "msg")
        self._check_travel()
        self._update_enabled()

    def _check_travel(self) -> None:
        if not self.program or not self.conn:
            return
        s = self.conn.settings
        if all(k in s for k in (130, 131, 132)):
            sz = self.program.size
            over = [a for a, v, k in zip("XYZ", sz, (130, 131, 132)) if v > s[k] + 1e-6]
            if over:
                self.console.append(f"Warning: the program is larger than the machine travel on {', '.join(over)}", "msg")

    def _editor_changed(self) -> None:
        if not self._loading_text and not self._running:
            self._reparse_timer.start()

    def _reparse_editor(self) -> None:
        if not self._running:
            self._parse(self.editor.toPlainText())

    # ------------------------------------------------------------------ run control
    def _confirm_start(self) -> bool:
        if self.program and self.program.warnings:
            txt = "\n".join(self.program.warnings[:12])
            if len(self.program.warnings) > 12:
                txt += f"\n… and {len(self.program.warnings) - 12} more"
            if QMessageBox.warning(self, "Program warnings", f"{txt}\n\nRun anyway?",
                                   QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return False
        return True

    def run_program(self) -> None:
        if not (self.program and self.connected) or not self._confirm_start():
            return
        cmds = self._leveled(self.program.commands)
        if cmds is not None:
            self._begin_run(cmds)

    def run_from_line(self, line: int) -> None:
        if not (self.program and self.connected) or self._running or self.status.base_state != "Idle":
            return
        if QMessageBox.question(
                self, "Resume from line",
                f"Restart from line {line + 1}?\n\nThe state (units, feed rate, spindle) will be restored, then the tool "
                "will rise above the workpiece, move to XY and descend to the height of that line. Check that the work zero is the original one.") != QMessageBox.Yes:
            return
        pre = [(line, c) for c in preamble_for(self.program, line)]
        rest = [(i, c) for i, c in self.program.commands if i >= line]
        cmds = self._leveled(pre + rest)
        if cmds is not None:
            self._begin_run(cmds)

    def _begin_run(self, cmds: list[tuple[int, str]]) -> None:
        self._run_cmds = cmds
        self._error_count = 0
        self._last_error = ""
        self._acked_idx = -1
        self._running = True
        self._paused = False
        self._t0 = time.monotonic()
        self._paused_total = 0.0
        self.view.set_progress_line(-1)
        self.conn.start_program(cmds)
        self.tabs.setCurrentIndex(0)
        self._update_enabled()

    def toggle_pause(self) -> None:
        if not self.connected:
            return
        if self._running:
            if self._paused or self.status.base_state == "Hold":
                if self._paused:
                    self._paused_total += time.monotonic() - self._paused_at
                self.conn.resume()
                self._paused = False
            else:
                self._paused_at = time.monotonic()
                self.conn.pause()
                self._paused = True
        elif self.status.base_state == "Hold":
            self.conn.send_realtime(grbl.RT_RESUME)
        else:
            self.conn.send_realtime(grbl.RT_HOLD)
        self._update_enabled()

    def stop_program(self) -> None:
        if self._probing and self.prober:
            self.prober.abort()
            return
        if self.connected:
            if self._running:
                self.conn.stop()
            else:
                self.conn.send_realtime(grbl.RT_JOG_CANCEL)
                if self.status.base_state in ("Run", "Hold"):
                    self.conn.stop()

    def on_program_state(self, s: str) -> None:
        if s != "error":
            self.error_bar.hide()
        if s == "running":
            self._paused = False
        elif s == "paused":
            self._paused = True
        elif s == "error":
            # non-modal: a blocking dialog per rejected line stacks up and freezes the main window
            self._paused = True
            self._paused_at = time.monotonic()
            self.error_label.setText(self._last_error or "GRBL rejected a line. The program is paused.")
            self.error_bar.show()
        elif s == "done":
            el = time.monotonic() - self._t0 - self._paused_total
            self.console.append(f"Program completed in {format_duration(el)}", "info")
            self.sb_state.setText(f"Program completed in {format_duration(el)}")
            self._finish_run_ui(done=True)
        elif s == "aborted":
            self.console.append("Program aborted", "info")
            self._finish_run_ui()
        self._update_enabled()

    def _finish_run_ui(self, done: bool = False) -> None:
        self._running = False
        self._paused = False
        if done:
            self.progress.setValue(1000)
            self.view.set_progress_line(10 ** 9)
        self._update_enabled()

    def on_line_acked(self, idx: int, ok: bool, code: int) -> None:
        self._acked_idx = idx
        if not ok and 0 <= idx < len(self._run_cmds):
            src, cmd = self._run_cmds[idx]
            self._error_count += 1
            if self._error_count == 1:      # keep the first rejected line: it is the cause
                self._first_error = (f"GRBL rejected line {src + 1}: {cmd}  →  error:{code} "
                                     f"{grbl.ERRORS.get(code, '')}. Program paused.")
            self._last_error = self._first_error + (f"  (+{self._error_count - 1} more lines rejected, see Console)"
                                                    if self._error_count > 1 else "")
            if self.error_bar.isVisible():
                self.error_label.setText(self._last_error)

    def _error_resume(self) -> None:
        self.error_bar.hide()
        self.toggle_pause()

    def _estimate_exec_line(self, idx: int) -> int:
        """GRBL acks a line when it enters the planner (up to ~15 blocks ahead of the tool), so snap
        the real tool position onto the nearest path segment among the most recently sent lines."""
        src_hi = self._run_cmds[idx][0]
        if not self.program or not len(self.program.path):
            return src_hi
        src_lo = self._run_cmds[max(0, idx - 30)][0]
        path = self.program.path
        i0 = int(np.searchsorted(path.line, src_lo, "left"))
        i1 = int(np.searchsorted(path.line, src_hi, "right"))
        if i1 <= i0:
            return src_hi
        a = path.start[i0:i1].astype(float)
        ab = path.end[i0:i1].astype(float) - a
        p = np.asarray(self.status.wpos, float)
        t = np.clip(((p - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0.0, 1.0)
        d = np.linalg.norm(a + t[:, None] * ab - p, axis=1)
        if d.min() > 2.0:
            return src_hi
        k = int(np.nonzero(d <= d.min() + 0.03)[0][-1])
        return int(path.line[i0 + k])

    def _ui_tick(self) -> None:
        if not self._running or self._acked_idx < 0 or not self._run_cmds:
            return
        idx = min(self._acked_idx, len(self._run_cmds) - 1)
        src = self._estimate_exec_line(idx)
        self.editor.set_exec_line(src)
        self.view.set_progress_line(src)
        self.progress.setValue(int(1000 * (idx + 1) / len(self._run_cmds)))
        el = (self._paused_at if self._paused else time.monotonic()) - self._t0 - self._paused_total
        rem = 0.0
        if self.program:
            rem = self.program.time_remaining(src) * 100.0 / max(self.status.ov_feed, 10)
        self.sb_time.setText(f"{format_duration(el)}  /  remaining ≈ {format_duration(rem)}")
