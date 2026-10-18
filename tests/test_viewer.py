import numpy as np
import pytest
from PySide6.QtWidgets import QApplication


def make():
    from cncdeck.viewer import ToolpathView
    QApplication.instance() or QApplication([])
    v = ToolpathView()
    v.resize(400, 300)
    return v


def test_top_view_x_right_y_up():
    v = make()
    v.set_view("top")
    o, x, y = v.project(np.array([[0.0, 0, 0], [10.0, 0, 0], [0, 10.0, 0]]))
    assert x[0] > o[0] and abs(x[1] - o[1]) < 1e-6      # X to the right
    assert y[1] < o[1] and abs(y[0] - o[0]) < 1e-6      # Y upwards


@pytest.mark.parametrize("view", ["iso", "front", "right", "left"])
def test_z_up_is_up_on_screen(view):
    v = make()
    v.set_view(view)
    o, z = v.project(np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 10.0]]))
    assert z[1] < o[1]          # screen y grows downwards: higher Z => smaller y


def test_orbit_drag_follows_mouse():
    v = make()
    v.yaw, v.pitch = 0.0, 55.0
    cy = v.center[1]
    cand = np.array([[v.center[0], cy - 10.0, 0.0], [v.center[0], cy + 10.0, 0.0]])
    near = cand[[int(np.argmax(v.project(cand)[:, 1]))]]    # edge of the plane nearest the viewer
    x0, y0 = v.project(near)[0]
    v.orbit(30, 0)                      # drag right -> surface under the cursor moves right
    assert v.project(near)[0][0] > x0
    v.yaw = 0.0
    v.orbit(0, 30)                      # drag down -> near edge moves down
    assert v.project(near)[0][1] > y0
