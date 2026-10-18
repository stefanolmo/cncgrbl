import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from cncdeck.viewer import ToolpathView


@pytest.mark.parametrize("view", ["iso", "front", "right", "left"])
def test_z_up_is_up_on_screen(view):
    QApplication.instance() or QApplication([])
    v = ToolpathView()
    v.resize(400, 300)
    v.set_view(view)
    o, z = v.project(np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 10.0]]))
    assert z[1] < o[1]          # screen y grows downwards: higher Z => smaller y


def test_top_view_axes():
    QApplication.instance() or QApplication([])
    v = ToolpathView()
    v.resize(400, 300)
    v.set_origin("bottom-left")
    v.set_view("top")
    o, x, y = v.project(np.array([[0.0, 0, 0], [10.0, 0, 0], [0, 10.0, 0]]))
    assert x[0] > o[0] and y[1] < o[1]   # X to the right, Y up


@pytest.mark.parametrize("origin,x_dir,y_dir", [
    ("bottom-left", (1, 0), (0, 1)), ("bottom-right", (0, 1), (-1, 0)),
    ("top-right", (-1, 0), (0, -1)), ("top-left", (0, -1), (1, 0))])
def test_origin_is_a_rotation_in_top_view(origin, x_dir, y_dir):
    QApplication.instance() or QApplication([])
    v = ToolpathView()
    v.resize(400, 300)
    v.set_origin(origin)
    v.set_view("top")
    o, x, y = v.project(np.array([[0.0, 0, 0], [10.0, 0, 0], [0, 10.0, 0]]))
    dx, dy = np.round(x - o, 6), np.round(y - o, 6)
    # screen y grows downwards, so "up" is negative
    assert np.sign(dx[0]) == x_dir[0] and np.sign(-dx[1]) == x_dir[1]
    assert np.sign(dy[0]) == y_dir[0] and np.sign(-dy[1]) == y_dir[1]
    # right-handed: X x Y points toward the viewer (never a mirror image)
    assert (dx[0] * -dy[1] - (-dx[1]) * dy[0]) > 0


def test_orbit_drag_follows_mouse():
    QApplication.instance() or QApplication([])
    v = ToolpathView()
    v.resize(400, 300)
    v.set_origin("bottom-left")
    v.yaw, v.pitch = 0.0, 55.0
    near = np.array([[0.0, -10.0, 0.0]])        # front edge of the work plane
    x0, y0 = v.project(near)[0]
    v.orbit(30, 0)                              # drag right -> front surface moves right
    assert v.project(near)[0][0] > x0
    v.yaw = 0.0
    v.orbit(0, 30)                              # drag down -> front edge moves down
    assert v.project(near)[0][1] > y0


def test_default_origin_is_top_right_and_z_still_up():
    QApplication.instance() or QApplication([])
    v = ToolpathView()
    v.resize(400, 300)
    assert v.origin == "top-right"
    v.set_view("iso")
    o, z = v.project(np.array([[0.0, 0, 0], [0, 0, 10.0]]))
    assert z[1] < o[1]
