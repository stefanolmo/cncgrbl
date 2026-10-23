import math
import time

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from cncdeck import simulator
from cncdeck.gcode import parse_program
from cncdeck.grbl import SIM_PORT, GrblConnection
from cncdeck.heightmap import HeightMap, HeightMapError, correct_program, grid_axes, serpentine
from cncdeck.probing import HeightMapProbe, ProbeParams


def plane(a=0.01, b=0.02, c=0.0):
    xs, ys = grid_axes(0, 40, 10), grid_axes(0, 30, 10)
    hm = HeightMap.blank(xs, ys)
    hm.z[:] = c + a * xs[None, :] + b * ys[:, None]
    return hm


def cmds(text):
    return parse_program(text).commands


def zs(out):
    import re
    return [float(m.group(1)) for _, c in out if (m := re.search(r"Z(-?[\d.]+)", c))]


def test_grid_axes_and_order():
    assert list(grid_axes(0, 25, 10)) == pytest.approx([0, 8.3333333, 16.6666667, 25])
    assert len(grid_axes(5, 5, 10)) == 2
    order = serpentine(3, 2)
    assert order == [(0, 0), (0, 1), (0, 2), (1, 2), (1, 1), (1, 0)]


def test_bilinear_exact_on_plane_and_clamped_outside():
    hm = plane()
    for x, y in [(0, 0), (13.7, 4.2), (40, 30), (22.2, 29.9)]:
        assert hm.z_at(x, y) == pytest.approx(0.01 * x + 0.02 * y, abs=1e-9)
    assert hm.z_at(-10, -10) == pytest.approx(0.0)
    assert hm.z_at(100, 100) == pytest.approx(0.01 * 40 + 0.02 * 30)


def test_shift_to_work_coordinates():
    hm = plane().shifted((10, 5, 1))
    assert hm.xs[0] == -10 and hm.ys[0] == -5
    assert hm.z_at(-10, -5) == pytest.approx(-1.0)


def test_json_roundtrip_and_errors():
    hm = plane()
    hm.z[1, 1] = np.nan
    back = HeightMap.from_json(hm.to_json())
    assert np.isnan(back.z[1, 1]) and back.z[0, 2] == hm.z[0, 2]
    with pytest.raises(HeightMapError):
        HeightMap.from_json('{"xs": [0, 1], "ys": [0, 1], "z": [[0]]}')
    with pytest.raises(HeightMapError):
        HeightMap.from_json("not json")


def test_correct_line_is_split_at_grid_and_follows_surface():
    out = correct_program(cmds("G90\nG0X0Y0Z5\nG1Z0F100\nG1X40"), plane(), start=(0, 0, 0))
    pts = [c for _, c in out if c.startswith("G1X")]
    assert len(pts) == 4            # grid lines at x=10, 20, 30, 40
    for c in pts:
        import re
        x, z = (float(v) for v in re.findall(r"[XZ](-?[\d.]+)", c))
        assert z == pytest.approx(0.01 * x, abs=1e-4)
    # rapid: only its end point gets corrected
    assert out[1][1].startswith("G0X0Y0Z5")


def test_correct_keeps_feed_and_modal_state_independent():
    out = correct_program(cmds("G90\nG1Z-1F200\nG1X20Y10\nX30"), plane(), start=(0, 0, 0))
    assert "F200" in out[1][1]
    assert all(c.startswith("G1") or c in ("G90",) for _, c in out)   # every move carries its own G word


def test_relative_program_is_emitted_absolute_and_mode_restored():
    out = correct_program(cmds("G91\nG1X10F100\nG1X10"), plane(), start=(0, 0, -1))
    texts = [c for _, c in out]
    assert texts[0] == "G91"
    assert texts[1].startswith("G90G1X10")
    assert texts[2].startswith("G1X20")
    assert texts[-1] == "G91"       # program was relative: machine state restored


def test_arc_becomes_segments_with_surface_height():
    out = correct_program(cmds("G90\nG1X30Y15F100\nG2X30Y15I-10J0"), plane(), start=(30, 15, 0))   # full circle
    import re
    pts = [tuple(float(v) for v in re.findall(r"[XYZ](-?[\d.]+)", c)) for _, c in out if c.startswith("G1X")]
    assert len(pts) > 20
    for x, y, z in pts:
        assert math.hypot(x - 20, y - 15) == pytest.approx(10, abs=0.03)
        assert z == pytest.approx(0.01 * x + 0.02 * y, abs=2e-4)


def test_inch_program_values_converted_back():
    hm = plane(a=0.0, b=0.0, c=0.254)           # 0.254 mm = 0.01 in
    out = correct_program(cmds("G20G90\nG1X1Y0Z-0.05F10"), hm, start=(0, 0, 0))
    last = out[-1][1]
    assert "Z-0.04" in last                     # -0.05 in + 0.01 in


@pytest.mark.parametrize("bad", ["G92X0", "G10L20P0Z0", "G55"])
def test_rejects_commands_that_move_the_zero(bad):
    with pytest.raises(HeightMapError):
        correct_program(cmds(f"G90\n{bad}\nG1X5Z0F10"), plane(), start=(0, 0, 0))


def test_unknown_position_after_machine_move_is_refused():
    with pytest.raises(HeightMapError):
        correct_program(cmds("G90\nG53G0Z0\nG1X5F10"), plane(), start=(0, 0, 0))
    # an absolute-everything move after it is still ambiguous: needs all three axes
    with pytest.raises(HeightMapError):
        correct_program(cmds("G90\nG28\nG1X5Y5F10"), plane())


def test_incomplete_map_refused():
    hm = plane()
    hm.z[0, 0] = np.nan
    with pytest.raises(HeightMapError):
        correct_program(cmds("G1X1F10"), hm, start=(0, 0, 0))


