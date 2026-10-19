"""Camera frame contract over the project's single device owner; offline import is a test path."""

from __future__ import annotations

import asyncio
import hashlib
import io
import time

from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError

from .capabilities import Capability
from .contracts import HarnessError, StereoCalibration
from .perception import CameraObservation, ImageFrame, StereoInput
from .storage import Store, canonical, uid


def image_frame(
    store: Store, data: bytes, stream: str, *, frame_id=None, capture_time=None, intrinsics=None
) -> ImageFrame:
    if len(data) > 32 * 1024 * 1024:
        raise HarnessError("invalid_frame", "Image exceeds size limit")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.mode not in ("L", "RGB", "RGBA", "P"):
                raise HarnessError(
                    "invalid_stereo", "Normalize high-bit-depth IR to 8-bit explicitly"
                )
            width, height = image.size
            if intrinsics and (width, height) != (intrinsics.width, intrinsics.height):
                raise HarnessError("invalid_stereo", "Image dimensions disagree with calibration")
            if width > 8192 or height > 8192:
                raise HarnessError("invalid_frame", "Image dimensions exceed the contract")
            # Canonical lossless artifacts. Never rescale pixels or silently change intrinsics.
            rgb = image.convert("RGB")
            output = io.BytesIO()
            rgb.save(output, format="PNG")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise HarnessError("invalid_frame", "Camera did not return a decodable image") from exc
    artifact = store.artifact(output.getvalue(), "image/png", {"stream_id": stream})
    return ImageFrame(
        stream_id=stream,
        artifact_id=artifact,
        width=width,
        height=height,
        encoding="rgb8",
        frame_id=frame_id,
        capture_time=capture_time,
        intrinsics=intrinsics,
    )


def stereo_observation(
    store: Store,
    left: bytes,
    right: bytes,
    calibration: StereoCalibration,
    *,
    camera="offline",
    source="offline_stereo",
) -> CameraObservation:
    frames = [
        image_frame(store, data, stream, frame_id=frame, intrinsics=intr)
        for data, stream, frame, intr in (
            (left, "left", calibration.frame_id, calibration.left),
            (right, "right", None, calibration.right),
        )
    ]
    return CameraObservation(
        id=uid("camera_obs"),
        camera=camera,
        source=source,
        qualification="offline_unvalidated",
        received_at=time.time(),
        frames=frames,
        stereo=StereoInput(
            left_stream="left", right_stream="right", calibration=calibration, pairing="offline"
        ),
    )


def save_camera(store: Store, observation: CameraObservation):
    with store.transaction():
        store.put("camera_observation", observation.id, observation.model_dump())
        store.event(
            "camera.captured",
            {"id": observation.id, "camera": observation.camera, "source": observation.source},
        )
    return observation


def load_camera(store: Store, identity: str) -> CameraObservation:
    try:
        data = store.get("camera_observation", identity)
    except HarnessError as exc:
        if exc.code != "not_found":
            raise
        legacy = store.get("observation", identity)
        if not legacy.get("camera_observation_id"):
            raise HarnessError(
                "camera_observation_required",
                "Need a typed camera observation; preview alone is not calibrated stereo",
            )
        data = store.get("camera_observation", legacy["camera_observation_id"])
    return CameraObservation.model_validate(data)


class Cameras:
    def __init__(self, owner, config, store: Store):
        self.owner, self.config, self.store = owner, config, store
        if not set(config.roles.values()) <= set(owner.cameras):
            raise ValueError("Camera mapping references an unknown source role")
        self.default_camera = config.default_camera
        self.lock = asyncio.Lock()
        self.capabilities = tuple(
            Capability(
                name=name,
                contract_version=1,
                provider_id=owner.provider_id + "_camera",
                implementation_version="0.1.0",
                qualification=owner.qualification,
                configuration_id=hashlib.sha256(
                    canonical(
                        {"mapping": config.model_dump(), "owner": owner.camera_identity}
                    ).encode()
                ).hexdigest(),
            )
            for name in ("camera.describe", "camera.capture")
        )

    def inputs(self):
        return {role: list(self.owner.camera_inputs) for role in self.config.roles}

    def describe(self):
        return {
            "default_camera": self.default_camera,
            "cameras": [
                {"role": role, "inputs": inputs, "availability": "not_checked"}
                for role, inputs in self.inputs().items()
            ],
        }

    async def capture(self, camera=None, require="any", max_age_s=None):
        role = camera or self.default_camera
        descriptions = self.inputs()
        if role not in descriptions:
            raise HarnessError("unsupported_camera", "Semantic camera role is not configured")
        if require != "any" and require not in descriptions[role]:
            raise HarnessError(
                "unsupported_camera_input",
                "Camera does not provide requested input",
                details={"camera": role, "required": require},
            )
        if self.lock.locked():
            raise HarnessError("camera_busy", "A capture is already in progress; no stale queue")
        async with self.lock:
            try:
                async with asyncio.timeout(self.config.capture_timeout_s):
                    raw = await self.owner.read_camera(self.config.roles[role])
                observation = CameraObservation.model_validate(raw.model_dump())
            except TimeoutError as exc:
                raise HarnessError("camera_timeout", "Camera capture deadline exceeded") from exc
            except ValidationError as exc:
                raise HarnessError(
                    "invalid_frame", "Camera backend returned an invalid frame contract"
                ) from exc
            if observation.camera != self.config.roles[role]:
                raise HarnessError("invalid_frame", "Camera owner returned a different source role")
            observation.source_role = observation.camera
            observation.camera = role
            if require == "stereo" and not observation.stereo:
                raise HarnessError(
                    "invalid_frame", "Camera owner omitted the requested stereo pair"
                )
            if require == "preview" and not observation.frames:
                raise HarnessError("invalid_frame", "Camera backend omitted the requested image")
            if max_age_s is not None:
                stamps = [frame.capture_time for frame in observation.frames]
                now = time.time()
                for stamp in stamps:
                    if (
                        not stamp
                        or stamp.kind != "unix"
                        or stamp.clock_domain != "utc"
                        or stamp.uncertainty_s is None
                    ):
                        raise HarnessError(
                            "freshness_unknown",
                            "Acquisition time is not comparable with the host clock",
                        )
                    age = now - stamp.value_s
                    if age < -stamp.uncertainty_s or age + stamp.uncertainty_s > max_age_s:
                        raise HarnessError(
                            "stale_observation", "Camera frame is outside the requested age budget"
                        )
            return save_camera(self.store, observation)

    async def close(self):
        pass  # Runtime owns the single device owner and closes it once.
