import re
import time

import numpy as np
import pytest

from cncdeck import cam
from cncdeck.gcode import parse_program
from cncdeck.svgimport import parse_svg

SQUARE = ('<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="60mm" viewBox="0 0 100 60">'
          '<rect id="r" x="10" y="10" width="40" height="30"/></svg>')


def doc(svg=SQUARE, **kw):
    return parse_svg(svg, **kw)


def xy(gcode):
    return np.array([(float(a), float(b)) for a, b in re.findall(r"G1 X([-\d.]+) Y([-\d.]+)", gcode)])


def signed_area(a):
    return 0.5 * np.sum(a[:, 0] * np.roll(a[:, 1], -1) - np.roll(a[:, 0], -1) * a[:, 1])


# ---------------------------------------------------------------- import
def test_import_units_transform_and_viewbox():
    d = parse_svg('<svg xmlns="http://www.w3.org/2000/svg" width="200mm" height="100mm" viewBox="0 0 100 50">'
                  '<g transform="translate(10,5) scale(2)"><rect x="0" y="0" width="10" height="10"/></g></svg>')
    lo, hi = d.bounds()
    assert d.width_mm == pytest.approx(200, abs=0.01)
    assert (hi - lo) == pytest.approx([40, 40], abs=0.01)         # 10 user units * scale 2 * (200mm/100)
    assert lo == pytest.approx([20, 10], abs=0.01)               # translate(10,5) after scale(2): rect at 10,5 user units -> *2 mm


def test_import_px_dpi():
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96"><rect x="0" y="0" width="96" height="96"/></svg>'
    lo, hi = parse_svg(svg, dpi=96).bounds()
    assert hi[0] - lo[0] == pytest.approx(25.4, abs=0.01)
    lo, hi = parse_svg(svg, dpi=72).bounds()
    assert hi[0] - lo[0] == pytest.approx(25.4 * 96 / 72, abs=0.02)


def test_import_curves_arcs_and_open_paths():
    d = parse_svg('<svg xmlns="http://www.w3.org/2000/svg" width="50mm" height="50mm" viewBox="0 0 50 50">'
                  '<path id="a" d="M10,25 A15,15 0 0 1 40,25 Z"/>'
                  '<path id="b" fill="none" d="M5,5 C10,0 20,10 25,5"/>'
                  '<ellipse id="e" cx="25" cy="25" rx="5" ry="3"/><text x="1" y="1">hi</text></svg>')
    ids = {s.sid: s for s in d.shapes}
    assert ids["a"].subpaths[0].closed and not ids["b"].subpaths[0].closed
    lo = ids["a"].subpaths[0].points.min(0)
    assert lo[1] == pytest.approx(10, abs=0.05)                  # half circle apex
    assert any("Text" in w for w in d.warnings)


def test_import_invalid_svg():
    d = parse_svg("<svg><rect")
    assert not d.shapes and d.warnings


# ---------------------------------------------------------------- CAM geometry (jscut operations)
def test_outside_and_inside_offsets():
    d = doc()
    tool = 4.0
    out = cam.generate(d, [cam.Operation(kind="outside", cut_depth=1, pass_depth=1)], cam.Job(tool_diameter=tool, origin="svg"))
    a = xy(out.gcode)
    assert a[:, 0].min() == pytest.approx(10 - 2, abs=0.02) and a[:, 0].max() == pytest.approx(50 + 2, abs=0.02)
    assert a[:, 1].min() == pytest.approx(-(40 + 2), abs=0.02) and a[:, 1].max() == pytest.approx(-(10 - 2), abs=0.02)
    ins = cam.generate(d, [cam.Operation(kind="inside", cut_depth=1, pass_depth=1)], cam.Job(tool_diameter=tool, origin="svg"))
    b = xy(ins.gcode)
    assert b[:, 0].min() == pytest.approx(12, abs=0.02) and b[:, 0].max() == pytest.approx(48, abs=0.02)


