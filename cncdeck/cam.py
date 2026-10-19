"""SVG polylines -> toolpaths -> G-code, following the approach of jscut (tbfleming/jscut).

jscut keeps its geometry as integer Clipper paths and builds toolpaths with Clipper offsetting:
  * outline(Inside/Outside): concentric offsets of the shape, `width` wide, stepped by tool*(1-overlap)
  * pocket: repeated inward offsets until nothing is left
  * engrave: the centre line itself
  * mergePaths: chain the rings into as few continuous paths as possible when the connecting move stays
    inside the already-cut area
Then G-code is produced pass by pass (pass depth) with plunge / cut / retract. This module re-implements that
with pyclipper (Python binding of the same Clipper library) and tidies the output for GRBL:
G0 rapids, modal feed words, no duplicate/collinear points, short lines.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pyclipper

from .svgimport import SvgDoc, simplify

SCALE = 10000.0            # Clipper integer units per mm (0.1 µm)
ARC_TOL = 0.005            # mm, round-join arc tolerance


@dataclass
class CamPath:
    points: np.ndarray     # (N,2) mm, SVG orientation (y down) until `emit` flips it
    closed: bool


@dataclass
class Operation:
    name: str = "Operazione"
    kind: str = "outside"            # inside | outside | pocket | engrave
    shapes: set[str] | None = None   # None = all shapes
    combine: str = "union"           # union | intersect | diff | xor
    climb: bool = False              # False = conventional
    cut_depth: float = 1.0           # mm below the top of the material
    pass_depth: float = 0.5
    step_over: float = 0.4           # fraction of tool diameter (jscut "overlap" = 1 - step_over)
    width: float = 0.0               # inside/outside band width; 0 = tool diameter (one pass)
    margin: float = 0.0              # extra offset of the geometry (outside: grow, inside: shrink)
    ramp: bool = False


@dataclass
class Job:
    tool_diameter: float = 3.175
    cut_feed: float = 500.0          # mm/min
    plunge_feed: float = 150.0
    safe_z: float = 5.0
    approach_z: float = 1.0          # rapid down to here, then plunge at plunge_feed
    spindle: bool = False            # emit M3 S.. / M5 (jscut does not)
    spindle_rpm: float = 1000.0
    units: str = "mm"                # output units: mm | in
    origin: str = "top-left"         # svg | top-left | bottom-left
    tolerance: float = 0.01          # mm, simplification of the final toolpath
    name: str = ""


@dataclass
class CamResult:
    gcode: str = ""
    guides: list[np.ndarray] = field(default_factory=list)   # design outlines in G-code coords (N,3)
    warnings: list[str] = field(default_factory=list)
    n_paths: int = 0


# ---------------------------------------------------------------- Clipper helpers
def _to_clip(points: np.ndarray):
    return [(int(round(x * SCALE)), int(round(y * SCALE))) for x, y in points]


def _from_clip(path) -> np.ndarray:
    return np.array(path, dtype=float) / SCALE


def shape_polygons(shape) -> list:
    """Closed subpaths of one SVG shape as Clipper paths, resolved with the shape's fill rule."""
    paths = [_to_clip(sp.points) for sp in shape.subpaths if sp.closed and len(sp.points) >= 3]
    if not paths:
        return []
    pc = pyclipper.Pyclipper()
    pc.AddPaths(paths, pyclipper.PT_SUBJECT, True)
    ft = pyclipper.PFT_EVENODD if shape.fill_rule == "evenodd" else pyclipper.PFT_NONZERO
    return pc.Execute(pyclipper.CT_UNION, ft, ft)


_CLIP = {"union": pyclipper.CT_UNION, "intersect": pyclipper.CT_INTERSECTION,
         "diff": pyclipper.CT_DIFFERENCE, "xor": pyclipper.CT_XOR}


def combine(polys_per_shape: list[list], mode: str) -> list:
    """jscut 'Combine': Union / Intersect / Diff (first minus the rest) / Xor over the selected shapes."""
    polys_per_shape = [p for p in polys_per_shape if p]
    if not polys_per_shape:
        return []
    if mode == "union" or len(polys_per_shape) == 1:
        pc = pyclipper.Pyclipper()
        for p in polys_per_shape:
            pc.AddPaths(p, pyclipper.PT_SUBJECT, True)
        return pc.Execute(pyclipper.CT_UNION, pyclipper.PFT_NONZERO, pyclipper.PFT_NONZERO)
    acc = polys_per_shape[0]
    for nxt in polys_per_shape[1:]:
        pc = pyclipper.Pyclipper()
        pc.AddPaths(acc, pyclipper.PT_SUBJECT, True)
        pc.AddPaths(nxt, pyclipper.PT_CLIP, True)
        acc = pc.Execute(_CLIP[mode], pyclipper.PFT_NONZERO, pyclipper.PFT_NONZERO)
    return acc


