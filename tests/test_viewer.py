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


@pytest.mark.parametrize("origin,x_right,y_up", [("bottom-left", True, True), ("top-left", True, False),
                                                 ("top-right", False, False), ("bottom-right", False, True)])
def test_origin_corner_in_top_view(origin, x_right, y_up):
    QApplication.instance() or QApplication([])
    v = ToolpathView()
    v.resize(400, 300)
    v.set_origin(origin)
    v.set_view("top")
    o, x, y = v.project(np.array([[0.0, 0, 0], [10.0, 0, 0], [0, 10.0, 0]]))
    assert (x[0] > o[0]) == x_right
    assert (y[1] < o[1]) == y_up


def test_default_origin_is_top_right_and_z_still_up():
    QApplication.instance() or QApplication([])
    v = ToolpathView()
    v.resize(400, 300)
    assert v.origin == "top-right"
    v.set_view("iso")
    o, z = v.project(np.array([[0.0, 0, 0], [0, 0, 10.0]]))
    assert z[1] < o[1]
