"""Converter mode: load an SVG, configure tool and operations (jscut-style), preview and produce G-code."""
from __future__ import annotations

import copy
import os

from PySide6.QtCore import QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton,
                               QScrollArea, QSpinBox, QSplitter, QTabWidget, QVBoxLayout, QWidget, QGridLayout)

from . import cam, theme
from .gcode import Program, format_duration, parse_program
from .svgimport import SvgDoc, parse_svg
from .viewer import ToolpathView, add_view_buttons
from .widgets import Card, CodeEditor

KINDS = [("outside", "Outside (profile outside)"), ("inside", "Inside (profile inside)"),
         ("pocket", "Pocket (clearing)"), ("engrave", "Engrave (centre line)")]
TABMODES = [("none", "None"), ("auto", "Automatic (evenly spaced)"), ("shapes", "From SVG shapes (markers)")]
COMBINE = [("union", "Union"), ("intersect", "Intersection"), ("diff", "Difference"), ("xor", "XOR")]
ORIGINS = [("top-left", "Top-left corner of the design"), ("bottom-left", "Bottom-left corner of the design"),
           ("svg", "SVG page origin (like jscut)")]


class GenThread(QThread):
    done = Signal(object, object, int)

    def __init__(self, doc, ops, job, name, serial_no, parent=None):
        super().__init__(parent)
        self.doc, self.ops, self.job, self.name, self.serial_no = doc, ops, job, name, serial_no

    def run(self) -> None:
        try:
            res = cam.generate(self.doc, self.ops, self.job, self.name)
            prog = parse_program(res.gcode) if res.gcode else None
        except Exception as e:  # never let a CAM failure kill the thread silently
            res = cam.CamResult(warnings=[f"Conversion error: {e}"])
            prog = None
        self.done.emit(res, prog, self.serial_no)


def _dspin(lo, hi, val, dec=2, suffix="", step=None) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(dec)
    s.setValue(val)
    if suffix:
        s.setSuffix(suffix)
    if step:
        s.setSingleStep(step)
    return s


def _combo(items) -> QComboBox:
    c = QComboBox()
    for key, label in items:
        c.addItem(label, key)
    return c