def offset(paths: list, delta_mm: float) -> list:
    if not paths:
        return []
    if abs(delta_mm) < 1e-9:
        return [list(p) for p in paths]
    co = pyclipper.PyclipperOffset(2.0, ARC_TOL * SCALE)
    co.AddPaths(paths, pyclipper.JT_ROUND, pyclipper.ET_CLOSEDPOLYGON)
    return co.Execute(delta_mm * SCALE)


def _clip_ccw(paths: list) -> list:
    return paths


def _reverse(p):
    return list(reversed(p))


# ---------------------------------------------------------------- jscut operations
def _inside_bounds(bounds: list, x: float, y: float) -> bool:
    """Even-odd membership of a point in a set of Clipper polygons (boundary counts as inside)."""
    pt = (int(round(x * SCALE)), int(round(y * SCALE)))
    n = 0
    for poly in bounds:
        r = pyclipper.PointInPolygon(pt, poly)
        if r == -1:
            return True
        n += 1 if r == 1 else 0
    return n % 2 == 1


def _segment_inside(bounds: list, a, b, step_mm: float = 0.2) -> bool:
    d = math.hypot(b[0] - a[0], b[1] - a[1])
    n = max(1, int(math.ceil(d / step_mm)))
    for k in range(n + 1):
        t = k / n
        if not _inside_bounds(bounds, a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t):
            return False
    return True


def merge_paths(bounds: list, paths: list) -> list[CamPath]:
    """jscut mergePaths: nearest-neighbour chaining; join two rings when the straight link stays in `bounds`."""
    rings = [_from_clip(p) for p in paths if len(p) >= 3]
    if not rings:
        return []
    current = np.vstack([rings[0], rings[0][:1]])
    rings[0] = None
    merged: list[np.ndarray] = []
    left = [i for i in range(1, len(rings))]
    while left:
        cp = current[-1]
        best = (None, None, 1e18)
        for i in left:
            r = rings[i]
            d = np.hypot(r[:, 0] - cp[0], r[:, 1] - cp[1])
            k = int(np.argmin(d))
            if d[k] < best[2]:
                best = (i, k, float(d[k]))
        i, k, _ = best
        r = rings[i]
        left.remove(i)
        r = np.vstack([r[k:], r[:k]])
        r = np.vstack([r, r[:1]])
        if _segment_inside(bounds, cp, r[0]):
            current = np.vstack([current, r])
        else:
            merged.append(current)
            current = r
    merged.append(current)
    return [CamPath(m, True) for m in merged]


def outline(geometry: list, tool: float, inside: bool, width: float, step_over: float, climb: bool) -> list[CamPath]:
    """jscut Cam.outline: concentric offsets of the geometry, `width` wide."""
    overlap_step = max(tool * step_over, tool * 0.05)
    width = max(width, tool)
    current_width = tool
    all_paths: list = []
    if inside:
        current = offset(geometry, -tool / 2)
        bounds = _difference(current, offset(geometry, -(width - tool / 2)))
        each = -overlap_step
        need_reverse = climb
    else:
        current = offset(geometry, tool / 2)
        bounds = _difference(offset(geometry, width - tool / 2), current)
        each = overlap_step
        need_reverse = not climb
    guard = 0
    while current_width <= width + 1e-9 and current and guard < 500:
        guard += 1
        cur = [_reverse(p) for p in current] if need_reverse else current
        all_paths = cur + all_paths
        nxt = current_width + overlap_step
        if nxt > width and width - current_width > 1e-9:
            current = offset(current, math.copysign(width - current_width, each))
            cur = [_reverse(p) for p in current] if need_reverse else current
            all_paths = cur + all_paths
            break
        current_width = nxt
        current = offset(current, each)
    return merge_paths(bounds, all_paths)       # empty bounds (single pass): rings are never joined


