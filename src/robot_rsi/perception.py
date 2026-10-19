"""Portable camera/depth contracts; artifacts contain pixels, contracts contain semantics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

import numpy as np
from pydantic import Field, model_validator

from .contracts import Contract, Finite, Identifier, Intrinsics, StereoCalibration

if TYPE_CHECKING:
    from .capabilities import Capability


class CaptureTime(Contract):
    value_s: Finite
    kind: Literal["unix", "device_monotonic"]
    clock_domain: Identifier
    uncertainty_s: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class ImageFrame(Contract):
    stream_id: Identifier
    artifact_id: Identifier
    width: int = Field(gt=0, le=8192)
    height: int = Field(gt=0, le=8192)
    encoding: Literal["rgb8", "mono8"]
    frame_id: Identifier | None = None
    capture_time: CaptureTime | None = None
    intrinsics: Intrinsics | None = None

    @model_validator(mode="after")
    def dimensions_match(self):
        if self.intrinsics and (self.width, self.height) != (
            self.intrinsics.width,
            self.intrinsics.height,
        ):
            raise ValueError("Image dimensions and intrinsics disagree")
        return self


class StereoInput(Contract):
    left_stream: Identifier
    right_stream: Identifier
    calibration: StereoCalibration
    pairing: Literal["offline", "hardware", "synchronized"]
    max_skew_ms: float = Field(default=5, ge=0, le=1000, allow_inf_nan=False)


class CameraObservation(Contract):
    schema_version: Literal[1] = 1
    id: Identifier
    camera: Identifier
    source_role: Identifier | None = None
    source: str
    qualification: str
    received_at: Finite
    frames: list[ImageFrame] = Field(default_factory=list, max_length=8)
    stereo: StereoInput | None = None

    @model_validator(mode="after")
    def consistent_streams(self):
        names = [frame.stream_id for frame in self.frames]
        if len(names) != len(set(names)):
            raise ValueError("Stream identities must be unique in a camera observation")
        if not names:
            raise ValueError("Camera observation must contain an image or depth stream")
        if self.stereo:
            pair = self.stereo
            if pair.left_stream == pair.right_stream:
                raise ValueError("Stereo needs distinct left and right streams")
            indexed = {frame.stream_id: frame for frame in self.frames}
            left, right = indexed.get(pair.left_stream), indexed.get(pair.right_stream)
            if not left or not right:
                raise ValueError("Stereo must reference existing image streams")
            for frame, intrinsics in (
                (left, pair.calibration.left),
                (right, pair.calibration.right),
            ):
                if (frame.width, frame.height) != (intrinsics.width, intrinsics.height):
                    raise ValueError("Stereo dimensions disagree with calibration")
                if frame.intrinsics and frame.intrinsics != intrinsics:
                    raise ValueError("Stereo image intrinsics disagree with calibration")
            if left.frame_id != pair.calibration.frame_id:
                raise ValueError("Left optical frame must match the stereo calibration")
            if pair.pairing == "synchronized":
                a, b = left.capture_time, right.capture_time
                if not a or not b or (a.kind, a.clock_domain) != (b.kind, b.clock_domain):
                    raise ValueError("Timestamp pairing requires comparable acquisition clocks")
                if a.uncertainty_s is None or b.uncertainty_s is None:
                    raise ValueError("Timestamp pairing requires bounded clock uncertainty")
                if (
                    abs(a.value_s - b.value_s) + a.uncertainty_s + b.uncertainty_s
                    > pair.max_skew_ms / 1000
                ):
                    raise ValueError("Stereo pair exceeds the timestamp skew budget")
        return self

    def image(self, stream_id: str) -> ImageFrame:
        return next(frame for frame in self.frames if frame.stream_id == stream_id)


@dataclass(frozen=True)
class MetricDepth:
    depth_m: np.ndarray
    intrinsics: Intrinsics
    frame_id: str
    calibration_version: str
    source_stream: str
    capture_time: CaptureTime | None
    qualification: str
    provenance: dict


class CameraPort(Protocol):
    capabilities: tuple[Capability, ...]

    def describe(self) -> dict: ...

    async def capture(
        self, camera: str | None, require: str = "any", max_age_s: float | None = None
    ) -> CameraObservation: ...


class DepthProvider(Protocol):
    capabilities: tuple[Capability, ...]

    def readiness(self) -> dict: ...

    async def estimate(self, observation: CameraObservation, max_depth_m: float) -> MetricDepth: ...

    async def close(self) -> None: ...


class DepthPort(Protocol):
    capabilities: tuple[Capability, ...]

    async def estimate(self, observation_id: str, max_depth_m: float) -> dict: ...
