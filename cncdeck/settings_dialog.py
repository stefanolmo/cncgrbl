"""Editor for GRBL `$` settings."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout)

from .grbl import SETTING_INFO


class GrblSettingsDialog(QDialog):
    applyRequested = Signal(list)   # list of "$n=value" strings
    refreshRequested = Signal()

    def __init__(self, settings: dict[int, float], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Impostazioni GRBL ($$)")
        self.resize(640, 640)
        lay = QVBoxLayout(self)
        hint = QLabel("Le modifiche vengono scritte nella EEPROM della scheda. Cambia solo ciò che sai: "
                      "valori errati su passi/mm o velocità possono far muovere la macchina in modo imprevisto.")
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        lay.addWidget(hint)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["$", "Descrizione", "Valore", "Unità"])
        self.table.verticalHeader().hide()
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 50)
        self.table.setColumnWidth(2, 100)
        self.table.setColumnWidth(3, 80)
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.btn_refresh = QPushButton("Rileggi")
        self.btn_apply = QPushButton("Scrivi modifiche")
        self.btn_apply.setObjectName("primary")
        self.btn_close = QPushButton("Chiudi")
        row.addWidget(self.btn_refresh)
        row.addStretch()
        row.addWidget(self.btn_close)
        row.addWidget(self.btn_apply)
        lay.addLayout(row)
        self.btn_close.clicked.connect(self.close)
        self.btn_refresh.clicked.connect(self.refreshRequested)
        self.btn_apply.clicked.connect(self._apply)
        self._orig: dict[int, str] = {}
        self.load(settings)

    @staticmethod
    def _fmt(v: float) -> str:
        return f"{v:g}" if abs(v - round(v)) < 1e-9 else f"{v:.3f}".rstrip("0").rstrip(".")

    def load(self, settings: dict[int, float]) -> None:
        # a refresh (e.g. the reply to the `$$` sent on open) must not wipe values the user already edited
        edited: dict[int, str] = {}
        for r in range(self.table.rowCount()):
            n = int(self.table.item(r, 0).text()[1:])
            txt = self.table.item(r, 2).text()
            if txt != self._orig.get(n):
                edited[n] = txt
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self._orig = {}
        for n in sorted(settings):
            r = self.table.rowCount()
            self.table.insertRow(r)
            desc, unit = SETTING_INFO.get(n, ("(non documentato)", ""))
            val = self._fmt(settings[n])
            self._orig[n] = val
            for c, text in enumerate((f"${n}", desc, edited.get(n, val), unit)):
                it = QTableWidgetItem(text)
                if c != 2:
                    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                self.table.setItem(r, c, it)
        self.table.blockSignals(False)

    def _apply(self) -> None:
        cmds = []
        for r in range(self.table.rowCount()):
            n = int(self.table.item(r, 0).text()[1:])
            v = self.table.item(r, 2).text().strip()
            try:
                float(v)
            except ValueError:
                continue
            if v != self._orig.get(n):
                cmds.append(f"${n}={v}")
        if cmds:
            self.applyRequested.emit(cmds)
