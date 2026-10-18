import time

import pytest
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

from cncdeck import simulator, theme
from cncdeck.mainwindow import MainWindow


def pump(cond, timeout=15.0):
    app = QApplication.instance()
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    return False


@pytest.fixture
def win(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(theme.STYLESHEET)
    orig = simulator.SimulatedSerial.__init__
    monkeypatch.setattr(simulator.SimulatedSerial, "__init__",
                        lambda self, speed=15.0, boot_delay=0.05: orig(self, speed=speed, boot_delay=boot_delay))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.Yes))
    monkeypatch.setattr(QInputDialog, "getDouble", staticmethod(lambda *a, **k: (12.0, True)))
    w = MainWindow()
    w.qs.clear()
    w.show()
    w.port_combo.setCurrentIndex(w.port_combo.findData("simulator"))
    w.toggle_connection()
    assert pump(lambda: w.connected and w.status.state == "Idle" and w.conn.settings)
    yield w
    w._running = False
    w.close()


def test_jog_step_and_zero(win):
    win.step_combo.setCurrentIndex(win.step_combo.findData(5.0))
    win.pad.buttons[2].pressed.emit()   # X+
    win.pad.buttons[2].released.emit()
    assert pump(lambda: abs(win.status.mpos[0] - 5.0) < 0.01 and win.status.state == "Idle")
    win.axes["X"].zeroClicked.emit("X")
    assert pump(lambda: abs(win.status.wpos[0]) < 0.01 and abs(win.status.wco[0] - 5.0) < 0.01)


def test_continuous_jog_stops_on_release(win):
    win.step_combo.setCurrentIndex(win.step_combo.findData(None))
    win.on_jog_press(0, 1, 0)
    assert pump(lambda: win.status.state == "Jog")
    win.on_jog_release()
    assert pump(lambda: win.status.state == "Idle")
    y = win.status.mpos[1]
    assert 0 < y < win._max_travel(1)
    time.sleep(0.4)
    QApplication.instance().processEvents()
    assert win.status.mpos[1] == pytest.approx(y, abs=0.01)


def test_load_run_and_finish(win, tmp_path):
    f = tmp_path / "job.nc"
    f.write_text("G21G90\nM3S500\nG1X10Y10F3000\nG1X0Y0\nM5\n")
    win.load_file(str(f))
    assert pump(lambda: win.program is not None and len(win.program.commands) == 5)
    assert win.act_run.isEnabled()
    win.run_program()
    assert pump(lambda: not win._running and win.progress.value() == 1000, 30)
    assert win.status.mpos[0] == pytest.approx(0, abs=0.01)


def test_probe_sets_z(win):
    win.probe_z()
    assert pump(lambda: win.status.state == "Idle" and not win._probe_pending and win.status.wco[2] != 0, 30)
    # touched at machine z=-18 and plate is 12 mm -> work z origin is 12 mm above... wpos at contact = 12
    assert win.status.mpos[2] > -18 + 4   # retracted
    assert win.status.wpos[2] == pytest.approx(win.status.mpos[2] - win.status.wco[2])


def test_settings_dialog_writes(win):
    win.show_settings()
    dlg = win._settings_dlg
    assert dlg.table.rowCount() > 20
    for r in range(dlg.table.rowCount()):
        if dlg.table.item(r, 0).text() == "$110":
            dlg.table.item(r, 2).setText("1500")
    dlg._apply()
    assert pump(lambda: win.conn.settings.get(110) == 1500.0)


def test_console_command(win):
    win.console.commandEntered.emit("$G")
    assert pump(lambda: "GC:" in win.console.log.toPlainText())


def test_macro_runs_commands(win, monkeypatch):
    win.qs.setValue("macro0_name", "Test")
    win.qs.setValue("macro0_cmd", "G91G21G0X3;G90")
    win._refresh_macros()
    assert win.macro_buttons[0].text() == "Test"
    win.macro_buttons[0].click()
    assert pump(lambda: abs(win.status.mpos[0] - 3.0) < 0.01 and win.status.state == "Idle")
    win.qs.setValue("macro0_name", "")
    win.qs.setValue("macro0_cmd", "")


def test_pause_button_resumes_after_m0(win, tmp_path):
    f = tmp_path / "t.nc"
    f.write_text("G21G90\nG1X5F3000\nT1M6\nG1X10F3000\n")
    win.load_file(str(f))
    assert pump(lambda: win.program is not None and len(win.program.commands) == 4)
    win.run_program()
    assert pump(lambda: win.status.state.startswith("Hold"), 20)   # M0 -> Grbl holds
    win.toggle_pause()                                             # must send '~', not '!'
    assert pump(lambda: not win._running and win.status.mpos[0] > 9.9, 30)


def test_continuous_jog_with_soft_limits_uses_g53(win):
    win.conn.send_line("$20=1", silent=True)
    assert pump(lambda: win.conn.settings.get(20) == 1.0)
    sent = []
    orig = win.conn.send_line
    win.conn.send_line = lambda text, silent=False: (sent.append(text), orig(text, silent))[1]
    win.step_combo.setCurrentIndex(win.step_combo.findData(None))
    win.on_jog_press(-1, 0, 0)
    assert sent and sent[0].startswith("$J=G53G21G90X-159.000")
    win.on_jog_release()