class ConverterPage(QWidget):
    sendRequested = Signal(str, str)     # gcode text, display name

    def __init__(self, parent=None):
        super().__init__(parent)
        self.qs = QSettings("cncdeck", "cncdeck")
        self.doc: SvgDoc | None = None
        self.svg_path: str | None = None
        self.ops: list[cam.Operation] = []          # starts empty, like jscut: the user creates the operations
        self.result: cam.CamResult | None = None
        self.program: Program | None = None
        self._serial = 0
        self._thread: GenThread | None = None
        self._loading = False
        self._dirty = False
        self._fit_next = True
        self._timer = QTimer(self, singleShot=True, interval=350)
        self._timer.timeout.connect(self.generate_now)
        self._build()
        self._refresh_ops_list()
        self._load_op_widgets()
        self._update_buttons()

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        root.addWidget(split)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(10, 10, 6, 10)
        lv.setSpacing(10)

        # --- file
        c = Card("SVG file")
        row = QHBoxLayout()
        b = QPushButton("Open SVG…")
        b.setObjectName("primary")
        b.clicked.connect(self.open_svg)
        self.file_label = QLabel("No file")
        self.file_label.setObjectName("muted")
        self.file_label.setWordWrap(True)
        row.addWidget(b)
        row.addWidget(self.file_label, 1)
        c.body.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("Resolution"))
        self.dpi = QSpinBox()
        self.dpi.setRange(10, 1200)
        self.dpi.setValue(int(self.qs.value("conv/dpi", 96)))
        self.dpi.setSuffix(" px/inch")
        self.dpi.setToolTip("Size of px in a unit-less SVG: 96 (CSS standard, Inkscape ≥ 0.92) or 72/90 (old files)")
        self.dpi.editingFinished.connect(self._reload_svg)
        row.addWidget(self.dpi, 1)
        row.addWidget(QLabel("Scale"))
        self.scale = _dspin(1, 1000, 100.0, 1, " %")
        self.scale.editingFinished.connect(self._reload_svg)
        row.addWidget(self.scale)
        c.body.addLayout(row)
        lv.addWidget(c)

        # --- operations
        c = Card("Operations")
        self.ops_list = QListWidget()
        self.ops_list.setMaximumHeight(110)
        self.ops_list.currentRowChanged.connect(self._op_selected)
        c.body.addWidget(self.ops_list)
        row = QHBoxLayout()
        self._op_btns: list[QPushButton] = []       # need an existing operation to act on
        for i, (text, slot) in enumerate((("＋ Add", self._add_op), ("Duplicate", self._dup_op),
                                          ("Remove", self._del_op), ("▲", lambda: self._move_op(-1)),
                                          ("▼", lambda: self._move_op(1)))):
            bt = QPushButton(text)
            bt.clicked.connect(slot)
            row.addWidget(bt)
            if i:
                self._op_btns.append(bt)
        c.body.addLayout(row)
        g = QGridLayout()
        g.setHorizontalSpacing(8)
        g.setVerticalSpacing(6)
        self.w_name = QLineEdit()
        self.w_kind = _combo(KINDS)
        self.w_combine = _combo(COMBINE)
        self.w_dir = _combo([("conv", "Conventional"), ("climb", "Climb")])
        self.w_cut = _dspin(0.01, 100, 1.0, 2, " mm", 0.1)
        self.w_pass = _dspin(0.01, 50, 0.5, 2, " mm", 0.1)
        self.w_step = _dspin(5, 100, 40.0, 0, " %", 5)
        self.w_width = _dspin(0, 100, 0.0, 2, " mm", 0.5)
        self.w_margin = _dspin(-20, 20, 0.0, 2, " mm", 0.1)
        self.w_ramp = QCheckBox("Ramp entry (instead of a vertical plunge)")
        self.w_tabmode = _combo(TABMODES)
        self.w_tabcount = QSpinBox()
        self.w_tabcount.setRange(1, 50)
        self.w_tabcount.setValue(4)
        self.w_tabw = _dspin(0.5, 100, 4.0, 1, " mm", 0.5)
        self.w_tabh = _dspin(0.1, 50, 1.0, 2, " mm", 0.1)
        self.w_tabmode.setToolTip("Holding tabs: along the contour cut the tool stays raised so the part does not detach "
                                  "or move. Outside/Inside only. Cut them afterwards with a saw/knife.")
        self.w_tabcount.setToolTip("Automatic: number of tabs on each part contour (holes get none)")
        self.w_tabw.setToolTip("Length of each tab along the path")
        self.w_tabh.setToolTip("Height of material left under the tab (from the bottom of the cut)")
        self.w_cut.setToolTip("Total depth below the material surface (Z = 0)")
        self.w_step.setToolTip("Distance between side-by-side passes, in % of the tool diameter (jscut: step over)")
        self.w_width.setToolTip("Width of the machined band for Outside/Inside; 0 = a single pass (= tool diameter)")
        self.w_margin.setToolTip("Grows (Outside) or shrinks (Inside) the geometry before computing the path")
        rows = [("Name", self.w_name), ("Type", self.w_kind), ("Combine shapes", self.w_combine),
                ("Direction", self.w_dir), ("Depth", self.w_cut), ("Per pass", self.w_pass),
                ("Step over", self.w_step), ("Width", self.w_width), ("Offset", self.w_margin)]
        for i, (lab, w) in enumerate(rows):
            g.addWidget(QLabel(lab), i, 0)
            g.addWidget(w, i, 1)
        c.body.addLayout(g)
        c.body.addWidget(self.w_ramp)
        tg = QGridLayout()
        tg.setHorizontalSpacing(8)
        tg.setVerticalSpacing(6)
        self.tab_widgets = [(QLabel("Tabs"), self.w_tabmode), (QLabel("  Count"), self.w_tabcount),
                            (QLabel("  Width"), self.w_tabw), (QLabel("  Height"), self.w_tabh)]
        for i, (lab, w) in enumerate(self.tab_widgets):
            tg.addWidget(lab, i, 0)
            tg.addWidget(w, i, 1)
        c.body.addLayout(tg)
        self.tab_shapes_label = QLabel("Shapes used as tabs (they are not milled)")
        self.tab_shape_list = QListWidget()
        self.tab_shape_list.setMaximumHeight(100)
        self.tab_shape_list.itemChanged.connect(self._tabshapes_changed)
        c.body.addWidget(self.tab_shapes_label)
        c.body.addWidget(self.tab_shape_list)
        c.body.addWidget(QLabel("Shapes used by this operation"))
        self.shape_list = QListWidget()
        self.shape_list.setMaximumHeight(120)
        self.shape_list.itemChanged.connect(self._shapes_changed)
        c.body.addWidget(self.shape_list)
        lv.addWidget(c)
        for w in (self.w_name, ):
            w.textEdited.connect(self._op_edited)
        for w in (self.w_kind, self.w_combine, self.w_dir):
            w.currentIndexChanged.connect(self._op_edited)
        for w in (self.w_cut, self.w_pass, self.w_step, self.w_width, self.w_margin, self.w_tabw, self.w_tabh):
            w.valueChanged.connect(self._op_edited)
        self.w_tabcount.valueChanged.connect(self._op_edited)
        self.w_tabmode.currentIndexChanged.connect(self._op_edited)
        self.w_ramp.toggled.connect(self._op_edited)

        # --- tool & job
        c = Card("Tool and speeds")
        g = QGridLayout()
        g.setHorizontalSpacing(8)
        g.setVerticalSpacing(6)
        q = self.qs
        self.j_tool = _dspin(0.05, 50, float(q.value("conv/tool", 3.175)), 3, " mm", 0.1)
        self.j_cut = _dspin(1, 20000, float(q.value("conv/cut", 500)), 0, " mm/min", 50)
        self.j_plunge = _dspin(1, 20000, float(q.value("conv/plunge", 150)), 0, " mm/min", 10)
        self.j_safe = _dspin(0.5, 100, float(q.value("conv/safe", 5.0)), 1, " mm", 0.5)
        self.j_spindle = QCheckBox("Start spindle (M3) / stop (M5)")
        self.j_spindle.setChecked(q.value("conv/spindle", "false") == "true")
        self.j_rpm = _dspin(1, 100000, float(q.value("conv/rpm", 1000)), 0, "  S", 100)
        self.j_units = _combo([("mm", "Millimetres (G21)"), ("in", "Inches (G20)")])
        self.j_origin = _combo(ORIGINS)
        self.j_origin.setCurrentIndex(max(0, self.j_origin.findData(q.value("conv/origin", "top-left"))))
        self.j_units.setCurrentIndex(max(0, self.j_units.findData(q.value("conv/units", "mm"))))
        self.j_origin.setToolTip("Where the G-code zero sits relative to the design. Your work zero must coincide with this point.")
        self.j_spindle.setToolTip("Off by default, like jscut: the spindle stays under your control (Spindle button in the Sender)")
        jrows = [("Tool Ø", self.j_tool), ("Feed rate", self.j_cut), ("Plunge", self.j_plunge),
                 ("Safe Z", self.j_safe), ("G-code units", self.j_units), ("G-code zero", self.j_origin)]
        for i, (lab, w) in enumerate(jrows):
            g.addWidget(QLabel(lab), i, 0)
            g.addWidget(w, i, 1)
        c.body.addLayout(g)
        row = QHBoxLayout()
        row.addWidget(self.j_spindle, 1)
        row.addWidget(self.j_rpm)
        c.body.addLayout(row)
        lv.addWidget(c)
        for w in (self.j_tool, self.j_cut, self.j_plunge, self.j_safe, self.j_rpm):
            w.valueChanged.connect(self._job_edited)
        for w in (self.j_units, self.j_origin):
            w.currentIndexChanged.connect(self._job_edited)
        self.j_spindle.toggled.connect(self._job_edited)

        # --- actions
        c = Card("G-code")
        self.btn_gen = QPushButton("Generate G-code")
        self.btn_gen.clicked.connect(self.generate_now)
        row = QHBoxLayout()
        self.btn_save = QPushButton("Save .gcode…")
        self.btn_save.clicked.connect(self.save_gcode)
        self.btn_send = QPushButton("Send to CNC  ➜")
        self.btn_send.setObjectName("primary")
        self.btn_send.setToolTip("Load the G-code into the Sender without saving a file; then start from the Sender")
        self.btn_send.clicked.connect(self._send)
        row.addWidget(self.btn_save)
        row.addWidget(self.btn_send, 1)
        c.body.addWidget(self.btn_gen)
        c.body.addLayout(row)
        self.summary = QLabel("")
        self.summary.setObjectName("muted")
        self.summary.setWordWrap(True)
        c.body.addWidget(self.summary)
        lv.addWidget(c)
        lv.addStretch()

        scroll = QScrollArea()
        scroll.setWidget(left)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(410)
        scroll.setMaximumWidth(450)
        split.addWidget(scroll)

        # right: preview + tabs
        right = QSplitter(Qt.Vertical)
        right.setChildrenCollapsible(False)
        top = QWidget()
        tv = QVBoxLayout(top)
        tv.setContentsMargins(0, 10, 10, 0)
        self.view = ToolpathView()
        self.view.setStyleSheet("border: 1px solid %s; border-radius: 10px;" % theme.BORDER)
        tv.addWidget(self.view, 1)
        add_view_buttons(self.view)
        right.addWidget(top)
        bottom = QWidget()
        bv = QVBoxLayout(bottom)
        bv.setContentsMargins(0, 0, 10, 10)
        tabs = QTabWidget()
        self.editor = CodeEditor()
        self.editor.setReadOnly(True)
        self.messages = QPlainTextEdit()
        self.messages.setReadOnly(True)
        tabs.addTab(self.editor, "Generated G-code")
        tabs.addTab(self.messages, "Messages")
        self.tabs = tabs
        bv.addWidget(tabs)
        right.addWidget(bottom)
        right.setStretchFactor(0, 3)
        right.setStretchFactor(1, 2)
        split.addWidget(right)
        split.setStretchFactor(1, 1)

    # ------------------------------------------------------------------ SVG
    def open_svg(self) -> None:
        d = self.qs.value("conv/last_dir", os.path.expanduser("~"))
        path, _ = QFileDialog.getOpenFileName(self, "Open SVG", d, "SVG (*.svg);;All files (*)")
        if path:
            self.load_svg(path)

    def load_svg(self, path: str) -> bool:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            QMessageBox.critical(self, "CNC Deck", str(e))
            return False
        self.svg_path = path
        self._svg_text = text
        self.qs.setValue("conv/last_dir", os.path.dirname(path))
        self._fit_next = True
        self._parse_svg()
        return self.doc is not None and bool(self.doc.shapes)

    def _reload_svg(self) -> None:
        if getattr(self, "_svg_text", None) and self.doc is not None:
            self.qs.setValue("conv/dpi", self.dpi.value())
            self._parse_svg()

    def _parse_svg(self) -> None:
        self.doc = parse_svg(self._svg_text, dpi=float(self.dpi.value()), scale=self.scale.value() / 100.0)
        name = os.path.basename(self.svg_path or "")
        n = len(self.doc.shapes)
        self.file_label.setText(f"{name}\n{n} shapes, {self.doc.width_mm:.1f} × {self.doc.height_mm:.1f} mm")
        valid = {s.sid for s in self.doc.shapes}
        for op in self.ops:
            if op.shapes is not None:
                op.shapes &= valid
        self._fill_shape_list()
        self._update_buttons()
        self.generate_now()

    def _fill_shape_list(self) -> None:
        self._loading = True
        self.shape_list.clear()
        self.tab_shape_list.clear()
        op = self.cur_op()
        if self.doc and op:
            for s in self.doc.shapes:
                it = QListWidgetItem(f"{s.label}  ({s.kind})")
                it.setData(Qt.UserRole, s.sid)
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(Qt.Checked if (op.shapes is None or s.sid in op.shapes) else Qt.Unchecked)
                self.shape_list.addItem(it)
                tt = QListWidgetItem(f"{s.label}  ({s.kind})")
                tt.setData(Qt.UserRole, s.sid)
                tt.setFlags(tt.flags() | Qt.ItemIsUserCheckable)
                tt.setCheckState(Qt.Checked if s.sid in op.tab_shapes else Qt.Unchecked)
                self.tab_shape_list.addItem(tt)
        self._loading = False
        self._update_tab_widgets()

    # ------------------------------------------------------------------ operations model <-> widgets
    def cur_op(self) -> cam.Operation | None:
        i = self.ops_list.currentRow()
        return self.ops[i] if 0 <= i < len(self.ops) else None

    def _op_label(self, i: int) -> str:
        op = self.ops[i]
        kind = dict(KINDS)[op.kind].split(" (")[0]
        return f"{i + 1}. {op.name}  ·  {kind}"

    def _refresh_ops_list(self, select: int | None = None) -> None:
        keep = self.ops_list.currentRow() if select is None else select
        self.ops_list.blockSignals(True)
        self.ops_list.clear()
        for i in range(len(self.ops)):
            self.ops_list.addItem(self._op_label(i))
        self.ops_list.setCurrentRow(max(0, min(keep, len(self.ops) - 1)) if self.ops else -1)
        self.ops_list.blockSignals(False)
        for b in self._op_btns:
            b.setEnabled(bool(self.ops))

    def _load_op_widgets(self) -> None:
        op = self.cur_op()
        self._loading = True
        en = op is not None
        for w in (self.w_name, self.w_kind, self.w_combine, self.w_dir, self.w_cut, self.w_pass, self.w_step,
                  self.w_width, self.w_margin, self.w_ramp, self.shape_list):
            w.setEnabled(en)
        if not en:
            for w in (self.w_tabmode, self.w_tabcount, self.w_tabw, self.w_tabh):
                w.setEnabled(False)
        if op:
            self.w_name.setText(op.name)
            self.w_kind.setCurrentIndex(self.w_kind.findData(op.kind))
            self.w_combine.setCurrentIndex(self.w_combine.findData(op.combine))
            self.w_dir.setCurrentIndex(self.w_dir.findData("climb" if op.climb else "conv"))
            self.w_cut.setValue(op.cut_depth)
            self.w_pass.setValue(op.pass_depth)
            self.w_step.setValue(op.step_over * 100)
            self.w_width.setValue(op.width)
            self.w_margin.setValue(op.margin)
            self.w_ramp.setChecked(op.ramp)
            self.w_tabmode.setCurrentIndex(self.w_tabmode.findData(op.tab_mode))
            self.w_tabcount.setValue(op.tab_count)
            self.w_tabw.setValue(op.tab_width)
            self.w_tabh.setValue(op.tab_height)
        self._loading = False
        self._fill_shape_list()

    def _op_selected(self, row: int) -> None:
        self._load_op_widgets()

    def _op_edited(self, *_) -> None:
        op = self.cur_op()
        if self._loading or op is None:
            return
        op.name = self.w_name.text() or "Operation"
        op.kind = self.w_kind.currentData()
        op.combine = self.w_combine.currentData()
        op.climb = self.w_dir.currentData() == "climb"
        op.cut_depth = self.w_cut.value()
        op.pass_depth = self.w_pass.value()
        op.step_over = self.w_step.value() / 100.0
        op.width = self.w_width.value()
        op.margin = self.w_margin.value()
        op.ramp = self.w_ramp.isChecked()
        op.tab_mode = self.w_tabmode.currentData()
        op.tab_count = self.w_tabcount.value()
        op.tab_width = self.w_tabw.value()
        op.tab_height = self.w_tabh.value()
        self._update_tab_widgets()
        self.ops_list.item(self.ops_list.currentRow()).setText(self._op_label(self.ops_list.currentRow()))
        self._schedule()

    def _update_tab_widgets(self) -> None:
        """Tabs only make sense for Outside/Inside; the count is for automatic tabs, the marker list for shapes mode."""
        op = self.cur_op()
        applicable = op is not None and op.kind in ("inside", "outside")
        mode = op.tab_mode if (op is not None and applicable) else "none"
        self.w_tabmode.setEnabled(applicable)
        self.w_tabcount.setEnabled(mode == "auto")
        self.w_tabw.setEnabled(mode != "none")
        self.w_tabh.setEnabled(mode != "none")
        self.tab_shape_list.setVisible(mode == "shapes")
        self.tab_shapes_label.setVisible(mode == "shapes")

    def _tabshapes_changed(self, _item) -> None:
        op = self.cur_op()
        if self._loading or op is None:
            return
        op.tab_shapes = {self.tab_shape_list.item(i).data(Qt.UserRole) for i in range(self.tab_shape_list.count())
                         if self.tab_shape_list.item(i).checkState() == Qt.Checked}
        self._schedule()

    def _shapes_changed(self, _item) -> None:
        op = self.cur_op()
        if self._loading or op is None:
            return
        ids = {self.shape_list.item(i).data(Qt.UserRole) for i in range(self.shape_list.count())
               if self.shape_list.item(i).checkState() == Qt.Checked}
        op.shapes = None if len(ids) == self.shape_list.count() else ids
        self._schedule()

    def _add_op(self) -> None:
        self.ops.append(cam.Operation(name=f"Operation {len(self.ops) + 1}", kind="outside"))
        self._refresh_ops_list(len(self.ops) - 1)
        self._load_op_widgets()
        self._schedule()

    def _dup_op(self) -> None:
        op = self.cur_op()
        if op:
            n = copy.deepcopy(op)
            n.name += " (copy)"
            self.ops.insert(self.ops_list.currentRow() + 1, n)
            self._refresh_ops_list(self.ops_list.currentRow() + 1)
            self._load_op_widgets()
            self._schedule()

    def _del_op(self) -> None:
        i = self.ops_list.currentRow()
        if 0 <= i < len(self.ops):
            del self.ops[i]
            self._refresh_ops_list(max(0, i - 1))
            self._load_op_widgets()
            self._schedule()

    def _move_op(self, d: int) -> None:
        i = self.ops_list.currentRow()
        j = i + d
        if 0 <= i < len(self.ops) and 0 <= j < len(self.ops):
            self.ops[i], self.ops[j] = self.ops[j], self.ops[i]
            self._refresh_ops_list(j)
            self._schedule()

    # ------------------------------------------------------------------ job
    def job(self) -> cam.Job:
        return cam.Job(tool_diameter=self.j_tool.value(), cut_feed=self.j_cut.value(), plunge_feed=self.j_plunge.value(),
                       safe_z=self.j_safe.value(), spindle=self.j_spindle.isChecked(), spindle_rpm=self.j_rpm.value(),
                       units=self.j_units.currentData(), origin=self.j_origin.currentData())

    def _job_edited(self, *_) -> None:
        q = self.qs
        q.setValue("conv/tool", self.j_tool.value())
        q.setValue("conv/cut", self.j_cut.value())
        q.setValue("conv/plunge", self.j_plunge.value())
        q.setValue("conv/safe", self.j_safe.value())
        q.setValue("conv/spindle", "true" if self.j_spindle.isChecked() else "false")
        q.setValue("conv/rpm", self.j_rpm.value())
        q.setValue("conv/units", self.j_units.currentData())
        q.setValue("conv/origin", self.j_origin.currentData())
        self._schedule()

    # ------------------------------------------------------------------ generation
    def _schedule(self) -> None:
        if self.doc is not None:
            self._dirty = True            # Save/Send stay disabled until the new G-code is ready
            self._update_buttons()
            self._timer.start()

    def generate_now(self) -> None:
        self._timer.stop()
        if self.doc is None or not self.doc.shapes:
            self._show_result(cam.CamResult(warnings=list(self.doc.warnings) if self.doc else []), None)
            return
        self._serial += 1                 # also invalidates any computation still running
        self._dirty = True                # the previous G-code is stale until the new one arrives
        self._update_buttons()
        if not self.ops:
            self._show_result(cam.CamResult(guides=cam.design_guides(self.doc, self.job())), None)
            return
        self.summary.setText("Computing the toolpath…")
        t = GenThread(self.doc, copy.deepcopy(self.ops), self.job(), os.path.basename(self.svg_path or ""),
                      self._serial, self)
        t.done.connect(self._on_generated)
        t.finished.connect(t.deleteLater)
        self._thread = t
        t.start()

    def _on_generated(self, res, prog, serial_no: int) -> None:
        if serial_no != self._serial:
            return
        self._show_result(res, prog)

    def _show_result(self, res: cam.CamResult, prog: Program | None) -> None:
        self.result, self.program = res, prog
        self._dirty = False
        self.editor.setPlainText(res.gcode)
        self.view.set_guides(res.guides)
        self.view.empty_message = ("Add an operation (＋ Add) to generate the toolpath" if self.doc
                                   else "Open an SVG file")
        self.view.set_program(prog, fit=self._fit_next)
        if prog is not None and len(prog.path):
            self._fit_next = False
        elif res.guides:
            self.view.fit()               # no toolpath yet: frame the design itself
        msgs = list(self.doc.warnings if self.doc else []) + res.warnings + (prog.warnings if prog else [])
        if prog is not None:
            sz = prog.size
            info = (f"{res.n_paths} paths, {len(prog.commands)} lines, size {sz[0]:.1f} × {sz[1]:.1f} × {sz[2]:.1f} mm, "
                    f"estimated time ≈ {format_duration(prog.total_time)}")
            self.summary.setText(info + (f"\n⚠ {len(msgs)} warnings (Messages tab)" if msgs else ""))
            msgs.insert(0, info)
        elif not self.ops and self.doc is not None:
            self.summary.setText("No operations: press «＋ Add» to create one")
        else:
            self.summary.setText("No G-code generated" + (" — see Messages" if msgs else ""))
        self.messages.setPlainText("\n".join(msgs))
        self._update_buttons()

    def _update_buttons(self) -> None:
        ok = bool(self.result and self.result.gcode) and not self._dirty
        self.btn_save.setEnabled(ok)
        self.btn_send.setEnabled(ok)
        self.btn_gen.setEnabled(self.doc is not None and bool(self.ops))

    # ------------------------------------------------------------------ output
    def suggested_name(self) -> str:
        base = os.path.splitext(os.path.basename(self.svg_path or "design"))[0]
        return base + ".gcode"

    def save_gcode(self) -> None:
        if not (self.result and self.result.gcode) or self._dirty:
            return
        d = self.qs.value("conv/last_dir", os.path.expanduser("~"))
        path, _ = QFileDialog.getSaveFileName(self, "Save G-code", os.path.join(d, self.suggested_name()),
                                              "G-code (*.gcode *.nc)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.result.gcode)

    def _send(self) -> None:
        if self.result and self.result.gcode and not self._dirty:
            self.sendRequested.emit(self.result.gcode, self.suggested_name())
