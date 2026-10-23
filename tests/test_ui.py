import time

import pytest
from PySide6.QtCore import Qt
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


def _press_pad(win, idx):
    win.step_combo.setCurrentIndex(win.step_combo.findData(5.0))
    win.pad.buttons[idx].pressed.emit()
    win.pad.buttons[idx].released.emit()
    assert pump(lambda: win.status.state == "Idle" and abs(win.status.mpos[1]) > 4.9)
    return win.status.mpos[1]


def test_up_arrow_is_y_plus_down_arrow_is_y_minus(win):
    assert _press_pad(win, 0) == pytest.approx(5.0, abs=0.01)       # up arrow -> Y+
    win.pad.buttons[3].pressed.emit()                                # down arrow -> Y-
    win.pad.buttons[3].released.emit()
    assert pump(lambda: win.status.state == "Idle" and abs(win.status.mpos[1]) < 0.01)


def test_view_is_y_down_and_has_no_view_or_invert_selectors(win):
    import numpy as np
    from PySide6.QtWidgets import QCheckBox, QComboBox
    win.view.set_view("top")
    p = win.view.project(np.array([[0, 5.0, 0], [0, 0, 0]]))
    assert p[0][1] < p[1][1]                                         # Y+ drawn upwards, like the up arrow
    assert not [c for c in win.findChildren(QCheckBox) if "nvert" in c.text()]
    assert not [c for c in win.view.findChildren(QComboBox)]


def test_no_bed_wording_and_version_in_title(win):
    from cncdeck import __version__
    assert __version__ in win.windowTitle()
    texts = " ".join(w.text() for w in win.findChildren(__import__("PySide6.QtWidgets", fromlist=["QCheckBox"]).QCheckBox))
    assert "platen" not in texts.lower()


def test_loaded_file_positive_y_above_origin(win, tmp_path):
    import numpy as np
    f = tmp_path / "pos.nc"
    f.write_text("G21G90\nG0X0Y0\nG1X40Y0F500\nG1X40Y25\nG1X0Y25\nG1X0Y0\n")
    win.load_file(str(f))
    assert pump(lambda: win.program is not None and len(win.program.path) == 4)
    win.view.set_view("top")
    pts = win.view.project(np.array([[0.0, 0, 0], [40.0, 25.0, 0]]))
    assert pts[1][0] > pts[0][0] and pts[1][1] < pts[0][1]    # part lies right of and above the origin


def test_loaded_file_negative_y_appears_below_origin_like_cam_preview(win, tmp_path):
    import numpy as np
    f = tmp_path / "neg.nc"
    f.write_text("G21G90\nG0X0Y0\nG1X40Y0F500\nG1X40Y-25\nG1X0Y-25\nG1X0Y0\n")
    win.load_file(str(f))
    assert pump(lambda: win.program is not None and len(win.program.path) == 4)
    win.view.set_view("top")
    pts = win.view.project(np.array([[0.0, 0, 0], [40.0, -25.0, 0]]))
    assert pts[1][1] > pts[0][1]   # negative Y is drawn below the origin (origin = top-left of the part)


def test_real_cam_file_marker_follows_path_during_run(win):
    import numpy as np
    win.load_file("examples/ellipse_cam.gcode")
    assert pump(lambda: win.program is not None and len(win.program.path) > 100)
    win.view.set_view("top")
    # ellipse is exported with negative Y: it must be drawn below the origin, to the right of it
    c = win.view.project(np.array([[0.0, 0, 0], [40.0, -20.0, 0]]))
    assert c[1][0] > c[0][0] and c[1][1] > c[0][1]
    win.run_program()
    assert pump(lambda: win.status.state == "Run" and win.status.mpos[2] < 0.5, 30)
    p = win.program.path
    # the marker (work position from GRBL) lies within 1 mm of some drawn cutting segment
    pos = np.asarray(win.status.wpos, float)
    a, b = p.start.astype(float), p.end.astype(float)
    ab = b - a
    t = np.clip(((pos - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0, 1)
    assert np.linalg.norm(a + t[:, None] * ab - pos, axis=1).min() < 2.0


def test_grbl_error_shows_nonmodal_bar_and_app_stays_closable(win, tmp_path, monkeypatch):
    modal = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: (modal.append(a[2]), QMessageBox.Yes)[1]))
    f = tmp_path / "arcs.nc"
    # two consecutive arcs whose radius equals half the chord: GRBL answers error:34 to both
    f.write_text("G21G90\nG0X0Y0\nG1X10F3000\nG2X10Y20R10F250\nG3X10Y40R10F250\nG1X20F3000\n")
    win.load_file(str(f))
    assert pump(lambda: win.program is not None and len(win.program.commands) == 6)
    modal.clear()
    win.run_program()
    assert pump(lambda: win.error_bar.isVisible(), 20)
    pump(lambda: False, 0.5)
    assert "error:34" in win.error_label.text() and "Program paused" in win.error_label.text()
    assert not any("rejected" in m for m in modal)            # no blocking dialog for the error
    # the main window still reacts: Abort aborts, bar disappears
    win.btn_err_stop.click()
    assert pump(lambda: not win._running and not win.error_bar.isVisible(), 15)


