import asyncio
import io
import time
from dataclasses import replace

import httpx
import numpy as np
import pytest
from PIL import Image
from pydantic import ValidationError

from robot_rsi.cameras import stereo_observation
from robot_rsi.contracts import HarnessError, HostConfig, Profile, StereoCalibration
from robot_rsi.depth import Depth, import_stereo
from robot_rsi.depth_backends import FFSDepth
from robot_rsi.perception import CameraObservation, CaptureTime
from robot_rsi.runtime import Runtime
from robot_rsi.storage import Store


def calibration():
    intr = {"width": 32, "height": 16, "fx": 100, "fy": 100, "cx": 16, "cy": 8}
    return StereoCalibration(
        version="fixture_calibration",
        frame_id="sensor_optical",
        left=intr,
        right=intr,
        baseline_m=0.05,
        rectified=True,
        source="TEST FIXTURE ONLY",
    )


def image_bytes():
    stream = io.BytesIO()
    Image.new("RGB", (32, 16), "gray").save(stream, format="PNG")
    return stream.getvalue()


def configured(profile, cameras):
    return Profile.model_validate({**profile.model_dump(), "host": {"cameras": cameras}})


class DisparityFixture:
    """FFS plumbing test double; no NVIDIA execution or accuracy claim."""

    def __init__(self):
        self.calls = 0

    def readiness(self):
        return {"configured": True, "ready": True, "reason": "TEST DOUBLE"}

    async def predict(self, left, right):
        assert Image.open(io.BytesIO(left)).size == (32, 16)
        self.calls += 1
        return np.full((16, 32), 5, dtype=np.float32), {"provider": "TEST_DOUBLE"}

    async def close(self):
        pass


async def test_role_mapping_uses_same_skill_and_single_owner(profile):
    profile = configured(profile, {"roles": {"workcell": "top"}, "default_camera": "workcell"})
    async with Runtime(profile) as runtime:
        assert runtime.cameras.owner is runtime.robot
        station = await runtime.invoke("station_describe")
        assert station["result"]["cameras"] == ["workcell"]
        catalog = await runtime.invoke("capabilities_list")
        names = {c["name"] for c in catalog["result"]["capabilities"]}
        assert {"camera.describe", "camera.capture", "depth.estimate"} <= names
        op = await runtime.invoke("task_run", {"request_id": "new-view"})
        done = await runtime.operations.get(op["result"]["id"], 3)
        assert done["status"] == "succeeded"
        assert all(obs["camera"] == "workcell" for obs in runtime.store.all("camera_observation"))
        assert all(obs["source_role"] == "top" for obs in runtime.store.all("camera_observation"))
        wrong = await runtime.invoke("camera_capture", {"camera": "top"})
        assert wrong["error"]["code"] == "unsupported_camera"
        injected = await runtime.invoke(
            "camera_capture", {"endpoint": "http://other/", "path": "/tmp/file"}
        )
        assert injected["error"]["code"] == "invalid_arguments"


async def test_offline_ffs_input_keeps_frame_identity_without_another_camera_system(profile):
    predictor = DisparityFixture()
    async with Runtime(profile, depth_provider=predictor) as runtime:
        imported = import_stereo(runtime.store, image_bytes(), image_bytes(), calibration())
        camera = runtime.store.get("camera_observation", imported["camera_observation_id"])
        assert camera["stereo"]["pairing"] == "offline"
        assert all(frame["capture_time"] is None for frame in camera["frames"])
        result = await runtime.invoke("depth_estimate", {"observation_id": imported["id"]})
        assert result["ok"], result
        depth = result["result"]
        assert depth["unit"] == "metre" and depth["source_stream"] == "left"
        assert depth["captured_at"] is None
        assert depth["provenance"]["provider"] == "TEST_DOUBLE" and predictor.calls == 1
        point = await runtime.invoke(
            "depth_point", {"depth_id": depth["id"], "stream_id": "left", "u": 20, "v": 8}
        )
        assert point["result"]["position_m"] == pytest.approx([0.04, 0, 1])
        mismatch = await runtime.invoke(
            "depth_point", {"depth_id": depth["id"], "stream_id": "rgb", "u": 20, "v": 8}
        )
        assert mismatch["error"]["code"] == "pixel_stream_mismatch"


async def test_yam_transition_reader_does_not_claim_stereo_or_acquisition_time(profile):
    requests = []

    def handler(request):
        requests.append((request.method, request.url.path))
        if request.url.path == "/status":
            return httpx.Response(200, json={"tick": 42, "phase": "hold"})
        return httpx.Response(200, content=image_bytes())

    profile.robot = "yam_legacy"
    profile = configured(profile, {"roles": {"workspace": "top"}, "default_camera": "workspace"})
    async with Runtime(profile, robot_transport=httpx.MockTransport(handler)) as runtime:
        await runtime.invoke("camera_describe")
        assert not requests
        unavailable = await runtime.invoke("camera_capture", {"require": "stereo"})
        assert unavailable["error"]["code"] == "unsupported_camera_input" and not requests
        obs = await runtime.observe()
        assert obs["state"]["tick"] == 42 and obs["camera"] == "workspace"
        assert not obs["state_camera_synchronized"] and obs["captured_at"] is None
        bundle = runtime.store.get("camera_observation", obs["camera_observation_id"])
        assert bundle["frames"][0]["capture_time"] is None and bundle["stereo"] is None
        depth = await runtime.invoke("depth_estimate", {"observation_id": obs["id"]})
        assert depth["error"]["code"] == "stereo_required"
    assert requests == [("GET", "/status"), ("GET", "/camera/top.jpg")]


