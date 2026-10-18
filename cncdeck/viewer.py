"""Software-rendered 3D toolpath viewer (orthographic, orbit/pan/zoom) built on QPainter + numpy."""
from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import QLineF, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QImage, QLinearGradient, QPainter, QPen,
                           QRadialGradient)
from PySide6.QtWidgets import QWidget

from . import theme
from .gcode import FEED, RAPID, Program

N_BUCKETS = 24
# Top view as on these machines: origin top-left, X to the right, Y increasing downwards (Y is mirrored
# with respect to the CAD convention). Z points up.
Y_FLIP = np.array([1.0, -1.0, 1.0])
VIEWS = {  # yaw, pitch (degrees)
    "iso": (-35.0, 55.0), "top": (0.0, 0.0), "front": (0.0, 90.0), "right": (-90.0, 90.0), "left": (90.0, 90.0),
}


def _lerp(c1: QColor, c2: QColor, t: float) -> QColor:
    return QColor(int(c1.red() + (c2.red() - c1.red()) * t), int(c1.green() + (c2.green() - c1.green()) * t),
                  int(c1.blue() + (c2.blue() - c1.blue()) * t))


def depth_color(t: float) -> QColor:
    """t=0 deepest → warm orange, t=1 top → cool cyan."""
    stops = [QColor("#ff7a45"), QColor("#ffc53d"), QColor("#4ade80"), QColor("#38bdf8")]
    t = min(max(t, 0.0), 1.0) * (len(stops) - 1)
    i = min(int(t), len(stops) - 2)
    return _lerp(stops[i], stops[i + 1], t - i)