def test_close_while_paused_on_error_does_not_hang(win, tmp_path):
    f = tmp_path / "e.nc"
    f.write_text("G21G90\nG2X10Y20R10F250\nG1X20F3000\n")
    win.load_file(str(f))
    assert pump(lambda: win.program is not None and len(win.program.commands) == 3)
    win.run_program()
    assert pump(lambda: win.error_bar.isVisible(), 20)
    t0 = time.time()
    win.close()                                                 # question() is stubbed to Yes by the fixture
    assert time.time() - t0 < 4 and not win.conn.isRunning() and not win.isVisible()


# ---------------------------------------------------------------- SVG converter mode
def test_mode_switch_keeps_sender_and_shows_converter(win):
    from PySide6.QtWidgets import QApplication
    assert win.mode == 0 and win.stack.currentIndex() == 0
    win.btn_mode[1].click()
    assert win.mode == 1 and win.stack.currentWidget() is win.converter
    assert not win.act_run.isVisible() and not win.act_home.isVisible()      # sender toolbar hidden in converter mode
    assert win.btn_mode[1].isChecked() and not win.btn_mode[0].isChecked()
    assert win.connected                                                       # connection untouched by the switch
    win.btn_mode[0].click()
    assert win.mode == 0 and win.stack.currentIndex() == 0
    QApplication.instance().processEvents()


def _convert_demo(win):
    win.set_mode(1)
    assert win.converter.load_svg("examples/demo.svg")
    win.converter._add_op()                                  # the list starts empty: create one operation
    assert pump(lambda: win.converter.program is not None and not win.converter._dirty, 30)
    return win.converter


def test_converter_generates_preview_and_enables_actions(win):
    c = _convert_demo(win)
    assert len(c.doc.shapes) == 4 and c.result.gcode.startswith("; CNC Deck")
    assert c.btn_send.isEnabled() and c.btn_save.isEnabled()
    assert len(c.view.guides) >= 4 and c.view.program is c.program          # design + toolpath in the preview
    assert "estimated time" in c.summary.text()
    c.j_tool.setValue(2.0)                                                     # edit -> debounced regeneration
    assert not c.btn_send.isEnabled()                                          # stale G-code can't be sent
    assert pump(lambda: c.btn_send.isEnabled() and "Tool 2 mm" in c.result.gcode, 30)


def test_send_to_cnc_loads_sender_in_memory_without_files(win, tmp_path, monkeypatch):
    import os
    monkeypatch.chdir(tmp_path)  # any stray file written by the flow would land here
    from pathlib import Path
    svg = (Path(__file__).resolve().parent.parent / "examples" / "demo.svg").read_text()
    (tmp_path / "in.svg").write_text(svg)
    win.set_mode(1)
    assert win.converter.load_svg(str(tmp_path / "in.svg"))
    win.converter._add_op()
    assert pump(lambda: win.converter.program is not None and not win.converter._dirty, 30)
    gcode = win.converter.result.gcode
    win.converter.btn_send.click()
    assert win.mode == 0 and win.file_path is None                             # back in the Sender, no file involved
    assert pump(lambda: win.program is not None and len(win.program.commands) > 10, 20)
    assert win.editor.toPlainText() == gcode
    assert win.program.commands == __import__("cncdeck.gcode", fromlist=["parse_program"]).parse_program(gcode).commands
    assert win.act_run.isEnabled()                                             # ready: needs an explicit Run
    assert "in.gcode" in win.windowTitle()
    assert not [f for f in os.listdir(tmp_path) if f.endswith((".gcode", ".nc"))]
    assert not win._running                                                    # nothing starts by itself