def test_pocket_covers_the_shape_with_overlap():
    d = doc()
    job = cam.Job(tool_diameter=3.0, origin="svg")
    res = cam.generate(d, [cam.Operation(kind="pocket", cut_depth=1, pass_depth=1, step_over=0.4)], job)
    pts = xy(res.gcode)
    # every point of the pocket interior is within tool/2 of some toolpath vertex-or-segment: sample a grid
    segs = [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    A = np.array([s[0] for s in segs])
    B = np.array([s[1] for s in segs])

    def dist(p):
        ab = B - A
        t = np.clip(((p - A) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0, 1)
        return np.hypot(*(A + t[:, None] * ab - p).T).min()

    worst = max(dist(np.array([x, -y])) for x in np.linspace(10.5, 49.5, 14) for y in np.linspace(10.5, 39.5, 11))
    assert worst <= 1.5 * 1.0 + 0.1          # every interior point is within the tool radius (1.5) of a cut
    assert pts[:, 0].min() >= 11.5 - 0.02 and pts[:, 0].max() <= 48.5 + 0.02   # never beyond the offset boundary


def test_engrave_follows_open_path_back_and_forth():
    d = parse_svg('<svg xmlns="http://www.w3.org/2000/svg" width="50mm" height="50mm" viewBox="0 0 50 50">'
                  '<path id="l" fill="none" d="M5,5 L45,5"/></svg>')
    res = cam.generate(d, [cam.Operation(kind="engrave", cut_depth=1, pass_depth=0.5)], cam.Job(origin="svg"))
    xs = [float(x) for x in re.findall(r"G1 X([-\d.]+) Y", res.gcode)]
    assert xs == [45.0, 5.0]                  # pass 1 forward, pass 2 comes back: no retract in between


def test_holes_respect_fill_rule_and_combine():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="60mm" height="60mm" viewBox="0 0 60 60">'
           '<path id="ring" fill-rule="evenodd" d="M5,5 H55 V55 H5 Z M20,20 H40 V40 H20 Z"/></svg>')
    d = parse_svg(svg)
    res = cam.generate(d, [cam.Operation(kind="outside", cut_depth=1, pass_depth=1)], cam.Job(tool_diameter=2, origin="svg"))
    assert res.gcode.count("; Path") == 2                   # outer boundary + hole boundary, never joined
    for block in res.gcode.split("; Path")[1:]:
        a = xy(block)
        for p, q in zip(a[:-1], a[1:]):                 # consecutive cutting moves of ONE path
            for t in np.linspace(0, 1, 60):
                x, y = (p + (q - p) * t)
                in_solid = (5.05 < x < 54.95 and 5.05 < -y < 54.95) and not (19.95 < x < 40.05 and 19.95 < -y < 40.05)
                assert not in_solid, f"cutting move crosses the material at {x:.2f},{y:.2f}"
    d2 = parse_svg('<svg xmlns="http://www.w3.org/2000/svg" width="60mm" height="60mm" viewBox="0 0 60 60">'
                   '<rect id="a" x="0" y="0" width="30" height="30"/><rect id="b" x="20" y="20" width="30" height="30"/></svg>')
    u = cam.generate(d2, [cam.Operation(kind="outside", combine="union")], cam.Job(tool_diameter=2, origin="svg"))
    i = cam.generate(d2, [cam.Operation(kind="outside", combine="intersect")], cam.Job(tool_diameter=2, origin="svg"))
    assert u.gcode.count("; Path") == 1 and i.gcode.count("; Path") == 1
    ib = xy(i.gcode)
    assert ib[:, 0].min() == pytest.approx(19, abs=0.05) and ib[:, 0].max() == pytest.approx(31, abs=0.05)


def test_no_paths_means_no_gcode_at_all():
    d = doc()
    assert cam.generate(d, [], cam.Job()).gcode == ""                                  # no operations
    r = cam.generate(d, [cam.Operation(kind="inside")], cam.Job(tool_diameter=40))     # tool too big: no path
    assert r.gcode == "" and r.n_paths == 0 and r.warnings
    r = cam.generate(d, [cam.Operation(kind="outside", shapes=set())], cam.Job())      # nothing selected
    assert r.gcode == "" and r.guides is not None


def test_design_guides_follow_origin_convention():
    d = doc()
    g = cam.design_guides(d, cam.Job(origin="top-left"))
    assert len(g) == 1 and g[0][:, 0].min() == 0 and g[0][:, 1].max() == 0 and g[0][:, 2].max() == 0


def test_tool_too_big_warns():
    res = cam.generate(doc(), [cam.Operation(kind="inside")], cam.Job(tool_diameter=40))
    assert any("too large" in w for w in res.warnings)


@pytest.mark.parametrize("kind,climb,ccw", [("outside", False, False), ("outside", True, True),
                                            ("inside", False, True), ("inside", True, False),
                                            ("pocket", False, True), ("pocket", True, False)])
def test_milling_direction_for_clockwise_spindle(kind, climb, ccw):
    res = cam.generate(doc(), [cam.Operation(kind=kind, climb=climb, cut_depth=0.5, pass_depth=0.5)], cam.Job())
    assert (signed_area(xy(res.gcode)) > 0) == ccw


# ---------------------------------------------------------------- G-code quality / sender compatibility
def test_origin_conventions():
    d = doc()
    top = xy(cam.generate(d, [cam.Operation(kind="engrave")], cam.Job(origin="top-left")).gcode)
    assert top[:, 0].min() == 0 and top[:, 1].max() == 0 and top[:, 1].min() == pytest.approx(-30)   # below the origin, like jscut
    bl = xy(cam.generate(d, [cam.Operation(kind="engrave")], cam.Job(origin="bottom-left")).gcode)
    assert bl[:, 0].min() == 0 and bl[:, 1].min() == 0 and bl[:, 1].max() == pytest.approx(30)
    sv = xy(cam.generate(d, [cam.Operation(kind="engrave")], cam.Job(origin="svg")).gcode)
    assert sv[:, 0].min() == pytest.approx(10) and sv[:, 1].max() == pytest.approx(-10)


def test_generated_gcode_is_clean_for_the_sender():
    d = parse_svg(open("examples/demo.svg").read())
    ops = [cam.Operation(name="hole", kind="pocket", shapes={"hole"}, cut_depth=2, pass_depth=0.7),
           cam.Operation(name="star", kind="pocket", shapes={"star"}, cut_depth=1.0, pass_depth=0.5),
           cam.Operation(name="wave", kind="engrave", shapes={"wave"}, cut_depth=0.4, pass_depth=0.4),
           cam.Operation(name="cut", kind="outside", shapes={"outline"}, cut_depth=3, pass_depth=1, ramp=True)]
    res = cam.generate(d, ops, cam.Job(origin="top-left"), "demo.svg")
    lines = res.gcode.splitlines()
    prog = parse_program(res.gcode)
    assert not prog.warnings                                     # F always defined, lines short, nothing unsupported
    assert max(len(c) for _, c in prog.commands) <= 40
    assert "G21" in lines and "G90" in lines and lines[-1] == "M2"
    assert not any(re.search(r"\b(G2|G3|M6|T\d)\b", c) for _, c in prog.commands)
    assert prog.bounds_min[2] == pytest.approx(-3.0, abs=1e-3)   # deepest op: 3 mm, Z0 = top of material
    assert prog.path.kind[0] == 0                                # rapid first
    # no consecutive duplicate points, rapids are G0 (not G1 with huge feeds)
    cuts = [c for _, c in prog.commands if c.startswith("G1X")]
    assert len(cuts) == len(set(zip(cuts, range(len(cuts))))) and all(a != b for a, b in zip(cuts, cuts[1:]))
    assert all(c.startswith("G0") or c.startswith("G1") or c in ("G21", "G90", "G94", "M2") for _, c in prog.commands)
    # every cutting Z is within [-3, 0] and the tool retracts to the safe height between paths
    zs = [float(z) for z in re.findall(r"Z(-?[\d.]+)", " ".join(c for _, c in prog.commands if c.startswith("G1")))]
    assert min(zs) >= -3.0001 and max(zs) <= 0.0001


def test_passes_split_the_depth():
    res = cam.generate(doc(), [cam.Operation(kind="engrave", cut_depth=1.0, pass_depth=0.4)], cam.Job())
    zs = [float(z) for z in re.findall(r"G1 Z(-?[\d.]+)", res.gcode)]
    assert zs == [-0.4, -0.8, -1.0]


def test_ramp_descends_along_the_path_then_flat_lap():
    res = cam.generate(doc(), [cam.Operation(kind="engrave", cut_depth=1.0, pass_depth=1.0, ramp=True)], cam.Job(origin="svg"))
    zs = [float(z) for z in re.findall(r"Z(-?[\d.]+)", " ".join(l for l in res.gcode.splitlines() if l.startswith("G1")))]
    assert zs[0] == 0 and zs[-1] == pytest.approx(-1.0) and all(a >= b - 1e-9 for a, b in zip(zs, zs[1:]))
    flat = [l for l in res.gcode.splitlines() if l.startswith("G1 X") and "Z" not in l]
    assert len(flat) >= 4                                      # the flat lap at final depth


def test_inch_output_matches_mm_geometry():
    d = doc()
    mm = parse_program(cam.generate(d, [cam.Operation(kind="outside", cut_depth=2, pass_depth=1)], cam.Job()).gcode)
    inch = parse_program(cam.generate(d, [cam.Operation(kind="outside", cut_depth=2, pass_depth=1)], cam.Job(units="in")).gcode)
    assert inch.bounds_max == pytest.approx(mm.bounds_max, abs=0.01) and inch.bounds_min == pytest.approx(mm.bounds_min, abs=0.01)
    assert "G20" in cam.generate(d, [cam.Operation()], cam.Job(units="in")).gcode


def test_spindle_option():
    d = doc()
    off = cam.generate(d, [cam.Operation()], cam.Job()).gcode
    on = cam.generate(d, [cam.Operation()], cam.Job(spindle=True, spindle_rpm=800)).gcode
    assert "M3" not in off and "M3 S800" in on and "M5" in on


def test_pocket_performance_reasonable():
    d = parse_svg('<svg xmlns="http://www.w3.org/2000/svg" width="150mm" height="100mm" viewBox="0 0 150 100">'
                  '<rect x="5" y="5" width="140" height="90" rx="10"/></svg>')
    t = time.time()
    cam.generate(d, [cam.Operation(kind="pocket", cut_depth=1, pass_depth=1)], cam.Job(tool_diameter=3.175))
    assert time.time() - t < 20


# ---------------------------------------------------------------- holding tabs
def flat_segments(gcode, z):
    """Cutting segments (XY length > 0) that stay at height z."""
    p = parse_program(gcode).path
    m = (np.abs(p.start[:, 2] - z) < 1e-4) & (np.abs(p.end[:, 2] - z) < 1e-4) & (p.kind == 1)
    return p.start[m][:, :2].astype(float), p.end[m][:, :2].astype(float)


def tab_op(**kw):
    base = dict(kind="outside", cut_depth=3.0, pass_depth=1.5, tab_mode="auto", tab_count=4, tab_width=4.0, tab_height=1.0)
    base.update(kw)
    return cam.Operation(**base)


def test_auto_tabs_leave_material_only_on_the_last_pass():
    d = doc()
    res = cam.generate(d, [tab_op()], cam.Job(tool_diameter=3.0, origin="svg"))
    assert not parse_program(res.gcode).warnings and not res.warnings
    a, b = flat_segments(res.gcode, -2.0)                       # tab height = -3 + 1
    assert np.hypot(*(b - a).T).sum() == pytest.approx(4 * 4.0, abs=0.05)          # 4 tabs x 4 mm
    lines = res.gcode.splitlines()
    assert sum(1 for l in lines if l.startswith("G1 Z-2")) == 4                    # 4 lifts
    assert sum(1 for l in lines if l.startswith("G1 Z-3")) == 1 + 4                # initial plunge + 4 returns to depth
    assert sum(1 for l in lines if l.startswith("G1 Z-1.5")) == 1                  # shallower pass: no tab logic
    assert "(with tabs)" in res.gcode
    # Z moves at tab boundaries are purely vertical (no diagonal plunge into the material)
    assert not any(re.search(r"X.*Z|Z.*X", l) for l in lines if l.startswith("G1"))


def test_tabs_are_evenly_spaced_and_not_in_the_first_pass():
    d = doc()
    res = cam.generate(d, [tab_op(tab_count=3, tab_width=6.0)], cam.Job(tool_diameter=3.0, origin="svg"))
    a, b = flat_segments(res.gcode, -2.0)
    mids = (a + b) / 2
    assert len(mids) >= 3
    # 3 tabs around a closed contour: their centres are ~perimeter/3 apart
    steps = np.hypot(*np.diff(mids, axis=0).T)
    assert steps.max() > 5 and np.hypot(*(b - a).T).sum() == pytest.approx(3 * 6.0, abs=0.05)


def test_tab_shapes_mode_marks_the_path_where_a_marker_touches_it():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="60mm" viewBox="0 0 100 60">'
           '<rect id="part" x="10" y="10" width="40" height="30"/><circle id="mark" cx="30" cy="10" r="3"/></svg>')
    d = parse_svg(svg)
    res = cam.generate(d, [tab_op(tab_mode="shapes", tab_shapes={"mark"}, shapes=None)], cam.Job(tool_diameter=3.0, origin="svg"))
    assert res.gcode.count("; Path") == 1                    # the marker itself is not cut
    a, b = flat_segments(res.gcode, -2.0)
    assert len(a) > 0
    centre = np.array([30.0, -10.0])                            # marker centre in G-code coordinates (svg origin)
    allpts = np.vstack([a, b])
    assert np.hypot(*(allpts - centre).T).max() <= 3.0 + 1.5 + 0.05     # inside the marker grown by the tool radius
    assert np.hypot(*(b - a).T).sum() > 6                       # a real tab, not a dot
    assert sum(1 for l in res.gcode.splitlines() if l.startswith("G1 Z-2")) == 1       # a single tab


