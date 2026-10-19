import pytest
from legacy_lab.capture import read_recording
from legacy_lab.geometry import tip_mm


def test_recordings_have_different_resolution():
    assert read_recording("detail")["width"] > read_recording("survey")["width"]


def test_slider_known_point_and_range():
    assert tip_mm(300, 250) == [300, 0, 250]
    with pytest.raises(ValueError):
        tip_mm(1001, 250)
