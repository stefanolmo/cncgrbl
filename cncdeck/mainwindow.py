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
                               QSplitter, QTabWidget, QToolBar, QToolButton, QVBoxLayout, QWidget)
from serial.tools import list_ports

from . import grbl, theme
from .gcode import Program, format_duration, parse_program, preamble_for
from .grbl import SIM_PORT, GrblConnection, Status
from .settings_dialog import GrblSettingsDialog
from .theme import icon
from .viewer import ToolpathView
from .widgets import (AxisRow, Card, CodeEditor, Console, JogPad, OverrideBox, RapidBox, StateBadge)

STEPS = [("0.01 mm", 0.01), ("0.1 mm", 0.1), ("1 mm", 1.0), ("5 mm", 5.0), ("10 mm", 10.0), ("50 mm", 50.0), ("Continuo", None)]
BAUDS = [115200, 57600, 38400, 19200, 9600, 250000]
APP_NAME = "CNC Deck"


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
        self.setWindowTitle(APP_NAME)
        self.setAcceptDrops(True)
        self.qs = QSettings("cncdeck", "cncdeck")
        self.conn: GrblConnection | None = None
        self.status = Status()
        self.program: Program | None = None
        self.file_path: str | None = None
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
        self._parse_serial = 0
        self._parse_thread: ParseThread | None = None
        self._loading_text = False
        self._settings_dlg: GrblSettingsDialog | None = None
        self._alarm_text = ""
        self._ports_sig: list[str] = []

        self._build_toolbar()
        self._build_ui()
        self._build_statusbar()
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
    def _act(self, name: str, text: str, tip: str, slot, shortcut: str | None = None, checkable=False) -> QAction:
        a = QAction(icon(name), text, self)
        a.setToolTip(f"{tip}" + (f"  ({shortcut})" if shortcut else ""))
        a.setCheckable(checkable)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        a.triggered.connect(slot)
        return a

    def _build_toolbar(self) -> None:
        tb = QToolBar("Principale")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.addToolBar(tb)
        self.act_open = self._act("open", "Apri", "Apri file G-code", self.open_file, "Ctrl+O")
        self.act_reload = self._act("reload", "Ricarica", "Ricarica il file da disco", self.reload_file, "Ctrl+Shift+R")
        self.act_save = self._act("save", "Salva", "Salva le modifiche al file", self.save_file, "Ctrl+S")
        self.act_run = self._act("play", "Avvia", "Avvia il programma", self.run_program, "Ctrl+R")
        self.act_pause = self._act("pause", "Pausa", "Feed hold / riprendi", self.toggle_pause, "Ctrl+P")
        self.act_stop = self._act("stop", "Stop", "Interrompi (hold + reset)", self.stop_program, "Ctrl+.")
        self.act_home = self._act("home", "Home", "Ciclo di homing ($H)", lambda: self.send("$H"))
        self.act_unlock = self._act("unlock", "Sblocca", "Sblocca allarme ($X)", lambda: self.send("$X"))
        self.act_reset = self._act("reset", "Reset", "Soft reset (Ctrl-X)", self.soft_reset)
        self.act_check = self._act("check", "Check", "Modalità check: simula senza muovere ($C)", lambda: self.send("$C"), checkable=True)
        self.act_probe = self._act("probe", "Tocca Z", "Azzera Z con piastrina di tocco (G38.2)", self.probe_z)
        self.act_settings = self._act("gear", "GRBL", "Impostazioni della scheda ($$)", self.show_settings)
        for a in (self.act_open, self.act_reload, self.act_save):
            tb.addAction(a)
        tb.addSeparator()
        for a in (self.act_run, self.act_pause, self.act_stop):
            tb.addAction(a)
        for a, name in ((self.act_run, "run"), (self.act_stop, "stopb")):
            tb.widgetForAction(a).setObjectName(name)
        tb.addSeparator()
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
        self.setCentralWidget(split)

        # ---- left column
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(10, 10, 6, 10)
        lv.setSpacing(10)

        c = Card("Connessione")
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
        self.btn_connect = QPushButton("Connetti")
        self.btn_connect.setObjectName("primary")
        self.btn_connect.clicked.connect(self.toggle_connection)
        self.badge = StateBadge()
        row.addWidget(self.btn_connect, 1)
        row.addWidget(self.badge)
        c.body.addLayout(row)
        lv.addWidget(c)

        c = Card("Posizione")
        self.axes = {a: AxisRow(a) for a in "XYZ"}
        for a, r in self.axes.items():
            r.zeroClicked.connect(lambda ax: self.send(f"G10L20P0{ax}0"))
            r.gotoClicked.connect(lambda ax: self.send(f"G90G0{ax}0"))
            c.body.addWidget(r)
        row = QHBoxLayout()
        b = QPushButton("Azzera tutto")
        b.clicked.connect(lambda: self.send("G10L20P0X0Y0Z0"))
        b2 = QPushButton("Vai a XY 0")
        b2.clicked.connect(lambda: self.send("G90G0X0Y0"))
        b3 = QPushButton("Z sicura")
        b3.setToolTip("Porta Z alla quota di sicurezza (coordinate di lavoro)")
        b3.clicked.connect(self.goto_safe_z)
        for x in (b, b2, b3):
            row.addWidget(x)
        c.body.addLayout(row)
        self.live = QLabel("Avanzamento 0   Mandrino 0")
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
        g.addWidget(QLabel("Passo"))
        self.step_combo = QComboBox()
        for label, v in STEPS:
            self.step_combo.addItem(label, v)
        self.step_combo.setCurrentIndex(2)
        g.addWidget(self.step_combo, 1)
        g.addWidget(QLabel("Vel."))
        self.jog_feed = QSpinBox()
        self.jog_feed.setRange(1, 20000)
        self.jog_feed.setValue(800)
        self.jog_feed.setSuffix(" mm/min")
        self.jog_feed.setSingleStep(50)
        g.addWidget(self.jog_feed, 1)
        c.body.addLayout(g)
        g = QHBoxLayout()
        self.chk_keys = QCheckBox("Tastiera: frecce XY, Pag↑/↓ Z")
        g.addWidget(self.chk_keys)
        g.addStretch()
        g.addWidget(QLabel("Z sicura"))
        self.safe_z = QDoubleSpinBox()
        self.safe_z.setRange(0, 200)
        self.safe_z.setValue(5.0)
        self.safe_z.setSuffix(" mm")
        g.addWidget(self.safe_z)
        c.body.addLayout(g)
        lv.addWidget(c)

        c = Card("Mandrino")
        row = QHBoxLayout()
        self.btn_spindle = QPushButton("  Acceso / Spento")
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

        c = Card("Macro  (tasto destro per modificare)")
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
        self.ov_feed = OverrideBox("Avanzam.")
        self.ov_spin = OverrideBox("Mandrino")
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
        bu = QPushButton("Sblocca ($X)")
        bu.clicked.connect(lambda: self.send("$X"))
        bh = QPushButton("Homing ($H)")
        bh.clicked.connect(lambda: self.send("$H"))
        ab.addWidget(self.alarm_label, 1)
        ab.addWidget(bu)
        ab.addWidget(bh)
        self.alarm_bar.hide()
        tv.addWidget(self.alarm_bar)
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
        self.job_label = QLabel("Nessun file caricato")
        self.job_label.setObjectName("muted")
        pv.addWidget(self.job_label)
        pv.addWidget(self.editor, 1)
        tabs.addTab(prog_page, "Programma")
        tabs.addTab(self.console, "Console")
        self.tabs = tabs
        bv.addWidget(tabs)
        right.addWidget(bottom)
        right.setStretchFactor(0, 3)
        right.setStretchFactor(1, 2)
        split.addWidget(right)
        split.setStretchFactor(1, 1)

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

        mk("3D", "Vista isometrica", lambda: self.view.set_view("iso"))
        mk("Alto", "Vista dall'alto", lambda: self.view.set_view("top"))
        mk("Fronte", "Vista frontale", lambda: self.view.set_view("front"))
        mk("Lato", "Vista laterale", lambda: self.view.set_view("right"))
        mk("Adatta", "Inquadra tutto (doppio clic)", self.view.fit)
        mk("Rapidi", "Mostra i movimenti rapidi", self._toggle_rapids, True, True)
        mk("Area macchina", "Mostra i limiti di corsa della macchina ($130-132)", self._toggle_envelope, True, False)
        org = QComboBox()
        org.setToolTip("Standard = come la macchina si muove davvero (X a destra, Y lontano da te, origine in basso a sinistra). "
                      "Specchiata = Y verso il basso: corretta solo se sulla tua macchina Y+ avvicina la fresa all'operatore")
        org.setStyleSheet("padding:3px 8px; font-size:11px; background: rgba(20,26,33,200);")
        for key, label in (("bottom-left", "Standard: Y verso l'alto"), ("top-left", "Specchiata: Y verso il basso")):
            org.addItem(label, key)
        saved = org.findData(self.qs.value("view_origin_v3", "bottom-left"))
        org.setCurrentIndex(max(0, saved))
        self.view.set_origin(org.currentData())

        def origin_changed():
            self.view.set_origin(org.currentData())
            self.qs.setValue("view_origin_v3", org.currentData())
            self.pad.set_y_down(org.currentData() == "top-left")

        self.pad.set_y_down(org.currentData() == "top-left")
        org.currentIndexChanged.connect(origin_changed)
        lay.addWidget(org)
        bar.move(10, 10)
        bar.adjustSize()

    def _build_statusbar(self) -> None:
        sb = self.statusBar()
        self.sb_state = QLabel("Non connesso")
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
        i = self.baud_combo.findData(int(self.qs.value("baud", 115200)))
        self.baud_combo.setCurrentIndex(max(0, i))

    def closeEvent(self, e) -> None:
        if self._running:
            if QMessageBox.question(self, APP_NAME, "Un programma è in esecuzione. Chiudere comunque?") != QMessageBox.Yes:
                e.ignore()
                return
        QApplication.instance().removeEventFilter(self)
        self.qs.setValue("geometry", self.saveGeometry())
        self.qs.setValue("jog_feed", self.jog_feed.value())
        self.qs.setValue("jog_step", self.step_combo.currentIndex())
        self.qs.setValue("safe_z", self.safe_z.value())
        self.qs.setValue("spindle_s", self.spindle_val.value())
        self.qs.setValue("keys", "true" if self.chk_keys.isChecked() else "false")
        self.qs.setValue("baud", self.baud_combo.currentData())
        self.qs.setValue("port", self.port_combo.currentData())
        if self.conn and self.conn.isRunning():
            self.conn.disconnect_port()
            self.conn.wait(2000)
        super().closeEvent(e)

    # ------------------------------------------------------------------ ports / connection
    def _list_ports(self):
        items = []
        for p in list_ports.comports():
            desc = p.description if p.description and p.description != "n/a" else "Porta seriale"
            items.append((p.device, f"{p.device}  —  {desc}"))
        items.sort(key=lambda t: (0 if ("ttyUSB" in t[0] or "ttyACM" in t[0]) else 1, t[0]))
        items.append((SIM_PORT, "Simulatore GRBL (demo)"))
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
        self.status = Status(state="Connecting")
        self.badge.set_state("Connecting")
        self.btn_connect.setText("Connessione…")
        self.btn_connect.setEnabled(False)
        self.port_combo.setEnabled(False)
        self.baud_combo.setEnabled(False)
        c.start()

    def on_connection(self, ok: bool, msg: str) -> None:
        self.sb_state.setText(msg)
        self.btn_connect.setEnabled(True)
        if ok:
            self.btn_connect.setText("Disconnetti")
            self.console.append(msg, "info")
        else:
            self.btn_connect.setText("Connetti")
            self.port_combo.setEnabled(True)
            self.baud_combo.setEnabled(True)
            self.status = Status()
            self.badge.set_state("Disconnected")
            self.alarm_bar.hide()
            self.console.append(msg, "error" if "mpossibile" in msg or "persa" in msg or "Nessuna" in msg else "info")
            if "Errno 13" in msg or "ermission" in msg:
                QMessageBox.warning(self, APP_NAME,
                                    f"{msg}\n\nSu Ubuntu aggiungi il tuo utente al gruppo 'dialout':\n"
                                    "  sudo usermod -aG dialout $USER\n(poi esci e rientra dalla sessione).\n\n"
                                    "Se la scheda con chip CH340 sparisce subito dopo averla collegata, "
                                    "rimuovi brltty:  sudo apt remove brltty")
            elif "mpossibile" in msg or "Nessuna" in msg:
                QMessageBox.warning(self, APP_NAME, msg)
            self._finish_run_ui()
        self._update_enabled()

    # ------------------------------------------------------------------ status handling
    def on_status(self, st: Status) -> None:
        self.status = st
        for i, a in enumerate("XYZ"):
            self.axes[a].set_values(st.wpos[i], st.mpos[i])
        self.view.set_tool(st.wpos)
        self.badge.set_state(st.state)
        self.act_check.setChecked(st.base_state == "Check")
        self.live.setText(f"Avanzamento {st.feed:.0f} mm/min    Mandrino {st.spindle:.0f}")
        self.ov_feed.set_value(st.ov_feed)
        self.ov_rapid.set_value(st.ov_rapid)
        self.ov_spin.set_value(st.ov_spindle)
        port = "Simulatore" if self.port_combo.currentData() == SIM_PORT else self.port_combo.currentData()
        self.sb_state.setText(f"{port}  •  {st.state}" + (f"  •  Pin: {st.pins}" if st.pins else ""))
        self.sb_buf.setText(f"Buffer: planner {st.planner_free}  rx {st.rx_free}" if st.planner_free else "")
        if st.base_state == "Alarm":
            self.alarm_label.setText(self._alarm_text or "Macchina in ALLARME. Verifica la posizione, poi sblocca ($X) o esegui l'homing.")
            self.alarm_bar.show()
        else:
            self.alarm_bar.hide()
            self._alarm_text = ""
        spindle_on = st.spindle > 0
        if self.btn_spindle.isChecked() != spindle_on and not self._running:
            self.btn_spindle.setChecked(spindle_on)
        self._update_envelope()
        self._update_enabled()

    def on_alarm(self, code: int, msg: str) -> None:
        self._alarm_text = f"ALLARME {code}: {msg}"
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
        if QMessageBox.question(self, "Scrivi impostazioni", "Scrivere nella EEPROM:\n\n" + "\n".join(cmds)) != QMessageBox.Yes:
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
        self.act_run.setEnabled(bool(idle and self.program and self.program.commands))
        self.act_pause.setEnabled(conn and (self._running or st in ("Run", "Hold")))
        self.act_stop.setEnabled(conn and (self._running or st in ("Run", "Hold", "Jog")))
        self.act_pause.setIcon(icon("play" if self._paused or st == "Hold" else "pause"))
        self.act_pause.setText("Riprendi" if self._paused or st == "Hold" else "Pausa")
        self.act_home.setEnabled(bool(conn and st in ("Idle", "Alarm")))
        self.act_unlock.setEnabled(conn and st == "Alarm")
        self.act_reset.setEnabled(conn)
        self.act_check.setEnabled(bool(idle))
        self.act_probe.setEnabled(bool(idle) and st == "Idle")
        self.act_settings.setEnabled(conn)
        self.act_reload.setEnabled(self.file_path is not None and not self._running)
        self.pad.setEnabled(bool(conn and st in ("Idle", "Jog") and not self._running))
        for r in self.axes.values():
            r.setEnabled(bool(idle))
        self.btn_spindle.setEnabled(bool(conn and st in ("Idle", "Run", "Hold") and not self._running))
        for w in (self.ov_feed, self.ov_rapid, self.ov_spin):
            w.setEnabled(conn)
        self.editor.setReadOnly(self._running)

    # ------------------------------------------------------------------ sending
    def send(self, text: str) -> None:
        if not self.connected:
            self.console.append("Non connesso", "error")
            return
        self.conn.send_line(text)

    # ------------------------------------------------------------------ macros
    def _macro(self, n: int) -> tuple[str, str]:
        return (str(self.qs.value(f"macro{n}_name", "")), str(self.qs.value(f"macro{n}_cmd", "")))

    def _refresh_macros(self) -> None:
        for i, b in enumerate(self.macro_buttons):
            name, cmd = self._macro(i)
            b.setText(name or "＋")
            b.setToolTip(cmd.replace(";", "\n") if cmd else "Clic destro per definire una macro")

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
        new_name, ok = QInputDialog.getText(self, "Macro", "Nome del pulsante (vuoto per cancellare):", text=name)
        if not ok:
            return
        if not new_name.strip():
            self.qs.setValue(f"macro{n}_name", "")
            self.qs.setValue(f"macro{n}_cmd", "")
        else:
            new_cmd, ok = QInputDialog.getMultiLineText(self, "Macro", "Comandi (uno per riga):", cmd.replace(";", "\n"))
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
        if self.chk_keys.isChecked() and ev.type() in (QEvent.KeyPress, QEvent.KeyRelease) and self.isActiveWindow():
            fw = QApplication.focusWidget()
            if not isinstance(fw, (QLineEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox)):
                if not ev.isAutoRepeat():
                    d = {Qt.Key_Left: (-1, 0, 0), Qt.Key_Right: (1, 0, 0), Qt.Key_Up: (0, self.pad.ysign, 0), Qt.Key_Down: (0, -self.pad.ysign, 0),
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
        v, ok = QInputDialog.getDouble(self, "Tocca Z", "Spessore della piastrina (mm):", self._probe_thickness, 0, 100, 2)
        if not ok:
            return
        self._probe_thickness = v
        self.qs.setValue("probe_thickness", v)
        if QMessageBox.question(self, "Tocca Z", "Collega la pinza del probe alla fresa, appoggia la piastrina sotto la punta "
                                "e verifica che il contatto funzioni (console: Pn:P). Procedere con la discesa lenta?") != QMessageBox.Yes:
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
            self.console.append(f"Z azzerata sulla piastrina (spessore {self._probe_thickness:g} mm)", "info")
        else:
            self.console.append("Probe fallito: nessun contatto", "error")

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
        path, _ = QFileDialog.getOpenFileName(self, "Apri G-code", d,
                                              "G-code (*.nc *.gcode *.ngc *.tap *.gc *.cnc *.txt);;Tutti i file (*)")
        if path:
            self.load_file(path)

    def reload_file(self) -> None:
        if self.file_path:
            self.load_file(self.file_path)

    def load_file(self, path: str) -> None:
        if self._running:
            QMessageBox.information(self, APP_NAME, "Non puoi caricare un file durante l'esecuzione.")
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            QMessageBox.critical(self, APP_NAME, str(e))
            return
        self.file_path = path
        self.qs.setValue("last_dir", os.path.dirname(path))
        self.setWindowTitle(f"{os.path.basename(path)} — {APP_NAME}")
        self._loading_text = True
        self.editor.setPlainText(text)
        self._loading_text = False
        self._parse(text)
        self.tabs.setCurrentIndex(0)

    def save_file(self) -> None:
        path = self.file_path
        if not path or not os.access(os.path.dirname(path) or ".", os.W_OK):
            path, _ = QFileDialog.getSaveFileName(self, "Salva G-code", self.qs.value("last_dir", os.path.expanduser("~")), "G-code (*.nc *.gcode)")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.editor.toPlainText())
        self.file_path = path
        self.setWindowTitle(f"{os.path.basename(path)} — {APP_NAME}")
        self.sb_state.setText(f"Salvato {path}")

    def _rates(self):
        s = self.conn.settings if self.conn else {}
        return (s.get(110, 2000.0), s.get(111, 2000.0), s.get(112, 800.0))

    def _parse(self, text: str) -> None:
        self._parse_serial += 1
        self.job_label.setText("Analisi del file in corso…")
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
        self.editor.errors = {int(w.split(":")[0].split()[1]) - 1 for w in prog.warnings if w.startswith("Riga ")}
        self.editor.gutter.update()
        sz = prog.size
        n_cmd = len(prog.commands)
        name = os.path.basename(self.file_path) if self.file_path else "(senza nome)"
        self.job_label.setText(
            f"<b>{name}</b> &nbsp;•&nbsp; {n_cmd} comandi &nbsp;•&nbsp; ingombro "
            f"{sz[0]:.1f} × {sz[1]:.1f} × {sz[2]:.1f} mm &nbsp;•&nbsp; lavoro {prog.feed_length:.0f} mm "
            f"&nbsp;•&nbsp; tempo stimato ≈ {format_duration(prog.total_time)}"
            + (f" &nbsp;•&nbsp; <span style='color:{theme.WARN}'>{len(prog.warnings)} avvisi</span>" if prog.warnings else ""))
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
                self.console.append(f"Attenzione: il programma è più grande della corsa della macchina su {', '.join(over)}", "msg")

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
                txt += f"\n… e altri {len(self.program.warnings) - 12}"
            if QMessageBox.warning(self, "Avvisi nel programma", f"{txt}\n\nAvviare comunque?",
                                   QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return False
        return True

    def run_program(self) -> None:
        if not (self.program and self.connected) or not self._confirm_start():
            return
        self._begin_run(self.program.commands)

    def run_from_line(self, line: int) -> None:
        if not (self.program and self.connected) or self._running or self.status.base_state != "Idle":
            return
        if QMessageBox.question(
                self, "Riprendi da riga",
                f"Ripartire dalla riga {line + 1}?\n\nVerrà ripristinato lo stato (unità, avanzamento, mandrino), poi la fresa "
                "salirà sopra il pezzo, andrà in XY e scenderà alla quota della riga. Verifica che lo zero pezzo sia quello originale.") != QMessageBox.Yes:
            return
        pre = [(line, c) for c in preamble_for(self.program, line)]
        rest = [(i, c) for i, c in self.program.commands if i >= line]
        self._begin_run(pre + rest)

    def _begin_run(self, cmds: list[tuple[int, str]]) -> None:
        self._run_cmds = cmds
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
        if self.connected:
            if self._running:
                self.conn.stop()
            else:
                self.conn.send_realtime(grbl.RT_JOG_CANCEL)
                if self.status.base_state in ("Run", "Hold"):
                    self.conn.stop()

    def on_program_state(self, s: str) -> None:
        if s == "running":
            self._paused = False
        elif s == "paused":
            self._paused = True
        elif s == "error":
            self._paused = True
            self.tabs.setCurrentIndex(1)
            QMessageBox.warning(self, "Errore GRBL",
                                "GRBL ha rifiutato una riga (vedi Console). Il programma è in pausa.\n"
                                "Premi Riprendi per continuare oppure Stop per interrompere.")
        elif s == "done":
            el = time.monotonic() - self._t0 - self._paused_total
            self.console.append(f"Programma completato in {format_duration(el)}", "info")
            self.sb_state.setText(f"Programma completato in {format_duration(el)}")
            self._finish_run_ui(done=True)
        elif s == "aborted":
            self.console.append("Programma interrotto", "info")
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
        self.sb_time.setText(f"{format_duration(el)}  /  restano ≈ {format_duration(rem)}")