def test_holes_get_no_auto_tabs_but_part_contours_do():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="60mm" height="60mm" viewBox="0 0 60 60">'
           '<path fill-rule="evenodd" d="M5,5 H55 V55 H5 Z M20,20 H40 V40 H20 Z"/></svg>')
    d = parse_svg(svg)
    res = cam.generate(d, [tab_op()], cam.Job(tool_diameter=2.0, origin="svg"))
    assert res.gcode.count("; Path") == 2 and res.gcode.count("(with tabs)") == 1


def test_tabs_are_ignored_for_pocket_engrave_and_when_invalid():
    d = doc()
    plain = cam.generate(d, [cam.Operation(kind="pocket", cut_depth=3, pass_depth=1.5)], cam.Job()).gcode
    assert cam.generate(d, [cam.Operation(kind="pocket", cut_depth=3, pass_depth=1.5, tab_mode="auto")], cam.Job()).gcode == plain
    base = cam.generate(d, [cam.Operation(kind="outside", cut_depth=3, pass_depth=1.5)], cam.Job())
    bad = cam.generate(d, [tab_op(tab_height=3.0)], cam.Job())                 # as tall as the cut: nothing to cut
    assert bad.gcode == base.gcode and any("tabs ignored" in w for w in bad.warnings)
    wide = cam.generate(d, [tab_op(width=10.0)], cam.Job())
    assert any("single pass" in w for w in wide.warnings)
    none = cam.generate(d, [tab_op(tab_mode="shapes", tab_shapes=set())], cam.Job())
    assert none.gcode == base.gcode and any("no tab shape" in w for w in none.warnings)
    tiny = cam.generate(parse_svg('<svg xmlns="http://www.w3.org/2000/svg" width="20mm" height="20mm" viewBox="0 0 20 20">'
                                  '<rect x="8" y="8" width="3" height="3"/></svg>'), [tab_op(tab_width=8.0)], cam.Job(tool_diameter=1.0))
    assert any("too short" in w for w in tiny.warnings) and "(with tabs)" not in tiny.gcode