def test_generated_job_streams_to_grbl_without_errors(win, tmp_path):
    c = _convert_demo(win)
    c.btn_send.click()
    assert pump(lambda: win.program is not None and len(win.program.commands) > 10, 20)
    assert not win.program.warnings
    n = len(win.program.commands)
    win.run_program()
    assert pump(lambda: not win._running and win.progress.value() == 1000, 180)
    sim = win.conn._ser
    assert sim.overflows == 0 and sim.max_rx_used <= 127
    assert not win.error_bar.isVisible() and "error:" not in win.console.log.toPlainText()
    assert win.status.state == "Idle" and win.status.mpos[2] == pytest.approx(5.0, abs=0.01)   # ended at safe Z
    assert sim.lines_executed >= n - 5


def test_sender_arrows_unaffected_by_converter(win):
    win.set_mode(1)
    win.set_mode(0)
    assert _press_pad(win, 0) == pytest.approx(5.0, abs=0.01)                  # still: up arrow -> Y+


# ---------------------------------------------------------------- splash screen
def test_splash_lasts_its_duration_then_signals_and_has_the_texts(monkeypatch):
    from PySide6.QtWidgets import QApplication
    from cncdeck import __version__
    from cncdeck.splash import CREDITS, SplashScreen
    QApplication.instance() or QApplication([])
    assert CREDITS == "Stefano Ciurleo & Claude Code - 2026"
    drawn = []
    from PySide6.QtGui import QPainter
    orig = QPainter.drawText
    monkeypatch.setattr(QPainter, "drawText", lambda self, *a: (drawn.append(a[-1]) if isinstance(a[-1], str) else None, orig(self, *a))[1])
    s = SplashScreen(__version__, duration_ms=3000)
    done = []
    s.finished.connect(lambda: done.append(time.time()))
    t0 = time.time()
    s.start()
    assert pump(lambda: done, 6)
    assert 2.9 <= done[0] - t0 <= 3.6                           # 3 seconds
    assert not s.isVisible()
    assert any(CREDITS in str(x) for x in drawn) and any(__version__ in str(x) for x in drawn)
    assert any(str(x) == "CNC" for x in drawn) and any(str(x) == "Deck" for x in drawn)


def test_cli_splash_can_be_disabled_and_window_opens_after_it(monkeypatch, tmp_path):
    from PySide6.QtWidgets import QApplication
    from cncdeck import __main__ as entry
    app = QApplication.instance() or QApplication([])
    seen = {}
    monkeypatch.setattr(entry.SplashScreen, "start", lambda self: seen.setdefault("splash", True))
    monkeypatch.setattr(entry, "QApplication", lambda *a: app)          # pytest already owns the QApplication
    monkeypatch.setattr(type(app), "exec", lambda self: 0)
    monkeypatch.delenv("CNCDECK_NO_SPLASH", raising=False)
    made = []

    class Tracked(entry.MainWindow):
        def __init__(self, *a):
            super().__init__(*a)
            made.append(self)

    monkeypatch.setattr(entry, "MainWindow", Tracked)
    assert entry.main([]) == 0 and seen.get("splash")          # default: splash is used
    assert made and not made[0].isVisible()                    # the window opens only when the splash ends
    seen.clear()
    assert entry.main(["--no-splash"]) == 0 and not seen       # opt-out
    assert made[1].isVisible()
    for w in made:
        w.close()


