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
    name: str = "Operation"
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
    # holding tabs (inside/outside only): the tool stays `tab_height` above the cut bottom along the tab
    tab_mode: str = "none"           # none | auto (evenly spaced on each part contour) | shapes (SVG shapes mark the tabs)
    tab_count: int = 4               # auto: tabs per part contour
    tab_width: float = 4.0           # mm along the path
    tab_height: float = 1.0          # mm of material left under the tab
    tab_shapes: set[str] = field(default_factory=set)   # shapes mode: SVG shapes used as tab markers (not cut)


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
    if op.tab_mode == "shapes" and op.kind in ("inside", "outside"):
        sel = [s for s in sel if s.sid not in op.tab_shapes]      # tab markers are not part of the cut
    if not sel:
        warnings.append(f"{op.name}: no shape selected")
        return []
    if op.kind == "engrave":
        return engrave(doc, ids, op.climb)
    for s in sel:
        if not any(sp.closed for sp in s.subpaths):
            warnings.append(f"{op.name}: «{s.label}» is an open path, ignored (use Engrave)")
    geometry = combine([shape_polygons(s) for s in sel], op.combine)
    if not geometry:
        warnings.append(f"{op.name}: no closed shape in the selection")
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
        warnings.append(f"{op.name}: the {job.tool_diameter:g} mm tool is too large for this shape")
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


def tabs_apply(op: Operation) -> bool:
    return op.kind in ("inside", "outside") and op.tab_mode in ("auto", "shapes")


def tab_zone(doc: SvgDoc, op: Operation, job: Job) -> list:
    """Shapes mode: the marker shapes grown by the tool radius (so a marker centred on the part edge covers
    the toolpath that runs one tool radius away from it)."""
    polys = [shape_polygons(s) for s in doc.shapes if s.sid in op.tab_shapes]
    union = combine(polys, "union")
    return offset(union, job.tool_diameter / 2) if union else []


def _nesting_depth(paths: list[CamPath]) -> list[int]:
    """How many other closed paths of the same operation enclose each path (0 = outermost, 1 = inside a ring...)."""
    clip = [_to_clip(p.points) if p.closed and len(p.points) >= 3 else None for p in paths]
    depth = []
    for i, p in enumerate(paths):
        pt = (int(round(p.points[0][0] * SCALE)), int(round(p.points[0][1] * SCALE)))
        depth.append(sum(1 for j, poly in enumerate(clip) if j != i and poly is not None
                         and pyclipper.PointInPolygon(pt, poly) == 1))
    return depth


def split_for_tabs(pts: np.ndarray, inside_fn, step: float = 0.2):
    """Cut a polyline where it enters/leaves a tab. `inside_fn(segment_index, t)` -> bool.

    Returns (nodes, flags): nodes (N,2) including the boundary points; flags[i] is True when the piece
    nodes[i] -> nodes[i+1] lies in a tab.
    """
    nodes = [pts[0]]
    flags: list[bool] = []
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        length = math.hypot(*(b - a))
        n = max(1, int(math.ceil(length / step)))
        mids = [(k + 0.5) / n for k in range(n)]
        st = [inside_fn(i, t) for t in mids]
        for k in range(1, n):
            if st[k] != st[k - 1]:
                lo, hi = mids[k - 1], mids[k]
                for _ in range(14):               # bisection: where exactly does the state flip
                    m = 0.5 * (lo + hi)
                    if inside_fn(i, m) == st[k - 1]:
                        lo = m
                    else:
                        hi = m
                t = 0.5 * (lo + hi)
                nodes.append(a + (b - a) * t)
                flags.append(st[k - 1])
        nodes.append(b)
        flags.append(st[-1])
    return np.array(nodes), flags


