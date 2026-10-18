import time

import pytest
from PySide6.QtWidgets import QApplication

from cncdeck.gcode import parse_program
from cncdeck.grbl import SIM_PORT, GrblConnection
from cncdeck import simulator


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def pump(app, cond, timeout=15.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    return False


@pytest.fixture
def conn(app, monkeypatch, request):
    # simulator time-warp: fast for streaming volume tests, slow when states must be observed
    speed = 200.0 if request.node.name == "test_stream_long_program_never_overflows" else 8.0
    orig = simulator.SimulatedSerial.__init__

    def fast(self, speed=speed, boot_delay=0.05):
        orig(self, speed=speed, boot_delay=boot_delay)

    monkeypatch.setattr(simulator.SimulatedSerial, "__init__", fast)
    c = GrblConnection(SIM_PORT)
    state = {"connected": False, "status": None, "program": [], "console": [], "acks": [], "settings": None}
    c.connectionChanged.connect(lambda ok, m: state.__setitem__("connected", ok))
    c.statusChanged.connect(lambda st: state.__setitem__("status", st))
    c.programState.connect(lambda s: state["program"].append(s))
    c.console.connect(lambda t, k: state["console"].append((k, t)))
    c.lineAcked.connect(lambda i, ok, code: state["acks"].append((i, ok, code)))
    c.settingsChanged.connect(lambda d: state.__setitem__("settings", d))
    c.start()
    assert pump(app, lambda: state["connected"] and state["status"] and state["settings"]), state
    c.state = state
    yield c
    c.disconnect_port()
    c.wait(3000)


def test_connect_and_settings(conn):
    assert conn.settings[130] == 160.0
    assert conn.state["status"].state == "Idle"


def test_stream_long_program_never_overflows(app, conn):
    lines = ["G21G90", "G0X0Y0", "M3S500"]
    for i in range(1, 400):
        lines.append(f"G1X{i * 0.1:.3f}Y{(i % 7) * 0.5:.3f}F6000")
    lines.append("M5")
    prog = parse_program("\n".join(lines))
    conn.start_program(prog.commands)
    assert pump(app, lambda: "done" in conn.state["program"], 30), conn.state["program"][-3:]
    sim = conn._ser
    assert sim.overflows == 0
    assert sim.max_rx_used <= 127
    assert len(conn.state["acks"]) == len(prog.commands)
    assert all(ok for _, ok, _ in conn.state["acks"])
    st = conn.state["status"]
    assert abs(st.mpos[0] - 39.9) < 0.01


def test_jog_and_cancel(app, conn):
    conn.send_line("$J=G91G21X100F300")
    assert pump(app, lambda: conn.state["status"].state == "Jog")
    conn.send_realtime(b"\x85")
    assert pump(app, lambda: conn.state["status"].state == "Idle")
    assert 0 < conn.state["status"].mpos[0] < 100


def test_zero_wco(app, conn):
    conn.send_line("G91G21G0X5")
    assert pump(app, lambda: abs(conn.state["status"].mpos[0] - 5) < 0.01 and conn.state["status"].state == "Idle")
    conn.send_line("G10L20P0X0")
    assert pump(app, lambda: abs(conn.state["status"].wco[0] - 5) < 0.01)
    assert abs(conn.state["status"].wpos[0]) < 0.01


def test_pause_resume_stop(app, conn):
    prog = parse_program("G21G90\nG1X150F600\nG1X0F600\n")
    conn.start_program(prog.commands)
    assert pump(app, lambda: conn.state["status"].state == "Run")
    conn.pause()
    assert pump(app, lambda: conn.state["status"].state.startswith("Hold"))
    x = conn.state["status"].mpos[0]
    time.sleep(0.3); app.processEvents()
    assert abs(conn.state["status"].mpos[0] - x) < 0.5
    conn.resume()
    assert pump(app, lambda: conn.state["status"].state == "Run")
    conn.stop()
    assert pump(app, lambda: "aborted" in conn.state["program"])
    assert pump(app, lambda: conn.state["status"].state == "Idle")
    assert conn.state["status"].mpos[0] > 0  # position preserved after hold+reset
    assert not any("sincronizzazione" in t for k, t in conn.state["console"])


def test_error_pauses_program(app, conn):
    prog = parse_program("G21G90\nG1X1\nG1X2F100\n")  # first G1 has no feed -> error:22
    conn.start_program(prog.commands)
    assert pump(app, lambda: "error" in conn.state["program"])
    assert any(not ok and code == 22 for _, ok, code in conn.state["acks"])