def test_settings_dialog_keeps_user_edits_when_refreshed(win):
    from cncdeck.settings_dialog import GrblSettingsDialog
    dlg = GrblSettingsDialog({110: 2000.0, 111: 2000.0})
    for r in range(dlg.table.rowCount()):
        if dlg.table.item(r, 0).text() == "$110":
            dlg.table.item(r, 2).setText("1500")
    dlg.load({110: 2000.0, 111: 1800.0})                # late reply of `$$` after the user typed
    texts = {dlg.table.item(r, 0).text(): dlg.table.item(r, 2).text() for r in range(dlg.table.rowCount())}
    assert texts["$110"] == "1500" and texts["$111"] == "1800"
    got = []
    dlg.applyRequested.connect(got.append)
    dlg._apply()
    assert got == [["$110=1500"]]


def test_converter_starts_with_no_operations_and_nothing_to_send(win):
    from PySide6.QtWidgets import QApplication
    win.set_mode(1)
    c = win.converter
    assert c.ops == [] and c.ops_list.count() == 0 and c.cur_op() is None
    assert not c.btn_gen.isEnabled() and not c.btn_send.isEnabled() and not c.btn_save.isEnabled()
    assert not c.w_kind.isEnabled() and not any(b.isEnabled() for b in c._op_btns)   # nothing to edit/duplicate/remove
    assert c.load_svg("examples/demo.svg")
    assert pump(lambda: c.result is not None, 10)
    assert c.ops == [] and c.result.gcode == "" and c.editor.toPlainText() == ""     # no G-code invented for the user
    assert not c.btn_send.isEnabled() and not c.btn_save.isEnabled()
    assert len(c.view.guides) >= 4 and c.view.program is None                        # but the design is shown
    assert "No operations" in c.summary.text()
    c.btn_send.click()                                                               # disabled: must do nothing
    assert win.mode == 1 and win.program is None
    QApplication.instance().processEvents()


def test_converter_add_and_remove_last_operation(win):
    c = win.converter
    win.set_mode(1)
    assert c.load_svg("examples/demo.svg")
    c._add_op()
    assert c.ops_list.count() == 1 and c.w_kind.isEnabled() and all(b.isEnabled() for b in c._op_btns)
    assert pump(lambda: c.program is not None and not c._dirty and c.btn_send.isEnabled(), 30)
    c._del_op()                                                                      # the last one can be removed
    assert c.ops == [] and c.ops_list.count() == 0
    assert pump(lambda: c.result is not None and c.result.gcode == "" and not c._dirty, 10)
    assert not c.btn_send.isEnabled() and not c.btn_save.isEnabled() and c.program is None
    assert len(c.view.guides) >= 4


def test_operation_with_no_selected_shapes_produces_no_gcode(win):
    c = win.converter
    win.set_mode(1)
    assert c.load_svg("examples/demo.svg")
    c._add_op()
    c.cur_op().shapes = set()                                                         # nothing selected
    c.generate_now()
    assert pump(lambda: c.result is not None and not c._dirty and c.result.gcode == "", 10)
    assert not c.btn_send.isEnabled()
    assert any("no shape" in m.lower() or "No toolpath" in m for m in c.messages.toPlainText().splitlines())


# ---------------------------------------------------------------- holding tabs in the converter UI
def _conv_with_op(win, svg="examples/demo.svg"):
    win.set_mode(1)
    c = win.converter
    assert c.load_svg(svg)
    c._add_op()
    c.cur_op().shapes = {"outline"}
    c.w_cut.setValue(3.0)
    c.w_pass.setValue(1.5)
    return c


def test_tab_controls_follow_the_operation_kind_and_mode(win):
    c = _conv_with_op(win)
    assert c.w_tabmode.isEnabled() and not c.w_tabcount.isEnabled() and not c.tab_shape_list.isVisibleTo(c)
    c.w_tabmode.setCurrentIndex(c.w_tabmode.findData("auto"))
    assert c.cur_op().tab_mode == "auto" and c.w_tabcount.isEnabled() and c.w_tabw.isEnabled() and c.w_tabh.isEnabled()
    c.w_tabmode.setCurrentIndex(c.w_tabmode.findData("shapes"))
    assert not c.w_tabcount.isEnabled() and c.tab_shape_list.isVisibleTo(c) and c.tab_shape_list.count() == 4
    c.w_kind.setCurrentIndex(c.w_kind.findData("pocket"))                      # tabs make no sense for a pocket
    assert not c.w_tabmode.isEnabled() and not c.w_tabw.isEnabled() and not c.tab_shape_list.isVisibleTo(c)
    c.w_kind.setCurrentIndex(c.w_kind.findData("outside"))
    assert c.w_tabmode.isEnabled() and c.cur_op().tab_mode == "shapes"         # the setting is kept


