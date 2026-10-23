"""Automatic probing of a grid of points (G38.2) to build a HeightMap."""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal

from .grbl import GrblConnection
from .heightmap import HeightMap, grid_axes, serpentine


@dataclass
class ProbeParams:
    area: tuple[float, float, float, float]   # x0, y0, x1, y1 in work coordinates (mm)
    step: float = 10.0
    safe_z: float = 5.0         # travel height (work Z)
    depth: float = 3.0          # probing goes down to -depth (work Z); no contact there = failure
    clearance: float = 1.0      # fast descent stops this far above the previous contact height
    feed: float = 25.0          # probing speed (mm/min)
    thickness: float = 0.0      # touch plate thickness, subtracted from the contact height

    def validate(self) -> str | None:
        x0, y0, x1, y1 = self.area
        if x1 < x0 or y1 < y0:
            return "Area non valida"
        if self.step < 1.0:
            return "Il passo della griglia deve essere almeno 1 mm"
        if self.depth <= 0 or self.depth > 20:
            return "La profondità massima di tastatura deve essere tra 0 e 20 mm"
        if self.safe_z <= self.clearance or self.safe_z < 1.0:
            return "La Z di sicurezza deve essere maggiore dell'avvicinamento (e almeno 1 mm)"
        if not 1.0 <= self.feed <= 300.0:
            return "La velocità di tastatura deve essere tra 1 e 300 mm/min"
        return None


class HeightMapProbe(QObject):
    """Drives GRBL through the probing grid, one point at a time, and reports a HeightMap (machine coords)."""

    progress = Signal(int, int)       # done, total
    finished = Signal(object)         # HeightMap
    failed = Signal(str)

    def __init__(self, conn: GrblConnection, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.active = False
        conn.probeResult.connect(self._on_probe)
        conn.alarmRaised.connect(self._on_alarm)
        conn.console.connect(self._on_console)
        conn.connectionChanged.connect(self._on_connection)

    # ---------- public ----------
    def start(self, p: ProbeParams) -> str | None:
        """Begin probing; returns an error message if it cannot start."""
        if self.active:
            return "Tastatura già in corso"
        err = p.validate()
        if err:
            return err
        st = self.conn.status
        if st.base_state != "Idle" or self.conn.is_streaming:
            return "La macchina deve essere ferma (Idle) e senza programma in esecuzione"
        if "P" in st.pins:
            return "La sonda risulta già chiusa (Pn:P): controlla pinza e collegamenti prima di tastare"
        self.p = p
        self.wco = tuple(st.wco)
        x0, y0, x1, y1 = p.area
        self.xs_w = grid_axes(x0, x1, p.step)
        self.ys_w = grid_axes(y0, y1, p.step)
        self.order = serpentine(len(self.xs_w), len(self.ys_w))
        self.map = HeightMap.blank(self.xs_w + self.wco[0], self.ys_w + self.wco[1])
        self.i = 0
        self.last_z_work: float | None = None
        self.active = True
        self._send(f"G21G90G0Z{p.safe_z:g}")
        self._go(0)
        self.progress.emit(0, len(self.order))
        return None

    @property
    def total(self) -> int:
        return len(self.order) if self.active else 0

    def abort(self, reason: str = "Tastatura interrotta") -> None:
        if not self.active:
            return
        self.active = False
        self.conn.stop()
        self.failed.emit(reason)

    # ---------- internals ----------
    def _send(self, text: str) -> None:
        self.conn.send_line(text, silent=True)

    def _target(self, i: int) -> tuple[float, float]:
        iy, ix = self.order[i]
        return float(self.xs_w[ix]), float(self.ys_w[iy])

    def _go(self, i: int) -> None:
        p = self.p
        x, y = self._target(i)
        if i > 0:
            self._send(f"G0Z{p.safe_z:g}")
        self._send(f"G0X{x:.3f}Y{y:.3f}")
        if self.last_z_work is not None:
            approach = min(p.safe_z, self.last_z_work + p.clearance)
            if approach > -p.depth + 0.5:
                self._send(f"G0Z{approach:.3f}")
        self._send(f"G38.2Z{-p.depth:g}F{p.feed:g}")

    def _fail(self, msg: str) -> None:
        if not self.active:
            return
        self.active = False
        self.conn.stop()
        self.failed.emit(msg)

    def _on_probe(self, mx: float, my: float, mz: float, ok: bool) -> None:
        if not self.active:
            return
        x, y = self._target(self.i)
        if abs(mx - (x + self.wco[0])) > 0.1 or abs(my - (y + self.wco[1])) > 0.1:
            return          # a probe made by hand, not ours
        if not ok:
            self._fail(f"Nessun contatto nel punto {self.i + 1}/{len(self.order)} (X{x:.1f} Y{y:.1f}) entro "
                       f"{self.p.depth:g} mm sotto lo zero. Controlla pinza e altezza di Z0, poi sblocca ($X) e rialza Z.")
            return
        iy, ix = self.order[self.i]
        self.map.z[iy, ix] = mz - self.p.thickness
        self.last_z_work = mz - self.wco[2]
        self.i += 1
        self.progress.emit(self.i, len(self.order))
        if self.i >= len(self.order):
            self.active = False
            self._send(f"G0Z{self.p.safe_z:g}")
            self.finished.emit(self.map)
        else:
            self._go(self.i)

    def _on_alarm(self, code: int, msg: str) -> None:
        self._fail(f"Allarme {code} durante la tastatura: {msg}")

    def _on_console(self, text: str, kind: str) -> None:
        if self.active and kind == "error" and text.startswith("error:"):
            self._fail(f"GRBL ha rifiutato un comando di tastatura: {text}")

    def _on_connection(self, ok: bool, _msg: str) -> None:
        if not ok and self.active:
            self.active = False
            self.failed.emit("Connessione persa durante la tastatura")
