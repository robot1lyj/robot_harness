import io

import numpy as np
import pytest
from PIL import Image
from pydantic import ValidationError

from robot_rsi.contracts import FFSConfig, HarnessError, StereoCalibration
from robot_rsi.depth import Depth, FastFoundationStereo, disparity_to_depth, import_stereo
from robot_rsi.depth_backends import FFSDepth
from robot_rsi.storage import Store


def calibration(**updates):
    intrinsics = {"width": 32, "height": 16, "fx": 100, "fy": 100, "cx": 16, "cy": 8}
    return StereoCalibration.model_validate(
        {
            "version": "synthetic-calib-1",
            "frame_id": "left_optical",
            "left": intrinsics,
            "right": intrinsics,
            "baseline_m": 0.05,
            "rectified": True,
            "source": "TEST FIXTURE, NOT A CAMERA CALIBRATION",
            **updates,
        }
    )


def png(width=32, height=16, mode="L"):
    buf = io.BytesIO()
    Image.new(mode, (width, height), 120).save(buf, format="PNG")
    return buf.getvalue()


def test_metric_depth_offset_invalids_and_original_pixel_units():
    calib = calibration()
    disparity = np.full((16, 32), 5, dtype=np.float32)
    disparity[0, 10:14] = [0, -1, np.nan, np.inf]
    depth, valid = disparity_to_depth(disparity, calib, 5)
    assert not valid[:, :5].any()  # right-image correspondence outside frame
    assert not valid[0, 10:14].any()
    assert np.isnan(depth[0, 10:14]).all()
    assert depth[8, 20] == pytest.approx(1.0)  # 100 px * .05 m / 5 px
    right = calib.right.model_copy(update={"cx": 15})
    offset = calib.model_copy(update={"right": right})
    assert disparity_to_depth(disparity, offset, 5)[0][8, 20] == pytest.approx(1.25)
    assert not disparity_to_depth(disparity, calib, 0.5)[1].any()
    with pytest.raises(HarnessError, match="shape"):
        disparity_to_depth(disparity[:, :10], calib, 5)


def test_requires_rectification_and_matching_calibration():
    with pytest.raises(ValidationError):
        calibration(rectified=False)
    right = calibration().right.model_dump()
    right["fy"] = 101
    with pytest.raises(ValidationError):
        calibration(right=right)


def test_high_bit_depth_ir_requires_explicit_normalization(profile):
    store = Store(profile)
    try:
        with pytest.raises(HarnessError, match="8-bit"):
            import_stereo(store, png(mode="I;16"), png(mode="I;16"), calibration())
    finally:
        store.close()


class FixturePredictor:
    """Tests storage/geometry only; does not implement or validate NVIDIA inference."""

    async def predict(self, left, right):
        assert Image.open(io.BytesIO(left)).mode == "RGB"
        assert Image.open(io.BytesIO(right)).mode == "RGB"
        return np.full((16, 32), 5, dtype=np.float32), {"provider": "TEST_DOUBLE"}


async def test_stereo_to_depth_artifacts_and_point(profile):
    store = Store(profile)
    try:
        obs = import_stereo(store, png(), png(), calibration())
        depth = Depth(store, FFSDepth(store, FixturePredictor()))
        result = await depth.estimate(obs["id"], 5)
        assert result["unit"] == "metre"
        assert result["confidence"] is None
        assert result["captured_at"] is None
        assert result["source"] == "offline_stereo"
        assert result["provenance"]["provider"] == "TEST_DOUBLE"
        assert store.load_array(result["mask_artifact"]).dtype == np.bool_
        point = depth.point(result["id"], 20, 8, 0)
        assert point["position_m"] == pytest.approx([0.04, 0, 1])
        assert point["frame_id"] == "left_optical"
        with pytest.raises(HarnessError, match="No valid"):
            depth.point(result["id"], 1, 1, 0)
        with pytest.raises(HarnessError, match="outside"):
            depth.point(result["id"], 32, 1, 0)
        with pytest.raises(HarnessError, match="dimensions"):
            import_stereo(store, png(10, 10), png(), calibration())
    finally:
        store.close()


async def test_unconfigured_ffs_fails_without_fabricating_depth(profile):
    provider = FastFoundationStereo(FFSConfig())
    assert not provider.readiness()["ready"]
    with pytest.raises(HarnessError, match="Configure"):
        await provider.predict(png(), png())
    assert provider.process is None
    store = Store(profile)
    try:
        store.put("observation", "preview", {"stereo": None})
        with pytest.raises(HarnessError, match="stereo"):
            await Depth(store, FFSDepth(store, provider)).estimate("preview", 5)
        assert not store.all("depth")
    finally:
        store.close()