def pocket(geometry: list, tool: float, step_over: float, climb: bool) -> list[CamPath]:
    """jscut Cam.pocket: inward offsets until empty, innermost ring first."""
    current = offset(geometry, -tool / 2)
    bounds = [list(p) for p in current]
    each = -max(tool * step_over, tool * 0.05)
    all_paths: list = []
    guard = 0
    while current and guard < 2000:
        guard += 1
        cur = [_reverse(p) for p in current] if climb else current
        all_paths = cur + all_paths
        current = offset(current, each)
    return merge_paths(bounds, all_paths)


def _difference(a: list, b: list) -> list:
    if not a:
        return []
    pc = pyclipper.Pyclipper()
    pc.AddPaths(a, pyclipper.PT_SUBJECT, True)
    if b:
        pc.AddPaths(b, pyclipper.PT_CLIP, True)
    return pc.Execute(pyclipper.CT_DIFFERENCE, pyclipper.PFT_NONZERO, pyclipper.PFT_NONZERO)


def engrave(doc: SvgDoc, ids: set[str] | None, climb: bool) -> list[CamPath]:
    """jscut Engrave: follow the centre line of the selected shapes (open paths allowed)."""
    out = []
    for s in doc.shapes:
        if ids is not None and s.sid not in ids:
            continue
        for sp in s.subpaths:
            pts = sp.points[::-1] if climb else sp.points
            pts = np.vstack([pts, pts[:1]]) if sp.closed else pts
            out.append(CamPath(np.array(pts), sp.closed))
    return out


def build_paths(doc: SvgDoc, op: Operation, job: Job, warnings: list[str]) -> list[CamPath]:
    ids = op.shapes
    sel = [s for s in doc.shapes if ids is None or s.sid in ids]
    if not sel:
        warnings.append(f"{op.name}: nessuna forma selezionata")
        return []
    if op.kind == "engrave":
        return engrave(doc, ids, op.climb)
    for s in sel:
        if not any(sp.closed for sp in s.subpaths):
            warnings.append(f"{op.name}: «{s.label}» è un tracciato aperto, ignorato (usa Incisione)")
    geometry = combine([shape_polygons(s) for s in sel], op.combine)
    if not geometry:
        warnings.append(f"{op.name}: nessuna forma chiusa nella selezione")
        return []
    if abs(op.margin) > 1e-9:
        geometry = offset(geometry, op.margin if op.kind != "inside" else -op.margin)
    # Clipper works in SVG (y-down) orientation, which is mirrored at output: invert so that, with a clockwise
    # spindle (M3) seen from above, "conventional" means outside = CW, inside/pocket = CCW and "climb" the opposite
    climb = not op.climb
    if op.kind == "pocket":
        paths = pocket(geometry, job.tool_diameter, op.step_over, climb)
    else:
        paths = outline(geometry, job.tool_diameter, op.kind == "inside", op.width or job.tool_diameter,
                        op.step_over, climb)
    if not paths:
        warnings.append(f"{op.name}: l'utensile da {job.tool_diameter:g} mm è troppo grande per questa forma")
    return paths


