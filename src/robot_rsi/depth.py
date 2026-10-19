from __future__ import annotations

import asyncio
import json
import os
import signal
import tempfile
import time
from pathlib import Path

import numpy as np

from .contracts import FFSConfig, HarnessError, StereoCalibration
from .storage import Store, uid


def import_stereo(store: Store, left: bytes, right: bytes, calibration: StereoCalibration) -> dict:
    """Compatibility host import; all new data also uses the camera v1 contract."""
    from .cameras import save_camera, stereo_observation

    camera = save_camera(store, stereo_observation(store, left, right, calibration))
    observation = {
        "id": uid("obs"),
        "captured_at": None,
        "received_at": camera.received_at,
        "source": camera.source,
        "qualification": camera.qualification,
        "state": None,
        "image_artifact": camera.frames[0].artifact_id,
        "camera_observation_id": camera.id,
        "stereo": {
            "left_artifact": camera.frames[0].artifact_id,
            "right_artifact": camera.frames[1].artifact_id,
            "calibration": calibration.model_dump(),
        },
    }
    store.put("observation", observation["id"], observation)
    return observation


def disparity_to_depth(
    disparity: np.ndarray, calibration: StereoCalibration, max_depth_m: float
) -> tuple[np.ndarray, np.ndarray]:
    """Disparity must be in pixels at the original left-image resolution."""
    intr = calibration.left
    if disparity.shape != (intr.height, intr.width):
        raise HarnessError("invalid_depth", "FFS returned an unexpected disparity shape")
    d = np.asarray(disparity, dtype=np.float32)
    effective = d - (intr.cx - calibration.right.cx)
    x = np.arange(intr.width)[None, :]
    valid = np.isfinite(d) & (d > 0) & (effective > 1e-6) & (x - d >= 0) & (x - d < intr.width)
    depth = np.full(d.shape, np.nan, dtype=np.float32)
    np.divide(intr.fx * calibration.baseline_m, effective, out=depth, where=valid)
    valid &= np.isfinite(depth) & (depth > 0) & (depth <= max_depth_m)
    depth[~valid] = np.nan
    return depth, valid