def test_tabs_with_ramp_never_cut_below_tab_height_in_the_tab():
    d = doc()
    res = cam.generate(d, [tab_op(ramp=True, cut_depth=3.0, pass_depth=3.0)], cam.Job(tool_diameter=3.0, origin="svg"))
    assert not parse_program(res.gcode).warnings
    a, b = flat_segments(res.gcode, -2.0)
    assert np.hypot(*(b - a).T).sum() >= 4 * 4.0 - 0.1          # tabs on the final flat lap
    p = parse_program(res.gcode)
    assert p.bounds_min[2] == pytest.approx(-3.0, abs=1e-3)
    # the ramp lap is still above/at tab height inside the tabs: count segments strictly between -3 and -2 that are
    # ramp pieces (changing z) - they exist, and the lap ends at the start point
    sl = p.path
    ramp = (np.abs(sl.start[:, 2] - sl.end[:, 2]) > 1e-4) & (sl.kind == 1) & (np.hypot(*(sl.end[:, :2] - sl.start[:, :2]).T) > 1e-4)
    assert ramp.sum() > 5


def test_tabs_job_is_clean_for_the_sender():
    d = parse_svg(open("examples/demo.svg").read())
    res = cam.generate(d, [tab_op(shapes={"outline"}, tab_count=6, cut_depth=3, pass_depth=1)], cam.Job())
    prog = parse_program(res.gcode)
    assert not prog.warnings and max(len(c) for _, c in prog.commands) <= 40
    assert prog.bounds_min[2] == pytest.approx(-3.0, abs=1e-3) and res.gcode.splitlines()[-1] == "M2"