def test_non_motion_lines_untouched():
    out = correct_program(cmds("G21G90\nM3S1000\nG4P1\nM5\nM30"), plane(), start=(0, 0, 0))
    assert [c for _, c in out] == ["G21G90", "M3S1000", "G4P1", "M5", "M30"]


def test_large_program_is_fast():
    lines = ["G90", "G1Z0F500"] + [f"G1X{(i % 40)}.5Y{(i % 29)}.25" for i in range(5000)]
    t = time.time()
    out = correct_program(cmds("\n".join(lines)), plane(), start=(0, 0, 0))
    assert time.time() - t < 6 and len(out) > 5000


# ------------------------------------------------------------------ probing against the simulator
@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def pump(app, cond, timeout=40.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    return False


def surface(mx, my):
    return -18.0 + 0.01 * mx + 0.02 * my


@pytest.fixture
def conn(app, monkeypatch):
    orig = simulator.SimulatedSerial.__init__
    monkeypatch.setattr(simulator.SimulatedSerial, "__init__",
                        lambda self, speed=120.0, boot_delay=0.05: orig(self, speed=speed, boot_delay=boot_delay))
    c = GrblConnection(SIM_PORT)
    st = {"ok": False, "status": None}
    c.connectionChanged.connect(lambda ok, m: st.__setitem__("ok", ok))
    c.statusChanged.connect(lambda s: st.__setitem__("status", s))
    c.start()
    assert pump(app, lambda: st["ok"] and st["status"])
    c._ser.probe_surface = surface
    c.st = st
    yield c
    c.disconnect_port()
    c.wait(3000)


def zero_at_surface(app, conn):
    """Put the tip on the virtual surface at the origin and make it work zero (as the user would)."""
    conn.send_line("G90G0Z-18")
    assert pump(app, lambda: abs(conn.st["status"].mpos[2] + 18) < 0.01 and conn.st["status"].state == "Idle")
    conn.send_line("G10L20P0X0Y0Z0")
    assert pump(app, lambda: abs(conn.st["status"].wco[2] + 18) < 0.01)
    conn.send_line("G0Z5")          # a closed probe is refused: lift off the surface first
    assert pump(app, lambda: abs(conn.st["status"].wpos[2] - 5) < 0.01 and "P" not in conn.st["status"].pins
                and conn.st["status"].state == "Idle")


def test_probe_builds_map_of_tilted_surface_and_correction_follows_it(app, conn):
    zero_at_surface(app, conn)
    probe = HeightMapProbe(conn)
    res = {}
    probe.finished.connect(lambda m: res.__setitem__("map", m))
    probe.failed.connect(lambda e: res.__setitem__("err", e))
    err = probe.start(ProbeParams(area=(0, 0, 20, 20), step=10, safe_z=5, depth=2, feed=60))
    assert err is None
    assert pump(app, lambda: res), res
    assert "err" not in res, res
    hm = res["map"]
    assert hm.complete and hm.shape == (3, 3)
    for j, y in enumerate(hm.ys):
        for i, x in enumerate(hm.xs):
            assert hm.z[j, i] == pytest.approx(surface(x, y), abs=0.01)
    # correct a program and run it: the cut depth must stay constant relative to the tilted surface
    assert pump(app, lambda: conn.st["status"].state == "Idle" and abs(conn.st["status"].wpos[2] - 5) < 0.05)
    work = hm.shifted(conn.st["status"].wco)
    prog = cmds("G90G21\nG0X0Y0\nG1Z-0.5F300\nG1X20\nG1Y20")
    out = correct_program(prog, work, start=conn.st["status"].wpos)
    done = []
    conn.programState.connect(done.append)
    conn.start_program(out)
    assert pump(app, lambda: "done" in done)
    assert pump(app, lambda: conn.st["status"].state == "Idle")
    mp = conn.st["status"].mpos
    assert (mp[0], mp[1]) == pytest.approx((20, 20), abs=0.01)
    assert mp[2] == pytest.approx(surface(20, 20) - 0.5, abs=0.01)


def test_probe_refuses_when_pin_already_closed(app, conn):
    conn.send_line("G90G0Z-30")
    assert pump(app, lambda: "P" in conn.st["status"].pins)
    err = HeightMapProbe(conn).start(ProbeParams(area=(0, 0, 10, 10)))
    assert err and "closed" in err


def test_probe_fails_cleanly_without_contact(app, conn):
    # work zero far above the surface: nothing within `depth` => failure + alarm, no map
    conn.send_line("G90G0Z-5")
    assert pump(app, lambda: abs(conn.st["status"].mpos[2] + 5) < 0.01 and conn.st["status"].state == "Idle")
    conn.send_line("G10L20P0X0Y0Z0")
    assert pump(app, lambda: abs(conn.st["status"].wco[2] + 5) < 0.01)
    probe = HeightMapProbe(conn)
    res = {}
    probe.finished.connect(lambda m: res.__setitem__("map", m))
    probe.failed.connect(lambda e: res.__setitem__("err", e))
    assert probe.start(ProbeParams(area=(0, 0, 10, 10), step=10, depth=1, feed=120)) is None
    assert pump(app, lambda: res)
    assert "map" not in res and not probe.active
    assert "contact" in res["err"] or "Alarm" in res["err"]


def test_params_validation():
    assert ProbeParams(area=(0, 0, 10, 10)).validate() is None
    assert ProbeParams(area=(0, 0, 10, 10), depth=0).validate()
    assert ProbeParams(area=(10, 0, 0, 10)).validate()
    assert ProbeParams(area=(0, 0, 10, 10), feed=1000).validate()
