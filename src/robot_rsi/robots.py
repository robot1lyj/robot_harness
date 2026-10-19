from __future__ import annotations

import asyncio
import copy
import io
import json
import time

import httpx
import numpy as np
from PIL import Image, ImageDraw

from .contracts import HarnessError, Profile
from .perception import CameraObservation, CaptureTime, ImageFrame
from .storage import Store, uid


class MockRobot:
    """Explicit synthetic scene; no SDK imports, IK claims, or physical qualification."""

    qualification = "synthetic"
    supports_motion = True
    provider_id = "mock_robot"
    default_camera = "top"
    arms = ("left", "right")
    cameras = ("top", "left", "right")
    camera_inputs = ("preview",)
    camera_identity = {"source": "mock"}
    frames = ("mock_table",)
    supports_hold = True

    def __init__(self, profile: Profile, store: Store):
        self.profile, self.store = profile, store
        self.poses = {"left": [0.25, 0, 0.2], "right": [0.25, -0.2, 0.2]}
        self.openings = {"left": 1.0, "right": 1.0}
        self.holding = {"left": None, "right": None}
        self.objects = {"red_block": {"position_m": [0.35, 0.05, 0.025], "supported": True}}
        self.regions = {"blue_box": {"position_m": [0.45, 0.18, 0.025], "radius_m": 0.045}}
        self.epoch = 0
        self.boot_id = uid("boot")
        self.failures = profile.mock_grasp_failures
        self.commands = []
        self.connected = False
        self.connect_count = self.close_count = 0

    async def connect(self):
        if not self.connected:
            self.connect_count += 1
            self.connected = True

    def state(self):
        return {
            "frame_id": "mock_table",
            "epoch": self.epoch,
            "boot_id": self.boot_id,
            "poses": copy.deepcopy(self.poses),
            "openings": dict(self.openings),
            "holding": dict(self.holding),
            "objects": copy.deepcopy(self.objects),
            "regions": copy.deepcopy(self.regions),
        }

    async def read_state(self):
        return {
            "id": uid("obs"),
            "captured_at": time.time(),
            "source": "mock",
            "qualification": self.qualification,
            "state": self.state(),
        }

    async def read_camera(self, source_role: str):
        sample = await self.observe(source_role)
        stamp = CaptureTime(
            value_s=sample["captured_at"], kind="unix", clock_domain="utc", uncertainty_s=0
        )
        return CameraObservation(
            id=uid("camera_obs"),
            camera=source_role,
            source="mock_camera",
            qualification="synthetic",
            received_at=time.time(),
            frames=[
                ImageFrame(
                    stream_id="preview",
                    artifact_id=sample["image_artifact"],
                    width=640,
                    height=480,
                    encoding="rgb8",
                    capture_time=stamp,
                )
            ],
        )

    async def observe(self, camera: str):
        if camera not in self.cameras:
            raise HarnessError("unsupported_camera", "Camera role is not bound on this station")
        state = self.state()
        image = Image.new("RGB", (640, 480), "#eef2f5")
        draw = ImageDraw.Draw(image)
        draw.text((12, 12), "SYNTHETIC YAM SCENE - NOT HARDWARE", fill="black")

        def xy(p):
            return int(p[0] * 800), int(300 - p[1] * 600)

        x, y = xy(self.regions["blue_box"]["position_m"])
        draw.rectangle((x - 40, y - 30, x + 40, y + 30), outline="blue", width=4)
        x, y = xy(self.objects["red_block"]["position_m"])
        draw.rectangle((x - 10, y - 10, x + 10, y + 10), fill="red")
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        artifact = self.store.artifact(buf.getvalue(), "image/png", {"source": "synthetic"})
        return {
            "id": uid("obs"),
            "captured_at": time.time(),
            "camera": camera,
            "qualification": self.qualification,
            "source": "mock",
            "state": state,
            "image_artifact": artifact,
            "stereo": None,
            "measurement_note": "Object poses are simulator state, not visual estimates",
        }

    def validate_position(self, point):
        x, y, z = point
        if not (0.1 <= x <= 0.6 and -0.3 <= y <= 0.3 and 0.02 <= z <= 0.4):
            raise HarnessError("workspace_limit", "Target is outside the synthetic workspace")

    def validate_arm(self, arm: str):
        if arm not in self.arms:
            raise HarnessError("unsupported_arm", "Manipulator role is not bound on this station")

    def validate_observation(self, observation: dict):
        if "model_ref" in observation:
            from .device_validation import validate_state_age

            validate_state_age(observation, self.boot_id, self.epoch, self.profile.observation_max_age_s)
            return
        captured = observation.get("captured_at")
        if (
            captured is None
            or not 0 <= time.time() - captured <= self.profile.observation_max_age_s
        ):
            raise HarnessError("stale_observation", "Acquire a fresh observation before motion")
        state = observation.get("state") or {}
        if (
            observation.get("source") != "mock"
            or state.get("epoch") != self.epoch
            or state.get("boot_id") != self.boot_id
        ):
            raise HarnessError("stale_observation", "Observation no longer matches execution state")

    async def move(self, arm: str, point):
        self.validate_arm(arm)
        self.validate_position(point)
        await asyncio.sleep(self.profile.mock_step_s)
        self.poses[arm] = list(point)
        self.commands.append({"move": arm, "position_m": list(point)})
        if object_id := self.holding[arm]:
            self.objects[object_id]["position_m"] = list(point)

    async def gripper(self, arm: str, opening: float):
        self.validate_arm(arm)
        await asyncio.sleep(self.profile.mock_step_s)
        self.openings[arm] = opening
        self.commands.append({"gripper": arm, "opening": opening})
        if opening > 0.5:
            if object_id := self.holding[arm]:
                self.objects[object_id]["supported"] = self.poses[arm][2] <= 0.03
            self.holding[arm] = None
        elif not self.holding[arm]:
            if self.failures:
                self.failures -= 1
                return
            for object_id, obj in self.objects.items():
                distance = np.linalg.norm(np.array(obj["position_m"]) - self.poses[arm])
                if obj["supported"] and distance < 0.02:
                    self.holding[arm] = object_id
                    obj["supported"] = False
                    break

    async def hold(self):
        self.epoch += 1
        return {
            "confirmed": True,
            "epoch": self.epoch,
            "qualification": self.qualification,
            "grippers_retained": True,
        }

    async def close(self):
        self.close_count += 1
        self.connected = False


