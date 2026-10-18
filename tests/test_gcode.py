import math

import pytest

from cncdeck.gcode import clean_line, parse_program, preamble_for


def test_clean_line():
    assert clean_line("N10 G1 X10.5 Y-2 (move) ; hi") == "G1X10.5Y-2"
    assert clean_line("%") == ""
    assert clean_line("(only comment)") == ""


def test_bounds_and_modal():
    p = parse_program("G21G90\nG0X5Y5Z2\nG1Z-1F100\nG91\nG1X10\nG90\nG1X0Y0\n")
    assert p.bounds_min == (0.0, 0.0, -1.0)
    assert p.bounds_max == (15.0, 5.0, 2.0)
    assert not p.warnings


def test_inch_units():
    p = parse_program("G20\nG1X1F10\n")
    assert math.isclose(p.bounds_max[0], 25.4, rel_tol=1e-6)


def test_arcs_ij_and_r_agree():
    a = parse_program("G90\nG0X10Y0\nG3X0Y10I-10J0F100\n")
    b = parse_program("G90\nG0X10Y0\nG3X0Y10R10F100\n")
    assert math.isclose(a.feed_length, b.feed_length, rel_tol=1e-3)
    assert math.isclose(a.feed_length, 2 * math.pi * 10 / 4, rel_tol=5e-3)
    assert abs(a.bounds_max[0] - 10) < 1e-3 and abs(a.bounds_max[1] - 10) < 1e-3


def test_full_circle_and_ccw():
    p = parse_program("G0X10Y0\nG3X10Y0I-10J0F100\n")
    assert math.isclose(p.feed_length, 2 * math.pi * 10, rel_tol=5e-3)
    assert math.isclose(p.bounds_min[0], -10, abs_tol=0.05)


def test_helix_z():
    p = parse_program("G0X10Y0Z0\nG3X10Y0Z-2I-10J0F100\n")
    assert math.isclose(p.bounds_min[2], -2, abs_tol=1e-6)


def test_time_estimate():
    p = parse_program("G1X100F600\n")
    assert math.isclose(p.total_time, 10.0, rel_tol=1e-6)


def test_warnings():
    p = parse_program("G1X10\nG41X1\n" + "G1X1" + "Y1" * 50 + "F100\n")
    text = " ".join(p.warnings)
    assert "senza avanzamento" in text and "G41" in text and "troppo lunga" in text


def test_commands_mapping():
    p = parse_program("; c\nG0X1\n\n(c)\nG0X2\n")
    assert [i for i, _ in p.commands] == [1, 4]


def test_preamble_resume():
    p = parse_program("G21G90\nM3S800\nG0Z5\nG0X1Y2\nG1Z-1F200\nG1X5\nG1X9\n")
    pre = preamble_for(p, 6)
    assert pre[0] == "G21" and "M3S800" in pre
    assert pre[-1].startswith("G1Z-1")
    assert any(c.startswith("G0X5") for c in pre)


def test_tool_change_becomes_pause():
    p = parse_program("G0X1\nT2M6\nG0X2\n(M6 in comment)\nM06\n")
    assert [c for _, c in p.commands] == ["G0X1", "M0", "G0X2", "M0"]
    assert any("M6" in w for w in p.warnings)


def test_cam_g1_positioning_above_work_is_drawn_as_air_move():
    from cncdeck.gcode import RAPID, FEED
    p = parse_program(open("examples/ellipse_cam.gcode").read())
    assert p.bounds_min[1] == pytest.approx(-39.05, abs=0.01) and p.bounds_max[1] == pytest.approx(-5.35, abs=0.01)
    assert p.bounds_min[0] == pytest.approx(22.35, abs=0.01) and p.bounds_max[0] == pytest.approx(56.05, abs=0.01)
    kinds = list(p.path.kind)
    assert kinds[0] == RAPID and kinds[1] == RAPID             # G1 Z2.54, G1 X.. Y.. above the work
    assert FEED in kinds and not p.warnings
    assert p.total_time > 5