def tab_plan(paths: list[CamPath], pts_list: list[np.ndarray], op: Operation, job: Job, zone: list,
             warnings: list[str]):
    """Per path: (nodes, flags) split at the tab boundaries, or None when the path gets no tabs."""
    depth = _nesting_depth(paths) if op.tab_mode == "auto" else [0] * len(paths)
    plans = []
    warned_short = False
    for cp, pts, dep in zip(paths, pts_list, depth):
        if len(pts) < 3 or not cp.closed:
            plans.append(None)
            continue
        seg = np.hypot(*np.diff(pts, axis=0).T)
        cum = np.concatenate([[0.0], np.cumsum(seg)])
        total = float(cum[-1])
        if op.tab_mode == "auto":
            if dep % 2 == 1:                      # holes / waste pieces: no tabs
                plans.append(None)
                continue
            n = min(op.tab_count, int(0.5 * total / max(op.tab_width, 1e-6)))
            if n < 1:
                if not warned_short:
                    warnings.append(f"{op.name}: contour too short for tabs of {op.tab_width:g} mm")
                    warned_short = True
                plans.append(None)
                continue
            centers = [(k + 0.5) * total / n for k in range(n)]
            half = op.tab_width / 2

            def inside(i, t, cum=cum, seg=seg, centers=centers, half=half):
                s = cum[i] + t * seg[i]
                return any(abs(s - c) <= half for c in centers)
        else:
            if not zone:
                plans.append(None)
                continue

            def inside(i, t, pts=pts):
                p = pts[i] + (pts[i + 1] - pts[i]) * t
                return _inside_bounds(zone, p[0], p[1])
        nodes, flags = split_for_tabs(pts, inside)
        plans.append((nodes, flags) if any(flags) else None)
    return plans


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
        res.warnings.append("No shapes to machine")
        return res
    if job.tool_diameter <= 0:
        res.warnings.append("Invalid tool diameter")
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
        f"; CNC Deck - SVG -> G-code conversion (jscut approach){' - ' + source_name if source_name else ''}",
        f"; Tool {job.tool_diameter:g} mm, cut feed {job.cut_feed:g}, plunge {job.plunge_feed:g} mm/min",
        "G21" if job.units == "mm" else "G20",
        "G90",
        "G94",
        f"G0 Z{Z(job.safe_z)}",
    ]
    if job.spindle:
        lines.append(f"M3 S{job.spindle_rpm:g}")
    cur_feed: float | None = None
    zc: float = job.safe_z            # tracked tool Z: avoids redundant Z moves
    n_paths = 0

    def feed_word(f: float) -> str:
        nonlocal cur_feed
        if cur_feed is not None and abs(cur_feed - f) < 1e-9:
            return ""
        cur_feed = f
        return f" F{F(f)}"

    def go_z(z: float) -> None:
        nonlocal zc
        if abs(z - zc) > 1e-9:
            lines.append(f"G1 Z{Z(z)}{feed_word(job.plunge_feed)}")
            zc = z

    def emit_pass(nodes: np.ndarray, flags: list[bool], z_from: float, z_to: float, ramp: bool,
                  tab_z: float | None) -> None:
        """One lap along `nodes`. Z is constant z_to, or ramps from z_from to z_to along the lap. Where flags[i]
        is True (a tab) the tool does not go below tab_z."""
        nonlocal zc
        seg = np.hypot(*np.diff(nodes, axis=0).T)
        total = float(seg.sum())

        def eff(z: float, in_tab: bool) -> float:
            return max(z, tab_z) if (in_tab and tab_z is not None) else z

        def z_at(acc: float) -> float:
            return z_from + (z_to - z_from) * min(acc / total, 1.0) if (ramp and total > 1e-9) else z_to

        go_z(eff(z_at(0.0), flags[0]))
        acc = 0.0
        for i in range(1, len(nodes)):
            acc += float(seg[i - 1])
            zn = z_at(acc)
            za = eff(zn, flags[i - 1])
            q = nodes[i]
            if ramp and abs(za - zc) > 1e-9:
                lines.append(f"G1 X{X(q[0])} Y{Y(q[1])} Z{Z(za)}{feed_word(job.cut_feed)}")
                zc = za
            else:
                lines.append(f"G1 X{X(q[0])} Y{Y(q[1])}{feed_word(job.cut_feed)}")
            if i < len(nodes) - 1:
                go_z(eff(zn, flags[i]))      # entering / leaving a tab: vertical move at the boundary point

    for oi, op in enumerate(ops, 1):
        paths = build_paths(doc, op, job, res.warnings)
        if not paths:
            continue
        lines.append(f"; Operation {oi}: {op.name} ({op.kind}), depth {op.cut_depth:g}, pass {op.pass_depth:g}")
        top = 0.0
        bottom = -abs(op.cut_depth)
        pass_depth = max(abs(op.pass_depth), 0.01)
        pts_list = [_clean(cp.points, cp.closed, job.tolerance) for cp in paths]
        tab_z: float | None = None
        plans: list = [None] * len(paths)
        if tabs_apply(op):
            if op.tab_height <= 0 or op.tab_height >= abs(op.cut_depth):
                res.warnings.append(f"{op.name}: tab height ({op.tab_height:g} mm) is not valid for a "
                                    f"depth of {abs(op.cut_depth):g} mm: tabs ignored")
            elif op.width > job.tool_diameter + 1e-9:
                res.warnings.append(f"{op.name}: tabs require a single pass (width = tool diameter)")
            else:
                tab_z = bottom + op.tab_height
                zone = tab_zone(doc, op, job) if op.tab_mode == "shapes" else []
                if op.tab_mode == "shapes" and not zone:
                    res.warnings.append(f"{op.name}: no tab shape selected")
                plans = tab_plan(paths, pts_list, op, job, zone, res.warnings)
                if not any(plans) and op.tab_mode == "shapes" and zone:
                    res.warnings.append(f"{op.name}: the tab shapes do not touch the path")
        for pi, (cp, pts, plan) in enumerate(zip(paths, pts_list, plans)):
            if len(pts) < 2:
                continue
            n_paths += 1
            nodes, flags = plan if plan else (pts, [False] * (len(pts) - 1))
            start = nodes[0]
            lines.append(f"; Path {pi}" + (" (with tabs)" if plan else ""))
            lines.append(f"G0 X{X(start[0])} Y{Y(start[1])}")
            zc = job.safe_z
            if job.approach_z < job.safe_z:
                lines.append(f"G0 Z{Z(job.approach_z)}")
                zc = job.approach_z
            use_tabs = tab_z if plan else None
            total_len = float(np.hypot(*np.diff(nodes, axis=0).T).sum())
            forward = True
            finished = top
            while finished > bottom + 1e-9:
                next_z = max(finished - pass_depth, bottom)
                walk = nodes if forward else nodes[::-1]
                wflags = flags if forward else flags[::-1]
                if op.ramp and total_len > 1e-6:
                    # jscut ramp plunge: descend linearly along one lap, then a flat lap at the final depth
                    emit_pass(walk, wflags, finished, next_z, True, use_tabs)
                    if next_z <= bottom + 1e-9:
                        # final depth reached: one flat lap (closed) / flat return (open) removes the slope
                        back = walk if cp.closed else walk[::-1]
                        bflags = wflags if cp.closed else wflags[::-1]
                        emit_pass(back, bflags, bottom, bottom, False, use_tabs)
                    elif not cp.closed:
                        forward = not forward
                else:
                    emit_pass(walk, wflags, next_z, next_z, False, use_tabs)
                    if not cp.closed:
                        forward = not forward      # open path: cut back and forth, no lift between passes
                finished = next_z
            lines.append(f"G0 Z{Z(job.safe_z)}")
            zc = job.safe_z
    res.guides = design_guides(doc, job, ids_all or None)
    res.n_paths = n_paths
    if n_paths == 0:
        # nothing to cut: never hand a header-only program to the sender
        res.warnings.append("No toolpath generated")
        return res
    if job.spindle:
        lines.append("M5")
    lines.append("M2")
    res.gcode = "\n".join(lines) + "\n"
    return res