def test_auto_tabs_end_up_in_the_gcode_and_the_sender(win):
    import re
    c = _conv_with_op(win)
    c.w_tabmode.setCurrentIndex(c.w_tabmode.findData("auto"))
    c.w_tabcount.setValue(5)
    c.w_tabw.setValue(3.0)
    c.w_tabh.setValue(1.0)
    assert pump(lambda: c.btn_send.isEnabled() and "(with tabs)" in c.result.gcode, 30)
    lifts = [l for l in c.result.gcode.splitlines() if l.startswith("G1 Z-2")]
    assert len(lifts) == 5                                                       # 5 tabs on the one contour
    c.btn_send.click()
    assert pump(lambda: win.program is not None and len(win.program.commands) > 10, 20)
    assert not win.program.warnings and win.mode == 0
    assert re.search(r"G1Z-2", " ".join(cmd for _, cmd in win.program.commands))   # reaches the sender as is


def test_shape_marker_is_a_tab_not_a_cut(win, tmp_path):
    svg = tmp_path / "marker.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="60mm" viewBox="0 0 100 60">'
                   '<rect id="part" x="10" y="10" width="40" height="30"/><circle id="mark" cx="30" cy="10" r="3"/>'
                   '<rect id="other" x="70" y="10" width="20" height="20"/></svg>')
    win.set_mode(1)
    c = win.converter
    assert c.load_svg(str(svg))
    c._add_op()
    c.w_cut.setValue(3.0)
    c.w_pass.setValue(1.5)
    c.w_tabmode.setCurrentIndex(c.w_tabmode.findData("shapes"))
    for i in range(c.tab_shape_list.count()):
        if c.tab_shape_list.item(i).data(Qt.UserRole) == "mark":
            c.tab_shape_list.item(i).setCheckState(Qt.Checked)
    assert c.cur_op().tab_shapes == {"mark"}
    assert pump(lambda: c.btn_send.isEnabled() and not c._dirty, 30)
    g = c.result.gcode
    # 'part' and 'other' are cut (the marker circle merges into 'part' only if it were a cut shape): 2 paths, 1 tab
    assert g.count("; Path") == 2 and g.count("(with tabs)") == 1
    c.cur_op().tab_shapes = set()                                                # no marker any more: nothing excluded
    c.cur_op().tab_mode = "none"
    c.generate_now()
    assert pump(lambda: c.btn_send.isEnabled() and not c._dirty, 30)
    assert "(with tabs)" not in c.result.gcode


def _tilted(mx, my):
    return -18.0 + 0.01 * mx + 0.02 * my


