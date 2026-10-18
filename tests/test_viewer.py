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
    v.set_view("top")
    o, x, y = v.project(np.array([[0.0, 0, 0], [10.0, 0, 0], [0, 10.0, 0]]))
    assert x[0] > o[0] and y[1] < o[1]   # X to the right, Y up