class ToolpathView(QWidget):
    viewChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(360, 300)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.ClickFocus)
        self.yaw, self.pitch = VIEWS["iso"]
        self.scale = 4.0
        self.pan = np.zeros(2)                  # screen-space pan in pixels
        self.center = np.zeros(3)               # world point at screen center
        self.program: Program | None = None
        self.tool = np.zeros(3)                 # tool position in work coordinates
        self.done_upto = 0                      # number of segments already executed
        self.show_rapids = True
        self.show_envelope = False
        self.envelope: tuple[np.ndarray, np.ndarray] | None = None  # (min,max) in work coords
        self._drag = None
        self._cache_key = None
        self._cache: QImage | None = None
        self._done_cache: QImage | None = None
        self._done_drawn = 0
        self._interactive = False
        self._settle = QTimer(self, singleShot=True, interval=140)
        self._settle.timeout.connect(self._end_interaction)
        self._seg_bucket = np.zeros(0, np.int32)
        self._zmin = 0.0
        self._zmax = 1.0

    # ---------- data ----------
    def set_program(self, prog: Program | None) -> None:
        self.program = prog
        self.done_upto = 0
        self._invalidate()
        if prog is not None and len(prog.path):
            p = prog.path
            z = (p.start[:, 2] + p.end[:, 2]) * 0.5
            feed_z = z[p.kind == FEED]
            ref = feed_z if len(feed_z) else z
            self._zmin, self._zmax = float(ref.min()), float(ref.max())
            span = max(self._zmax - self._zmin, 1e-6)
            self._seg_bucket = np.clip(((z - self._zmin) / span * (N_BUCKETS - 1)).round().astype(np.int32), 0, N_BUCKETS - 1)
            self.fit()
        else:
            self.update()

    def set_progress_line(self, source_line: int) -> None:
        """Mark everything up to (and including) the given source line as executed."""
        if self.program is None or not len(self.program.path):
            return
        k = int(np.searchsorted(self.program.path.line, source_line, side="right")) if source_line >= 0 else 0
        if k != self.done_upto:
            self.done_upto = k
            self.update()

    def set_tool(self, pos) -> None:
        self.tool = np.asarray(pos, dtype=float)
        self.update()

    def set_envelope(self, lo, hi) -> None:
        self.envelope = None if lo is None else (np.asarray(lo, float), np.asarray(hi, float))
        self.update()

    def _invalidate(self) -> None:
        self._cache_key = None
        self._done_cache = None
        self._done_drawn = 0

    # ---------- camera ----------
    def _rot(self) -> np.ndarray:
        y, p = math.radians(self.yaw), math.radians(self.pitch)
        cz, sz, cx, sx = math.cos(y), math.sin(y), math.cos(p), math.sin(p)
        rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
        rx = np.array([[1, 0, 0], [0, cx, sx], [0, -sx, cx]])  # +Z must point up on screen
        return rx @ rz

    def project(self, pts: np.ndarray) -> np.ndarray:
        r = self._rot()
        q = ((pts - self.center) * Y_FLIP) @ r.T
        out = np.empty((len(q), 2))
        out[:, 0] = self.width() / 2 + self.pan[0] + q[:, 0] * self.scale
        out[:, 1] = self.height() / 2 + self.pan[1] - q[:, 1] * self.scale
        return out

    def set_view(self, name: str) -> None:
        self.yaw, self.pitch = VIEWS[name]
        self.pan[:] = 0
        self.fit(keep_angles=True)

    def _scene_bounds(self):
        lo = np.array([-10.0, -10.0, -5.0])
        hi = np.array([50.0, 50.0, 10.0])
        if self.program is not None and len(self.program.path):
            lo = np.array(self.program.bounds_min)
            hi = np.array(self.program.bounds_max)
            lo = np.minimum(lo, 0.0)
            hi = np.maximum(hi, 0.0)
        return lo, hi

    def fit(self, keep_angles: bool = False) -> None:
        lo, hi = self._scene_bounds()
        self.center = (lo + hi) / 2
        corners = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
        q = ((corners - self.center) * Y_FLIP) @ self._rot().T
        w = max(q[:, 0].max() - q[:, 0].min(), 1.0)
        h = max(q[:, 1].max() - q[:, 1].min(), 1.0)
        self.scale = 0.84 * min(self.width() / w, self.height() / h)
        self.pan[:] = 0
        self._invalidate()
        self.update()

    # ---------- input ----------
    def _begin_interaction(self) -> None:
        self._interactive = True
        self._settle.start()

    def _end_interaction(self) -> None:
        self._interactive = False
        self._invalidate()
        self.update()

    def mousePressEvent(self, e):
        self.setFocus()
        self._drag = (e.button(), e.position())

    def mouseReleaseEvent(self, e):
        self._drag = None

    def orbit(self, dx: float, dy: float) -> None:
        """Grab-style orbit: the surface under the cursor follows the mouse."""
        self.yaw += dx * 0.4
        self.pitch = min(max(self.pitch - dy * 0.4, 0.0), 180.0)

    def mouseMoveEvent(self, e):
        if not self._drag:
            return
        btn, last = self._drag
        d = e.position() - last
        self._drag = (btn, e.position())
        if btn == Qt.LeftButton:
            self.orbit(d.x(), d.y())
        else:
            self.pan += np.array([d.x(), d.y()])
        self._begin_interaction()
        self._invalidate_view_only()
        self.update()

    def _invalidate_view_only(self) -> None:
        self._invalidate()

    def wheelEvent(self, e):
        f = 1.0015 ** e.angleDelta().y()
        pos = np.array([e.position().x(), e.position().y()])
        c = np.array([self.width() / 2, self.height() / 2]) + self.pan
        self.pan += (pos - c) * (1 - f)
        self.scale = min(max(self.scale * f, 0.05), 5000)
        self._begin_interaction()
        self._invalidate_view_only()
        self.update()

    def mouseDoubleClickEvent(self, e):
        self.fit()

    def resizeEvent(self, e):
        self._invalidate()

    # ---------- painting ----------
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        g = QLinearGradient(0, 0, 0, self.height())
        g.setColorAt(0, QColor("#101720"))
        g.setColorAt(1, QColor("#0a0e13"))
        p.fillRect(self.rect(), g)
        self._draw_grid(p)
        self._draw_bbox(p)
        self._draw_envelope(p)
        self._draw_path(p)
        self._draw_axes(p)
        self._draw_tool(p)
        self._draw_hud(p)
        p.end()

    def _grid_extent(self):
        lo, hi = self._scene_bounds()
        if self.envelope is not None and self.show_envelope:
            lo, hi = np.minimum(lo, self.envelope[0]), np.maximum(hi, self.envelope[1])
        lo = np.minimum(lo, self.tool - 5)
        hi = np.maximum(hi, self.tool + 5)
        return lo, hi

    def _draw_grid(self, p: QPainter) -> None:
        lo, hi = self._grid_extent()
        span = max(hi[0] - lo[0], hi[1] - lo[1])
        step = 1.0
        while span / step > 40:
            step *= 5 if int(round(math.log10(step) * 3)) % 3 == 0 else 2
        step = max(step, 1.0)
        major = step * 5
        x0, x1 = math.floor((lo[0] - step) / step) * step, math.ceil((hi[0] + step) / step) * step
        y0, y1 = math.floor((lo[1] - step) / step) * step, math.ceil((hi[1] + step) / step) * step
        xs = np.arange(x0, x1 + step / 2, step)
        ys = np.arange(y0, y1 + step / 2, step)
        for vals, horizontal in ((xs, False), (ys, True)):
            for v in vals:
                is_major = abs(v / major - round(v / major)) < 1e-6
                a = np.array([[x0, v, 0.0], [x1, v, 0.0]]) if horizontal else np.array([[v, y0, 0.0], [v, y1, 0.0]])
                s = self.project(a)
                p.setPen(QPen(QColor(255, 255, 255, 34 if is_major else 14), 1))
                p.drawLine(QPointF(*s[0]), QPointF(*s[1]))
        # tick labels on the major lines along X and Y
        p.setFont(QFont("monospace", 7))
        p.setPen(QColor(255, 255, 255, 70))
        for v in xs:
            if abs(v / major - round(v / major)) < 1e-6 and abs(v) > 1e-9:
                s = self.project(np.array([[v, y0, 0.0]]))[0]
                p.drawText(QPointF(s[0] + 2, s[1] + 11), f"{v:g}")
        for v in ys:
            if abs(v / major - round(v / major)) < 1e-6 and abs(v) > 1e-9:
                s = self.project(np.array([[x0, v, 0.0]]))[0]
                p.drawText(QPointF(s[0] - 26, s[1] - 2), f"{v:g}")

    def _draw_axes(self, p: QPainter) -> None:
        lo, hi = self._scene_bounds()
        ln = max(8.0, 0.12 * float(np.max(hi - lo)))
        o = np.zeros(3)
        p.setFont(QFont("sans", 9, QFont.Bold))
        for i, name in enumerate("XYZ"):
            e = np.zeros(3)
            e[i] = ln
            s = self.project(np.array([o, e]))
            col = QColor(theme.AXIS[name])
            p.setPen(QPen(col, 2.2, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(*s[0]), QPointF(*s[1]))
            p.drawText(QPointF(s[1][0] + 4, s[1][1] - 4), name)
        p.setBrush(QColor("#ffffff"))
        p.setPen(Qt.NoPen)
        s0 = self.project(np.array([o]))[0]
        p.drawEllipse(QPointF(*s0), 3, 3)

    def _draw_bbox(self, p: QPainter) -> None:
        if self.program is None or not len(self.program.path):
            return
        lo = np.array(self.program.bounds_min)
        hi = np.array(self.program.bounds_max)
        c = np.array([[x, y, z] for z in (lo[2], hi[2]) for x, y in
                      ((lo[0], lo[1]), (hi[0], lo[1]), (hi[0], hi[1]), (lo[0], hi[1]))])
        s = self.project(c)
        pen = QPen(QColor(255, 255, 255, 60), 1, Qt.DashLine)
        p.setPen(pen)
        for a, b in [(0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4), (0, 4), (1, 5), (2, 6), (3, 7)]:
            p.drawLine(QPointF(*s[a]), QPointF(*s[b]))
        p.setFont(QFont("monospace", 8))
        p.setPen(QColor(255, 255, 255, 150))
        size = hi - lo
        mid = lambda a, b: QPointF(*((s[a] + s[b]) / 2 + np.array([4, 12])))  # noqa: E731
        p.drawText(mid(0, 1), f"{size[0]:.1f}")
        p.drawText(mid(1, 2), f"{size[1]:.1f}")
        p.drawText(mid(1, 5), f"{size[2]:.1f}")

    def _draw_envelope(self, p: QPainter) -> None:
        if not (self.show_envelope and self.envelope):
            return
        lo, hi = self.envelope
        c = np.array([[x, y, z] for z in (lo[2], hi[2]) for x, y in
                      ((lo[0], lo[1]), (hi[0], lo[1]), (hi[0], hi[1]), (lo[0], hi[1]))])
        s = self.project(c)
        p.setPen(QPen(QColor(theme.WARN), 1, Qt.DotLine))
        for a, b in [(0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4), (0, 4), (1, 5), (2, 6), (3, 7)]:
            p.drawLine(QPointF(*s[a]), QPointF(*s[b]))

    def _build_layer(self, segs: slice | None, done: bool, decimate: int) -> QImage:
        img = QImage(self.size(), QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        if segs is not None:
            self._paint_segments(img, segs, done, decimate)
        return img

    def _paint_segments(self, img: QImage, sl: slice, done: bool, decimate: int) -> None:
        path = self.program.path
        qp = QPainter(img)
        qp.setRenderHint(QPainter.Antialiasing, not self._interactive)
        start = path.start[sl][::decimate]
        end = path.end[sl][::decimate]
        kind = path.kind[sl][::decimate]
        bucket = self._seg_bucket[sl][::decimate]
        if not len(kind):
            qp.end()
            return
        s0 = self.project(start.astype(float))
        s1 = self.project(end.astype(float))
        if self.show_rapids:
            m = kind == RAPID
            if m.any():
                col = QColor(120, 140, 170, 70 if not done else 35)
                qp.setPen(QPen(col, 1, Qt.DashLine))
                qp.drawLines([QLineF(a[0], a[1], b[0], b[1]) for a, b in zip(s0[m], s1[m])])
        m = kind == FEED
        for b in np.unique(bucket[m]):
            mm = m & (bucket == b)
            if done:
                col = QColor(62, 76, 96)
                width = 2.0
            else:
                col = depth_color(b / (N_BUCKETS - 1))
                width = 1.6
            qp.setPen(QPen(col, width, Qt.SolidLine, Qt.RoundCap))
            qp.drawLines([QLineF(a[0], a[1], c[0], c[1]) for a, c in zip(s0[mm], s1[mm])])
        qp.end()

    def _draw_path(self, p: QPainter) -> None:
        if self.program is None or not len(self.program.path):
            return
        n = len(self.program.path)
        decimate = max(1, n // 25000) if self._interactive else 1
        key = (self.width(), self.height(), round(self.yaw, 2), round(self.pitch, 2), round(self.scale, 4),
               tuple(np.round(self.pan, 1)), tuple(self.center), decimate, self.show_rapids, id(self.program))
        if key != self._cache_key or self._cache is None:
            self._cache = self._build_layer(slice(0, n), False, decimate)
            self._cache_key = key
            self._done_cache = None
            self._done_drawn = 0
        p.drawImage(0, 0, self._cache)
        if self.done_upto > 0:
            if self._done_cache is None or self._done_drawn > self.done_upto:
                self._done_cache = self._build_layer(None, True, 1)
                self._done_drawn = 0
            if self._done_drawn < self.done_upto:
                self._paint_segments(self._done_cache, slice(self._done_drawn, self.done_upto), True, decimate)
                self._done_drawn = self.done_upto
            # erase the coloured version of executed part by drawing the grey one on top
            p.drawImage(0, 0, self._done_cache)

    def _draw_tool(self, p: QPainter) -> None:
        t = self.tool
        tip, top = np.array(t), np.array(t) + np.array([0, 0, 14.0])
        ground = np.array([t[0], t[1], 0.0])
        s = self.project(np.array([tip, top, ground]))
        if abs(t[2]) > 0.01:
            p.setPen(QPen(QColor(255, 255, 255, 60), 1, Qt.DotLine))
            p.drawLine(QPointF(*s[0]), QPointF(*s[2]))
        # shank
        p.setPen(QPen(QColor("#e5e7eb"), 3, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(*s[0]), QPointF(*s[1]))
        grad = QRadialGradient(QPointF(*s[0]), 16)
        grad.setColorAt(0, QColor(45, 212, 191, 150))
        grad.setColorAt(1, QColor(45, 212, 191, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(grad))
        p.drawEllipse(QPointF(*s[0]), 16, 16)
        p.setBrush(QColor(theme.ACCENT))
        p.setPen(QPen(QColor("#04211d"), 1.5))
        p.drawEllipse(QPointF(*s[0]), 4.5, 4.5)

    def _draw_hud(self, p: QPainter) -> None:
        p.setFont(QFont("monospace", 9))
        p.setPen(QColor(255, 255, 255, 120))
        if self.program is None or not len(self.program.path):
            p.setFont(QFont("sans", 13))
            p.setPen(QColor(255, 255, 255, 90))
            p.drawText(self.rect(), Qt.AlignCenter, "Apri un file G-code per vedere il percorso\n(trascina qui un file .nc / .gcode)")
        else:
            lo, hi = self.program.bounds_min, self.program.bounds_max
            p.drawText(QPointF(12, self.height() - 12),
                       f"X {lo[0]:.1f}…{hi[0]:.1f}   Y {lo[1]:.1f}…{hi[1]:.1f}   Z {lo[2]:.1f}…{hi[2]:.1f} mm")
            # legend: depth ramp
            x0, y0, wd, ht = self.width() - 28, 52, 8, 110
            for i in range(ht):
                p.fillRect(QRectF(x0, y0 + i, wd, 1.2), depth_color(1 - i / ht))
            p.setFont(QFont("monospace", 8))
            p.setPen(QColor(255, 255, 255, 150))
            p.drawText(QPointF(x0 - 38, y0 + 8), f"{self._zmax:.1f}")
            p.drawText(QPointF(x0 - 38, y0 + ht), f"{self._zmin:.1f}")