@pytest.mark.parametrize("status,content", [(404, b""), (200, b"not an image"), (503, b"failed")])
async def test_bad_images_never_become_camera_evidence(profile, status, content):
    def handler(request):
        if request.url.path == "/status":
            return httpx.Response(200, json={"tick": 3})
        return httpx.Response(status, content=content)

    profile.robot = "yam_legacy"
    async with Runtime(profile, robot_transport=httpx.MockTransport(handler)) as runtime:
        obs = await runtime.observe()
        assert obs["state"]["tick"] == 3
        assert obs["camera_observation_id"] is None and obs["image_artifact"] is None
        assert obs["camera_error"]["code"] in ("camera_unavailable", "invalid_frame")
        assert not runtime.store.all("camera_observation")


@pytest.mark.parametrize(
    "kind,offset,uncertainty,code",
    [
        ("device_monotonic", 0, 0, "freshness_unknown"),
        ("unix", 0, None, "freshness_unknown"),
        ("unix", -10, 0, "stale_observation"),
        ("unix", 10, 0, "stale_observation"),
    ],
)
async def test_freshness_never_uses_receive_time(
    profile, monkeypatch, kind, offset, uncertainty, code
):
    async with Runtime(profile) as runtime:
        original = runtime.robot.read_camera

        async def altered(role):
            observation = await original(role)
            observation.frames[0].capture_time = CaptureTime(
                value_s=time.time() + offset,
                kind=kind,
                clock_domain="device_a" if kind == "device_monotonic" else "utc",
                uncertainty_s=uncertainty,
            )
            return observation

        monkeypatch.setattr(runtime.robot, "read_camera", altered)
        result = await runtime.invoke("camera_capture", {"max_age_s": 1})
        assert result["error"]["code"] == code
        assert not runtime.store.all("camera_observation")


async def test_busy_and_cancel_do_not_queue_captures(profile, monkeypatch):
    async with Runtime(profile) as runtime:
        started = asyncio.Event()
        original = runtime.robot.read_camera

        async def slow(role):
            started.set()
            await asyncio.sleep(10)
            return await original(role)

        monkeypatch.setattr(runtime.robot, "read_camera", slow)
        task = asyncio.create_task(runtime.cameras.capture())
        await started.wait()
        with pytest.raises(HarnessError) as caught:
            await runtime.cameras.capture()
        assert caught.value.code == "camera_busy"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not runtime.store.all("camera_observation")
        monkeypatch.setattr(runtime.robot, "read_camera", original)
        assert (await runtime.cameras.capture(max_age_s=1)).frames


async def test_deadline_releases_capture_owner(profile, monkeypatch):
    profile = configured(profile, {"capture_timeout_s": 0.02})
    async with Runtime(profile) as runtime:

        async def too_slow(role):
            await asyncio.sleep(1)

        monkeypatch.setattr(runtime.robot, "read_camera", too_slow)
        result = await runtime.invoke("camera_capture")
        assert result["error"]["code"] == "camera_timeout"
        assert not runtime.cameras.lock.locked() and not runtime.store.all("camera_observation")


def test_stereo_contract_rejects_wrong_streams_frames_and_pairing(profile):
    store = Store(profile)
    try:
        raw = stereo_observation(store, image_bytes(), image_bytes(), calibration()).model_dump()
        bad = CameraObservation.model_validate(raw).model_dump()
        bad["stereo"]["right_stream"] = "left"
        with pytest.raises(ValidationError, match="distinct"):
            CameraObservation.model_validate(bad)
        bad = CameraObservation.model_validate(raw).model_dump()
        bad["frames"][0]["frame_id"] = "unrelated_frame"
        with pytest.raises(ValidationError, match="optical frame"):
            CameraObservation.model_validate(bad)
        bad = CameraObservation.model_validate(raw).model_dump()
        bad["stereo"]["pairing"] = "synchronized"
        with pytest.raises(ValidationError, match="clocks"):
            CameraObservation.model_validate(bad)
        for index, frame in enumerate(bad["frames"]):
            frame["capture_time"] = {
                "kind": "unix",
                "clock_domain": "utc",
                "uncertainty_s": 0,
                "value_s": 100 + index,
            }
        with pytest.raises(ValidationError, match="skew"):
            CameraObservation.model_validate(bad)
    finally:
        store.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("frame_id", "wrong_frame"),
        ("calibration_version", "wrong_version"),
        ("capture_time", CaptureTime(value_s=100, kind="unix", clock_domain="utc")),
    ],
)
async def test_depth_cannot_relabel_evidence(profile, field, value):
    store = Store(profile)
    try:
        observation = import_stereo(store, image_bytes(), image_bytes(), calibration())

        class IncorrectDepth(FFSDepth):
            async def estimate(self, observation, maximum):
                output = await super().estimate(observation, maximum)
                return replace(output, **{field: value})

        with pytest.raises(HarnessError) as caught:
            await Depth(store, IncorrectDepth(store, DisparityFixture())).estimate(
                observation["id"], 2
            )
        assert caught.value.code == "invalid_depth" and not store.all("depth")
    finally:
        store.close()


def test_minimal_config_rejects_missing_roles_and_unimplemented_backends(profile):
    with pytest.raises(ValidationError, match="default_camera"):
        configured(profile, {"roles": {"view": "top"}, "default_camera": "missing"})
    with pytest.raises(ValidationError):
        HostConfig.model_validate({"depth": {"provider": "native"}})
    with pytest.raises(ValidationError):
        Profile(yam_url="http://")
    invalid = configured(profile, {"roles": {"view": "unknown"}, "default_camera": "view"})
    with pytest.raises(ValueError, match="source role"):
        Runtime(invalid)
