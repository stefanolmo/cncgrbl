"""Height map (auto-leveling): probe grid, bilinear surface and G-code correction.

The surface is stored in *machine* coordinates, so it stays valid if the work zero is changed after the
probing; it is shifted into work coordinates (current WCO) only when a program is corrected.
GRBL 1.1 has no native compensation, so the correction is applied to the commands before streaming:
every cutting move is split where it crosses a grid line and the surface height is added to its Z.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field

import numpy as np

from .gcode import WORD_RE, _arc_points, _split_words, arc_center_from_radius

FORMAT = 1
INCH = 25.4


class HeightMapError(ValueError):
    """The program cannot be corrected safely (message is shown to the user)."""


def grid_axes(lo: float, hi: float, step: float) -> np.ndarray:
    """Evenly spaced probe positions covering [lo, hi] with spacing <= step (at least 2 points)."""
    step = max(step, 0.5)
    n = max(2, int(math.ceil((hi - lo) / step - 1e-9)) + 1)
    return np.linspace(lo, hi, n)


def serpentine(nx: int, ny: int) -> list[tuple[int, int]]:
    """Probe order (row, column): one row after the other, alternating direction to save travel."""
    out = []
    for iy in range(ny):
        cols = range(nx) if iy % 2 == 0 else range(nx - 1, -1, -1)
        out.extend((iy, ix) for ix in cols)
    return out


@dataclass
class HeightMap:
    xs: np.ndarray                      # machine X of the columns, ascending
    ys: np.ndarray                      # machine Y of the rows, ascending
    z: np.ndarray                       # (ny, nx) machine Z of the *surface*; NaN = not probed yet
    created: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M"))
    note: str = ""

    @classmethod
    def blank(cls, xs, ys) -> "HeightMap":
        xs, ys = np.asarray(xs, float), np.asarray(ys, float)
        return cls(xs, ys, np.full((len(ys), len(xs)), np.nan))

    @property
    def shape(self) -> tuple[int, int]:
        return self.z.shape

    @property
    def complete(self) -> bool:
        return bool(np.isfinite(self.z).all())

    @property
    def area(self) -> tuple[float, float, float, float]:
        return float(self.xs[0]), float(self.ys[0]), float(self.xs[-1]), float(self.ys[-1])

    @property
    def spread(self) -> float:
        """Peak-to-peak surface height (mm)."""
        return float(np.nanmax(self.z) - np.nanmin(self.z)) if np.isfinite(self.z).any() else 0.0

    def shifted(self, wco) -> "HeightMap":
        """The same surface expressed in work coordinates (work = machine - WCO)."""
        return HeightMap(self.xs - wco[0], self.ys - wco[1], self.z - wco[2], self.created, self.note)

    def z_at(self, x: float, y: float) -> float:
        """Bilinear surface height; outside the probed area the nearest edge value is used."""
        xs, ys = self.xs, self.ys
        x = min(max(x, xs[0]), xs[-1])
        y = min(max(y, ys[0]), ys[-1])
        i = min(max(int(np.searchsorted(xs, x, "right")) - 1, 0), len(xs) - 2)
        j = min(max(int(np.searchsorted(ys, y, "right")) - 1, 0), len(ys) - 2)
        tx = (x - xs[i]) / (xs[i + 1] - xs[i])
        ty = (y - ys[j]) / (ys[j + 1] - ys[j])
        z = self.z
        return float((z[j, i] * (1 - tx) + z[j, i + 1] * tx) * (1 - ty)
                     + (z[j + 1, i] * (1 - tx) + z[j + 1, i + 1] * tx) * ty)

    def covers(self, lo, hi, tol: float = 0.05) -> bool:
        x0, y0, x1, y1 = self.area
        return lo[0] >= x0 - tol and lo[1] >= y0 - tol and hi[0] <= x1 + tol and hi[1] <= y1 + tol

    # ---------- persistence ----------
    def to_json(self) -> str:
        return json.dumps({"format": FORMAT, "created": self.created, "note": self.note,
                           "xs": self.xs.tolist(), "ys": self.ys.tolist(),
                           "z": [[None if not math.isfinite(v) else v for v in row] for row in self.z.tolist()]})

    @classmethod
    def from_json(cls, text: str) -> "HeightMap":
        try:
            d = json.loads(text)
            xs, ys = np.array(d["xs"], float), np.array(d["ys"], float)
            z = np.array([[np.nan if v is None else v for v in row] for row in d["z"]], float)
            if z.shape != (len(ys), len(xs)) or len(xs) < 2 or len(ys) < 2:
                raise ValueError("inconsistent dimensions")
            if np.any(np.diff(xs) <= 0) or np.any(np.diff(ys) <= 0):
                raise ValueError("axes not increasing")
            return cls(xs, ys, z, d.get("created", ""), d.get("note", ""))
        except (KeyError, TypeError, ValueError) as e:
            raise HeightMapError(f"Invalid height map file: {e}") from e


def _fmt(v: float, inch: bool) -> str:
    s = f"{v / INCH:.5f}" if inch else f"{v:.4f}"
    s = s.rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def _split_at_grid(p0, p1, hm: HeightMap) -> list[np.ndarray]:
    """Points along p0→p1 (excluding p0, including p1) at every grid line the XY projection crosses."""
    ts = [1.0]
    for axis, grid in ((0, hm.xs), (1, hm.ys)):
        a, b = p0[axis], p1[axis]
        if abs(b - a) < 1e-9:
            continue
        lo, hi = (a, b) if a < b else (b, a)
        for g in grid[(grid > lo + 1e-9) & (grid < hi - 1e-9)]:
            ts.append((g - a) / (b - a))
    ts = sorted(set(round(t, 9) for t in ts))
    p0, p1 = np.asarray(p0, float), np.asarray(p1, float)
    return [p0 + (p1 - p0) * t for t in ts]


def correct_program(commands: list[tuple[int, str]], hm: HeightMap, start=None) -> list[tuple[int, str]]:
    """Return `commands` with the surface height of `hm` (work coordinates) added to every move's Z.

    `start` is the current work position (mm) used as the origin of the first move. Cutting moves are
    split at grid lines; arcs become G1 segments (Z varies along them). Rapid moves only get their end
    point corrected. Raises HeightMapError for programs that change the work coordinate system.
    """
    if not hm.complete:
        raise HeightMapError("The height map is incomplete")
    out: list[tuple[int, str]] = []
    abs_ = True          # distance mode as seen by the program
    out_abs = True       # distance mode the machine is actually in after the lines emitted so far
    inch = False
    plane = 17
    motion: int | None = None
    pos: list[float | None] = [None, None, None] if start is None else [float(v) for v in start]

    def emit(src: int, text: str) -> None:
        out.append((src, text))

    for src, cmd in commands:
        if cmd.startswith("$"):
            emit(src, cmd)
            continue
        gs, ms, vals = _split_words(cmd)
        for g in gs:
            if g in (92, 10) or g in (55, 56, 57, 58, 59):
                raise HeightMapError(f"Line {src + 1}: G{g:g} changes the coordinate system, "
                                     "which is incompatible with the height map")
        had_mode = 90 in gs or 91 in gs
        if 90 in gs:
            abs_ = True
        if 91 in gs:
            abs_ = False
        if 20 in gs:
            inch = True
        if 21 in gs:
            inch = False
        for g in (17, 18, 19):
            if g in gs:
                plane = int(g)
        k = INCH if inch else 1.0
        has_axis = any(c in vals for c in "XYZ")

        passthrough = (not has_axis or 53 in gs or any(g in (28, 30, 38.2, 38.3, 38.4, 38.5) for g in gs))
        if not passthrough:
            for g in gs:
                if g in (0, 1, 2, 3):
                    motion = int(g)
            passthrough = motion is None
        if passthrough:
            if has_axis and not had_mode and out_abs != abs_:
                emit(src, "G90" if abs_ else "G91")
                out_abs = abs_
            emit(src, cmd)
            if had_mode:
                out_abs = abs_
            if has_axis:
                # position after G53/G28/G30/probing is not known to the program
                pos = [None, None, None]
            continue

        if any(p is None for p in pos):
            raise HeightMapError(f"Line {src + 1}: start position unknown "
                                 "(after G28/G30/G53 or with no initial position): use absolute moves")
        tgt = list(pos)
        for i, c in enumerate("XYZ"):
            if c in vals:
                tgt[i] = vals[c] * k if abs_ else pos[i] + vals[c] * k
        extra_g = [g for g in gs if g not in (0, 1, 2, 3, 90, 91, 17, 18, 19, 20, 21)]
        pre = "".join(f"G{g:g}" for g in gs if g in (17, 18, 19, 20, 21)) + "".join(f"G{g:g}" for g in extra_g)
        if pre:
            emit(src, pre)
        feed_s = "".join(f"{l}{n}" for l, n in WORD_RE.findall(cmd) if l in "FS")
        m_s = "".join(f"M{m:g}" for m in ms)
        gprefix = "" if out_abs else "G90"

        def words(p, with_xy=True, with_z=True):
            return (("X" + _fmt(p[0], inch) + "Y" + _fmt(p[1], inch) if with_xy else "")
                    + ("Z" + _fmt(p[2], inch) if with_z else ""))

        if motion == 0:
            p = list(tgt)
            has_xy = "X" in vals or "Y" in vals
            with_z = "Z" in vals or p[2] <= 0.5
            if with_z:
                p[2] = tgt[2] + hm.z_at(tgt[0], tgt[1])
            emit(src, f"{gprefix}G0" + words(p, has_xy, with_z) + feed_s + m_s)
            out_abs = True
            pos = tgt
            continue

        a = np.array(pos, float)
        b = np.array(tgt, float)
        if motion == 1:
            pts = [b]
        else:
            if plane != 17:
                raise HeightMapError(f"Line {src + 1}: arcs on the G18/G19 planes are not supported with the height map")
            cw = motion == 2
            if "R" in vals:
                r = vals["R"] * k
                if (b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2 == 0 or 4 * r * r < (b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2:
                    raise HeightMapError(f"Line {src + 1}: arc with invalid radius")
                ca, cb = arc_center_from_radius(a[0], a[1], b[0], b[1], r, cw)
            else:
                ca, cb = a[0] + vals.get("I", 0.0) * k, a[1] + vals.get("J", 0.0) * k
            pts = list(_arc_points(a, b, ca, cb, 0, 1, 2, cw))
        has_xy = "X" in vals or "Y" in vals or motion in (2, 3)
        first = True
        prev = a
        for target in pts:
            for q in _split_at_grid(prev, target, hm):
                z = q[2] + hm.z_at(q[0], q[1])
                text = "G1" + words([q[0], q[1], z], has_xy, True)
                if first:
                    text = gprefix + text + feed_s
                    first = False
                emit(src, text)
            prev = target
        if m_s:
            out[-1] = (out[-1][0], out[-1][1] + m_s)
        out_abs = True
        pos = tgt

    if out_abs != abs_:
        emit(commands[-1][0] if commands else 0, "G90" if abs_ else "G91")
    return out