# ---------------------------------------------------------------- G-code
def _fmt(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def origin_offset(doc: SvgDoc, ids: set[str] | None, origin: str):
    b = doc.bounds(ids)
    if origin == "svg" or b is None:
        return 0.0, 0.0
    (x0, y0), (x1, y1) = b
    return (float(x0), float(y0)) if origin == "top-left" else (float(x0), float(y1))


def _clean(points: np.ndarray, closed: bool, tol: float) -> np.ndarray:
    keep = [0]
    for i in range(1, len(points)):
        if math.hypot(*(points[i] - points[keep[-1]])) > 1e-4:
            keep.append(i)
    pts = points[keep]
    return simplify(pts, tol, False) if len(pts) > 2 else pts


def design_guides(doc: SvgDoc, job: Job, ids: set[str] | None = None) -> list[np.ndarray]:
    """Outlines of the design in G-code coordinates (flat at Z=0), using the job's origin convention."""
    ox, oy = origin_offset(doc, ids, job.origin)
    out = []
    for s in doc.shapes:
        if ids is None or s.sid in ids:
            for sp in s.subpaths:
                p = np.vstack([sp.points, sp.points[:1]]) if sp.closed else sp.points
                out.append(np.column_stack([p[:, 0] - ox, -(p[:, 1] - oy), np.zeros(len(p))]))
    return out


def generate(doc: SvgDoc, ops: list[Operation], job: Job, source_name: str = "") -> CamResult:
    res = CamResult()
    if not doc.shapes:
        res.warnings.append("Nessuna forma da lavorare")
        return res
    if job.tool_diameter <= 0:
        res.warnings.append("Diametro utensile non valido")
        return res
    ids_all: set[str] = set()
    for op in ops:
        ids_all |= (op.shapes if op.shapes is not None else {s.sid for s in doc.shapes})
    ox, oy = origin_offset(doc, ids_all or None, job.origin)
    k = 1.0 / 25.4 if job.units == "in" else 1.0

    def X(x):
        return _fmt((x - ox) * k)

    def Y(y):
        return _fmt(-(y - oy) * k)   # SVG y-down -> machine Y-up (same convention as jscut)

    def Z(z):
        return _fmt(z * k)

    def F(f):
        return _fmt(f * k)

    lines: list[str] = [
        f"; CNC Deck - conversione SVG -> G-code (approccio jscut){' - ' + source_name if source_name else ''}",
        f"; Utensile {job.tool_diameter:g} mm, avanzamento {job.cut_feed:g}, tuffo {job.plunge_feed:g} mm/min",
        "G21" if job.units == "mm" else "G20",
        "G90",
        "G94",
        f"G0 Z{Z(job.safe_z)}",
    ]
    if job.spindle:
        lines.append(f"M3 S{job.spindle_rpm:g}")
    cur_feed: float | None = None
    n_paths = 0

    def feed_word(f: float) -> str:
        nonlocal cur_feed
        if cur_feed is not None and abs(cur_feed - f) < 1e-9:
            return ""
        cur_feed = f
        return f" F{F(f)}"

    for oi, op in enumerate(ops, 1):
        paths = build_paths(doc, op, job, res.warnings)
        if not paths:
            continue
        lines.append(f"; Operazione {oi}: {op.name} ({op.kind}), profondita {op.cut_depth:g}, passata {op.pass_depth:g}")
        top = 0.0
        bottom = -abs(op.cut_depth)
        pass_depth = max(abs(op.pass_depth), 0.01)
        for pi, cp in enumerate(paths):
            pts = _clean(cp.points, cp.closed, job.tolerance)
            if len(pts) < 2:
                continue
            n_paths += 1
            start = pts[0]
            lines.append(f"; Percorso {pi}")
            lines.append(f"G0 X{X(start[0])} Y{Y(start[1])}")
            if job.approach_z < job.safe_z:
                lines.append(f"G0 Z{Z(job.approach_z)}")
            seg_len = np.hypot(*np.diff(pts, axis=0).T)
            total_len = float(seg_len.sum())
            cur_z = top
            forward = True
            at_start = True
            finished = top
            while finished > bottom + 1e-9:
                next_z = max(finished - pass_depth, bottom)
                walk = pts if forward else pts[::-1]
                if op.ramp and total_len > 1e-6:
                    # jscut ramp plunge: descend linearly along one lap, then a flat lap at the final depth
                    if at_start:
                        lines.append(f"G1 Z{Z(cur_z)}{feed_word(job.plunge_feed)}")
                    acc = 0.0
                    for j in range(1, len(walk)):
                        acc += math.hypot(*(walk[j] - walk[j - 1]))
                        z = cur_z + (next_z - cur_z) * min(acc / total_len, 1.0)
                        lines.append(f"G1 X{X(walk[j][0])} Y{Y(walk[j][1])} Z{Z(z)}{feed_word(job.cut_feed)}")
                    if next_z <= bottom + 1e-9:
                        # final depth reached: one flat lap (closed) / flat return (open) removes the slope
                        back = walk if cp.closed else walk[::-1]
                        for q in back[1:]:
                            lines.append(f"G1 X{X(q[0])} Y{Y(q[1])}{feed_word(job.cut_feed)}")
                    elif not cp.closed:
                        forward = not forward
                    at_start = False
                else:
                    lines.append(f"G1 Z{Z(next_z)}{feed_word(job.plunge_feed)}")
                    for q in walk[1:]:
                        lines.append(f"G1 X{X(q[0])} Y{Y(q[1])}{feed_word(job.cut_feed)}")
                    if not cp.closed:
                        forward = not forward      # open path: cut back and forth, no lift between passes
                cur_z = next_z
                finished = next_z
            lines.append(f"G0 Z{Z(job.safe_z)}")
    res.guides = design_guides(doc, job, ids_all or None)
    res.n_paths = n_paths
    if n_paths == 0:
        # nothing to cut: never hand a header-only program to the sender
        res.warnings.append("Nessun percorso generato")
        return res
    if job.spindle:
        lines.append("M5")
    lines.append("M2")
    res.gcode = "\n".join(lines) + "\n"
    return res
