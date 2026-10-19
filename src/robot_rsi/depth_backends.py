"""Explicit implementations of the metric-depth contract; no automatic fallback."""

from __future__ import annotations

import hashlib

from .capabilities import Capability
from .contracts import HarnessError
from .depth import disparity_to_depth
from .perception import CameraObservation, MetricDepth
from .storage import canonical


def manifest(provider, config=None):
    return (
        Capability(
            name="depth.estimate",
            contract_version=1,
            provider_id=provider,
            implementation_version="0.1.0",
            qualification="unvalidated",
            configuration_id=hashlib.sha256(canonical(config or {}).encode()).hexdigest(),
        ),
    )


class FFSDepth:
    def __init__(self, store, predictor, config=None):
        self.store, self.predictor = store, predictor
        self.capabilities = manifest("nvidia_ffs", config)

    def readiness(self):
        return self.predictor.readiness()

    async def estimate(self, observation: CameraObservation, max_depth_m: float):
        pair = observation.stereo
        if pair is None:
            raise HarnessError(
                "stereo_required", "Selected depth provider requires rectified calibrated stereo"
            )
        left, right = observation.image(pair.left_stream), observation.image(pair.right_stream)
        disparity, provenance = await self.predictor.predict(
            self.store.read_artifact(left.artifact_id), self.store.read_artifact(right.artifact_id)
        )
        depth, _ = disparity_to_depth(disparity, pair.calibration, max_depth_m)
        return MetricDepth(
            depth_m=depth,
            intrinsics=pair.calibration.left,
            frame_id=pair.calibration.frame_id,
            calibration_version=pair.calibration.version,
            source_stream=left.stream_id,
            capture_time=left.capture_time,
            qualification="inference_not_accuracy_validated",
            provenance=provenance,
        )

    async def close(self):
        await self.predictor.close()
