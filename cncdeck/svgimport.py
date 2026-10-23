"""SVG import: shapes/paths -> flattened polylines in millimetres (SVG y-down, origin at the page corner).

Parsing and transforms (nested groups, viewBox, units, <use>) are done by `svgelements`; curves are
flattened here. The CAM stage works on these polylines, like jscut does after Snap.svg flattening.
"""
from __future__ import annotations

import io
import math
from dataclasses import dataclass, field

import numpy as np

MM_PER_INCH = 25.4


@dataclass
class Subpath:
    points: np.ndarray      # (N,2) mm, SVG coordinates (y down)
    closed: bool


@dataclass
class SvgShape:
    sid: str
    label: str
    kind: str
    subpaths: list[Subpath]
    fill_rule: str = "nonzero"


@dataclass
class SvgDoc:
    shapes: list[SvgShape] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    width_mm: float = 0.0
    height_mm: float = 0.0

    def bounds(self, ids: set[str] | None = None):
        pts = [sp.points for s in self.shapes if ids is None or s.sid in ids for sp in s.subpaths if len(sp.points)]
        if not pts:
            return None
        a = np.concatenate(pts)
        return a.min(axis=0), a.max(axis=0)


def simplify(points: np.ndarray, tol: float, closed: bool) -> np.ndarray:
    """Douglas-Peucker (iterative). For closed rings the start point is kept."""
    n = len(points)
    if n < 3 or tol <= 0:
        return points
    pts = np.vstack([points, points[:1]]) if closed else points
    keep = np.zeros(len(pts), bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        a, b = pts[i], pts[j]
        seg = pts[i + 1:j]
        d = b - a
        ln = math.hypot(d[0], d[1])
        if ln < 1e-12:
            dist = np.hypot(*(seg - a).T)
        else:
            dist = np.abs(d[0] * (seg[:, 1] - a[1]) - d[1] * (seg[:, 0] - a[0])) / ln
        k = int(np.argmax(dist))
        if dist[k] > tol:
            m = i + 1 + k
            keep[m] = True
            stack.append((i, m))
            stack.append((m, j))
    out = pts[keep]
    return out[:-1] if closed else out


def _sample_segment(seg, mm_per_unit: float, step_mm: float) -> list[tuple[float, float]]:
    from svgelements import Line
    if isinstance(seg, Line):
        return [(float(seg.end.x), float(seg.end.y))]
    # svgelements' seg.length() is a slow numerical integration: estimate the length from a coarse sampling
    coarse = [seg.point(k / 16) for k in range(17)]
    length = sum(math.hypot(b.x - a.x, b.y - a.y) for a, b in zip(coarse, coarse[1:])) * mm_per_unit
    n = int(min(1500, max(8, math.ceil(length / step_mm))))
    out = []
    for k in range(1, n + 1):
        p = seg.point(k / n)
        out.append((float(p.x), float(p.y)))
    return out


def parse_svg(text: str, dpi: float = 96.0, scale: float = 1.0, tolerance: float = 0.02) -> SvgDoc:
    """Parse SVG text. `dpi` is the px-per-inch of unit-less SVG lengths (96 = CSS/Inkscape >= 0.92)."""
    from svgelements import SVG, Close, Move, Path, Shape, Text

    doc = SvgDoc()
    try:
        svg = SVG.parse(io.StringIO(text), reify=True, ppi=dpi)
    except Exception as e:  # malformed XML etc.
        doc.warnings.append(f"Invalid SVG: {e}")
        return doc
    mm_per_unit = MM_PER_INCH / dpi * scale
    try:
        doc.width_mm = float(svg.width) * mm_per_unit
        doc.height_mm = float(svg.height) * mm_per_unit
    except Exception:
        pass
    counter = 0
    for el in svg.elements():
        if isinstance(el, Text):
            doc.warnings.append("Text ignored: convert it to paths (Inkscape: Path → Object to Path)")
            continue
        if not isinstance(el, Shape):
            continue
        vals = getattr(el, "values", {}) or {}
        if str(vals.get("display", "")).strip() == "none" or str(vals.get("visibility", "")).strip() in ("hidden", "collapse"):
            continue
        try:
            path = Path(el) if not isinstance(el, Path) else el
            path.approximate_arcs_with_cubics()
            segments = list(path.segments(transformed=True))
        except Exception as e:
            doc.warnings.append(f"Element {el.id or type(el).__name__} cannot be converted: {e}")
            continue
        subpaths: list[Subpath] = []
        cur: list[tuple[float, float]] = []
        closed = False

        def flush():
            nonlocal cur, closed
            if len(cur) >= 2:
                arr = np.array(cur, float) * mm_per_unit
                if not closed and np.hypot(*(arr[0] - arr[-1])) < 1e-3:
                    closed = True
                if closed and np.hypot(*(arr[0] - arr[-1])) < 1e-3:
                    arr = arr[:-1]
                arr = simplify(arr, tolerance, closed)
                if len(arr) >= 2:
                    subpaths.append(Subpath(arr, closed and len(arr) >= 3))
            cur, closed = [], False

        for seg in segments:
            if isinstance(seg, Move):
                flush()
                cur = [(float(seg.end.x), float(seg.end.y))]
            elif isinstance(seg, Close):
                closed = True
                flush()
            else:
                if not cur:
                    cur = [(float(seg.start.x), float(seg.start.y))]
                cur.extend(_sample_segment(seg, mm_per_unit, 0.25))
        flush()
        if not subpaths:
            continue
        counter += 1
        kind = type(el).__name__.lower()
        sid = str(el.id) if getattr(el, "id", None) else f"{kind}{counter}"
        fr = str(vals.get("fill-rule", "nonzero")).strip().lower()
        doc.shapes.append(SvgShape(sid=_unique(sid, doc), label=sid, kind=kind, subpaths=subpaths,
                                   fill_rule="evenodd" if fr == "evenodd" else "nonzero"))
    if not doc.shapes:
        doc.warnings.append("No shapes found in the SVG")
    return doc


def _unique(sid: str, doc: SvgDoc) -> str:
    used = {s.sid for s in doc.shapes}
    if sid not in used:
        return sid
    i = 2
    while f"{sid}_{i}" in used:
        i += 1
    return f"{sid}_{i}"