class FastFoundationStereo:
    """Serialized CUDA worker, using the official NVIDIA model's public inference API."""

    def __init__(self, config: FFSConfig):
        self.config = config
        self.process = None
        self.lock = asyncio.Lock()
        self.stderr_task = None
        self.stderr_tail = b""
        self.model_identity = None

    def readiness(self):
        for field in ("python", "repository", "checkpoint"):
            value = getattr(self.config, field)
            if not value or not Path(value).expanduser().exists():
                return {"ready": False, "reason": f"Configure existing ffs.{field}"}
        if not (Path(self.config.repository).expanduser() / "core/utils/utils.py").is_file():
            return {"ready": False, "reason": "FFS source does not contain InputPadder"}
        return {
            "ready": self.process is not None and self.process.returncode is None,
            "configured": True,
            "reason": "CUDA/model checked on first explicit inference",
        }

    async def _drain_stderr(self):
        while data := await self.process.stderr.read(4096):
            self.stderr_tail = (self.stderr_tail + data)[-16384:]

    async def _start(self):
        if self.process and self.process.returncode is None:
            return
        if self.process:
            await self.close()
        if not self.readiness().get("configured"):
            raise HarnessError("depth_unavailable", self.readiness()["reason"])
        self.process = await asyncio.create_subprocess_exec(
            str(Path(self.config.python).expanduser()),
            "-u",
            str(Path(__file__).with_name("ffs_worker.py")),
            "--repository",
            str(Path(self.config.repository).expanduser().resolve()),
            "--checkpoint",
            str(Path(self.config.checkpoint).expanduser().resolve()),
            "--valid-iters",
            str(self.config.valid_iters),
            "--max-disp",
            str(self.config.max_disp),
            "--scale",
            str(self.config.scale),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self.stderr_task = asyncio.create_task(self._drain_stderr())
        line = await self.process.stdout.readline()
        if not line:
            raise HarnessError(
                "depth_unavailable",
                "FFS worker exited before ready",
                details={"diagnostic": self.stderr_tail.decode(errors="replace")[-1500:]},
            )
        hello = json.loads(line)
        if not hello.get("ready"):
            raise HarnessError(
                "depth_unavailable", hello.get("error", "Worker failed to initialize")
            )
        self.model_identity = hello["model"]

    async def predict(self, left: bytes, right: bytes) -> tuple[np.ndarray, dict]:
        if self.lock.locked():
            raise HarnessError(
                "depth_busy", "One depth request is already running; no queued stale images"
            )
        async with self.lock:
            try:
                async with asyncio.timeout(self.config.timeout_s):
                    await self._start()
                    with tempfile.TemporaryDirectory(prefix="rsi-ffs-") as directory:
                        root = Path(directory)
                        (root / "left.png").write_bytes(left)
                        (root / "right.png").write_bytes(right)
                        request = {
                            "left": str(root / "left.png"),
                            "right": str(root / "right.png"),
                            "output": str(root / "disparity.npy"),
                        }
                        self.process.stdin.write((json.dumps(request) + "\n").encode())
                        await self.process.stdin.drain()
                        line = await self.process.stdout.readline()
                        result = json.loads(line) if line else {}
                        if not result.get("ok"):
                            raise HarnessError(
                                "depth_inference_failed", result.get("error", "Worker exited")
                            )
                        disparity = np.load(root / "disparity.npy", allow_pickle=False)
                        return disparity, {
                            "provider": "nvidia_fast_foundation_stereo",
                            "model": self.model_identity,
                            "settings": {
                                "scale": self.config.scale,
                                "valid_iters": self.config.valid_iters,
                                "max_disp": self.config.max_disp,
                            },
                            "inference_s": result["inference_s"],
                        }
            except BaseException as exc:
                await self.close()
                if isinstance(exc, TimeoutError):
                    raise HarnessError(
                        "depth_timeout", "FFS deadline exceeded; worker stopped"
                    ) from exc
                if isinstance(exc, (OSError, ValueError)):
                    raise HarnessError(
                        "depth_inference_failed", "Invalid response from FFS worker"
                    ) from exc
                raise

    async def close(self):
        if self.process:
            if self.process.returncode is None:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            await self.process.wait()
        if self.stderr_task:
            await self.stderr_task
        self.process = None
        self.stderr_task = None


class Depth:
    """Metric-depth service shared by explicitly selected providers."""

    def __init__(self, store: Store, provider):
        self.store, self.provider = store, provider
        self.capabilities = provider.capabilities

    async def estimate(self, observation_id: str, max_depth_m: float):
        from .cameras import load_camera

        observation = load_camera(self.store, observation_id)
        pinned = observation.model_copy(deep=True)
        measured = await self.provider.estimate(observation, max_depth_m)
        intr = measured.intrinsics
        raw = np.asarray(measured.depth_m)
        if raw.shape != (intr.height, intr.width) or raw.dtype.kind != "f":
            raise HarnessError(
                "invalid_depth", "Provider must return a floating metric array matching intrinsics"
            )
        streams = {frame.stream_id: frame for frame in pinned.frames}
        source = streams.get(measured.source_stream)
        if source is None or source.frame_id != measured.frame_id:
            raise HarnessError(
                "invalid_depth", "Depth must identify its input optical frame and pixel stream"
            )
        if source.intrinsics and source.intrinsics != intr:
            raise HarnessError(
                "invalid_depth", "Output intrinsics must match the source pixel stream"
            )
        if measured.capture_time != source.capture_time:
            raise HarnessError("invalid_depth", "Depth provider changed the acquisition clock")
        expected_calibration = None
        if pinned.stereo and measured.source_stream == pinned.stereo.left_stream:
            expected_calibration = pinned.stereo.calibration.version
        if expected_calibration is None or measured.calibration_version != expected_calibration:
            raise HarnessError(
                "invalid_depth", "Depth calibration identity does not match its input"
            )
        depth = raw.astype(np.float32, copy=True)
        valid = np.isfinite(depth) & (depth > 0) & (depth <= max_depth_m)
        depth[~valid] = np.nan
        identity = uid("depth")
        metadata = {
            "observation_id": observation_id,
            "camera_observation_id": observation.id,
            "frame_id": measured.frame_id,
            "source_stream": measured.source_stream,
            "calibration_version": measured.calibration_version,
            "unit": "metre",
        }
        stamp = measured.capture_time
        result = {
            "id": identity,
            **metadata,
            "depth_artifact": self.store.array(depth, metadata),
            "mask_artifact": self.store.array(
                valid, {**metadata, "meaning": "validity_not_confidence"}
            ),
            "intrinsics": intr.model_dump(),
            "valid_fraction": float(valid.mean()),
            "confidence": None,
            "source": observation.source,
            "qualification": measured.qualification,
            "capture_time": stamp.model_dump() if stamp else None,
            "captured_at": stamp.value_s
            if stamp and stamp.kind == "unix" and stamp.clock_domain == "utc"
            else None,
            "computed_at": time.time(),
            "provenance": measured.provenance,
        }
        self.store.put("depth", identity, result)
        self.store.event("depth.completed", result)
        return result

    def point(self, depth_id: str, u: int, v: int, radius: int, stream_id: str | None = None):
        result = self.store.get("depth", depth_id)
        intr = result["intrinsics"]
        if stream_id is not None and stream_id != result["source_stream"]:
            raise HarnessError(
                "pixel_stream_mismatch",
                "Pixel belongs to a different image/depth stream; align explicitly",
            )
        if not (0 <= u < intr["width"] and 0 <= v < intr["height"]) or not 0 <= radius <= 5:
            raise HarnessError(
                "pixel_outside_image", "Pixel or sampling radius is outside the contract"
            )
        array = self.store.load_array(result["depth_artifact"])
        patch = array[max(0, v - radius) : v + radius + 1, max(0, u - radius) : u + radius + 1]
        values = patch[np.isfinite(patch)]
        if len(values) == 0:
            raise HarnessError("invalid_depth", "No valid depth near requested pixel")
        z = float(np.median(values))
        return {
            "position_m": [(u - intr["cx"]) * z / intr["fx"], (v - intr["cy"]) * z / intr["fy"], z],
            "frame_id": result["frame_id"],
            "source_stream": result["source_stream"],
            "depth_id": depth_id,
            "valid_samples": len(values),
            "confidence": None,
            "note": "Source optical frame; not a robot-base target or grasp pose",
        }