class LegacyYam:
    """Temporary reader while YAM implementation migrates into this project; no second SDK owner."""

    qualification = "legacy_unvalidated"
    supports_motion = False
    provider_id = "yam_legacy"
    default_camera = "top"
    arms = ("left", "right")
    cameras = ("top", "left", "right")
    camera_inputs = ("preview",)
    frames = ()

    async def connect(self):
        # This reader has no SDK/session connection; explicit reads contact the existing owner.
        return None

    def validate_arm(self, arm: str):
        if arm not in self.arms:
            raise HarnessError("unsupported_arm", "Manipulator role is not bound on this station")

    def validate_observation(self, observation: dict):
        raise HarnessError("unsupported", "Legacy YAM has no motion admission contract yet")

    def __init__(self, profile: Profile, store: Store, *, transport=None):
        self.profile, self.store = profile, store
        self.camera_identity = {"source": "yam_transition", "endpoint": profile.yam_url}
        self.supports_hold = profile.yam_allow_hold
        self.client = httpx.AsyncClient(
            base_url=profile.yam_url,
            timeout=3,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )
        self.session_id = uid("harness")

    async def read_state(self):
        try:
            response = await self.client.get("/status")
            response.raise_for_status()
            state = response.json()
            if not isinstance(state, dict):
                raise ValueError("Status must be an object")
        except (httpx.HTTPError, ValueError) as exc:
            raise HarnessError("device_unavailable", "YAM owner status could not be read") from exc
        raw = self.store.artifact(json.dumps(state).encode(), "application/json")
        return {
            "id": uid("obs"),
            "captured_at": None,
            "received_at": time.time(),
            "source": "yam_legacy",
            "qualification": self.qualification,
            "state": {
                key: state.get(key)
                for key in (
                    "connection",
                    "mode",
                    "source",
                    "hil_input",
                    "phase",
                    "epoch",
                    "tick",
                    "running",
                    "camera_connection",
                    "follower_state",
                    "sdk_state_age_s",
                    "stop_latched",
                    "operator_error",
                    "error",
                )
            },
            "state_artifact": raw,
        }

    async def read_camera(self, source_role: str):
        from .cameras import image_frame

        if source_role not in self.cameras:
            raise HarnessError(
                "unsupported_camera", "Camera source role is not owned by this device owner"
            )
        try:
            async with self.client.stream("GET", f"/camera/{source_role}.jpg") as response:
                if response.status_code == 404:
                    raise HarnessError("camera_unavailable", "Legacy preview is missing or expired")
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 32 * 1024 * 1024:
                        raise HarnessError("invalid_frame", "Preview exceeds size limit")
        except httpx.HTTPError as exc:
            raise HarnessError("camera_unavailable", "Camera owner could not be read") from exc
        frame = image_frame(self.store, bytes(data), "preview")
        return CameraObservation(
            id=uid("camera_obs"),
            camera=source_role,
            source="yam_preview",
            qualification=self.qualification,
            received_at=time.time(),
            frames=[frame],
        )

    async def hold(self):
        if not self.profile.yam_allow_hold:
            raise HarnessError("not_enabled", "Host profile has not enabled legacy HOLD")
        try:
            response = await self.client.post(
                "/event/hold", headers={"X-YAM-Control": "1", "X-YAM-Session": self.session_id}
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HarnessError("hold_unknown", "HOLD acknowledgement unavailable") from exc
        return {
            "confirmed": False,
            "queued": True,
            "note": "Legacy acknowledgement does not prove physical stop",
        }

    async def close(self):
        await self.client.aclose()