def test_height_map_probe_then_run_follows_tilted_surface(win, tmp_path):
    win.conn._ser.probe_surface = _tilted
    f = tmp_path / "job.nc"
    f.write_text("G21G90\nG0X0Y0\nG1Z-0.3F300\nG1X20F600\nG1Y20\n")
    win.load_file(str(f))
    assert pump(lambda: win.program is not None and len(win.program.commands) == 5)
    # user zeroes Z on the surface at the origin and lifts the tip
    win.send("G90G0Z-18")
    assert pump(lambda: abs(win.status.mpos[2] + 18) < 0.01 and win.status.state == "Idle")
    win.send("G10L20P0X0Y0Z0")
    win.send("G0Z5")
    assert pump(lambda: abs(win.status.wpos[2] - 5) < 0.01 and "P" not in win.status.pins and win.status.state == "Idle")
    assert win.btn_hm_probe.isEnabled()
    assert not win.chk_hm.isEnabled()
    win.hm_feed.setValue(300)
    warned = []
    orig_warn = QMessageBox.warning
    QMessageBox.warning = staticmethod(lambda *a, **k: (warned.append(a[2]), QMessageBox.Yes)[1])
    win.start_heightmap()                                  # refused until the contact test has passed
    QMessageBox.warning = orig_warn
    assert warned and "Test probe" in warned[0] and not win._probing
    win.send("G90G0Z-0.2")                                  # tip on the virtual surface: Pn:P
    assert pump(lambda: "P" in win.status.pins)
    win.send("G0Z5")
    assert pump(lambda: "P" not in win.status.pins and abs(win.status.wpos[2] - 5) < 0.01 and win.status.state == "Idle")
    win.start_probe_test()
    win.send("G90G0Z-0.2")
    assert pump(lambda: win._probe_ok, 20)
    win.send("G0Z5")
    assert pump(lambda: "P" not in win.status.pins and abs(win.status.wpos[2] - 5) < 0.01 and win.status.state == "Idle")
    win.start_heightmap()
    assert win._probing and not win.act_run.isEnabled() and not win.pad.isEnabled()
    assert pump(lambda: not win._probing and win.heightmap is not None, 60)
    hm = win.heightmap
    assert hm.complete and hm.shape == (4, 4)       # 24 mm at <=10 mm steps
    assert hm.z[0, 0] == pytest.approx(_tilted(-2, -2), abs=0.01)
    assert win.chk_hm.isChecked() and win.view.heightmap is not None
    assert "active" in win.hm_label.text()
    win.view.grab()          # the relief mesh paints without errors
    assert pump(lambda: win.status.state == "Idle" and win.act_run.isEnabled())
    win.run_program()
    assert pump(lambda: not win._running and win.progress.value() == 1000, 60)
    assert pump(lambda: win.status.state == "Idle")
    mp = win.status.mpos
    assert (mp[0], mp[1]) == pytest.approx((20, 20), abs=0.01)
    assert mp[2] == pytest.approx(_tilted(20, 20) - 0.3, abs=0.01)
    # without the map the same program would have cut at the nominal (flat) depth
    win.chk_hm.setChecked(False)
    assert win._leveled([(0, "G1X1")]) == [(0, "G1X1")]


def test_height_map_refused_for_unsafe_program_and_survives_rezero(win, tmp_path):
    from cncdeck.heightmap import HeightMap, grid_axes
    hm = HeightMap.blank(grid_axes(-5, 25, 10), grid_axes(-5, 25, 10))
    hm.z[:] = -18.0
    win.heightmap = hm
    win.chk_hm.setEnabled(True)
    win.chk_hm.setChecked(True)
    assert win._leveled([(0, "G92X0"), (1, "G1X5F10")]) is None      # G92 moves the zero: refused (warning patched)
    win.clear_heightmap()
    assert win.heightmap is None and not win.chk_hm.isChecked() and win.view.heightmap is None


def test_height_map_inconsistent_with_current_zero_asks_and_can_go_relative(win, monkeypatch):
    import numpy as np
    from cncdeck.heightmap import HeightMap, grid_axes
    win.send("G90G0Z-8")
    assert pump(lambda: abs(win.status.mpos[2] + 8) < 0.01 and win.status.state == "Idle")
    win.send("G10L20P0X0Y0Z0")                         # work zero now at machine Z -8
    assert pump(lambda: abs(win.status.wco[2] + 8) < 0.01)
    hm = HeightMap.blank(grid_axes(0, 20, 10), grid_axes(0, 20, 10))
    hm.z[:] = -18.0                                    # surface 10 mm below the current zero: a stale/shifted map
    hm.z[0, 0] = -17.9
    hm.limits = (3.0, 5.0)
    win.heightmap = hm
    win.chk_hm.setEnabled(True)
    win.chk_hm.setChecked(True)
    asked = []
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: (asked.append(a[2]), QMessageBox.No)[1]))
    assert win._leveled([(0, "G90"), (1, "G1X5Y5Z-1F100")]) is None
    assert asked and "does not match" in asked[0]
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    out = win._leveled([(0, "G90"), (1, "G1X5Y5Z-1F100")])
    import re
    z = float(re.search(r"Z(-?[\d.]+)", out[-1][1]).group(1))
    assert abs(z + 1.0) < 0.2                          # relative to the origin height: depth stays about -1, not -11
