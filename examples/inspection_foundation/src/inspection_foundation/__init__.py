"""Synthetic sensor scenes over the existing offline robot contract example."""
import io
import time
from PIL import Image
from pydantic import Field
from robot_rsi.cameras import image_frame, save_camera
from robot_rsi.contracts import Contract
from robot_rsi.perception import CameraObservation
from robot_rsi.storage import uid
from synthetic_adapter import Camera, Options as BaseOptions, factory as base_factory


class Options(Contract):
    # Scene sensor truth is independent of commanded success/desired criteria.
    scenes: dict[str, str] = Field(default_factory=dict)
    transient_dark_frames: int = Field(default=0, ge=0, le=3)
    delay_s: float = Field(default=0.001, ge=0, le=2)
    disconnect_after_submit: bool = False


class SceneCamera(Camera):
    def __init__(self, store, connection, options):
        super().__init__(store, connection)
        self.options = options
        self.samples = []
        self.counts = {}

    async def capture(self, camera=None, require="any", max_age_s=None):
        self.connection.require_connection()
        if camera not in (None, "inspection") or require not in ("any", "preview"):
            from robot_rsi.contracts import HarnessError
            raise HarnessError("unsupported_camera_input", "Only inspection preview is available")
        x = round(self.connection.joints["carriage_x"], 4)
        key = str(x)
        self.counts[key] = self.counts.get(key, 0) + 1
        label = self.options.scenes.get(key, "normal")
        if self.counts[key] <= self.options.transient_dark_frames:
            label = "unreadable"
        color = {"normal": "green", "anomaly": "red", "unreadable": "black"}[label]
        data = io.BytesIO()
        Image.new("RGB", (16, 12), color).save(data, format="PNG")
        self.capture_count += 1
        self.samples.append({"x": x, "sample": self.counts[key], "label": label})
        return save_camera(self.store, CameraObservation(
            id=uid("camera_obs"), camera="inspection", source="synthetic_position_scene_v1",
            qualification="synthetic", received_at=time.time(),
            frames=[image_frame(self.store, data.getvalue(), "preview")]))


def factory(context):
    from dataclasses import replace
    base = replace(context, options=BaseOptions(delay_s=context.options.delay_s,
        disconnect_after_submit=context.options.disconnect_after_submit))
    adapter = base_factory(base)
    camera = SceneCamera(context.store, adapter.owner, context.options)
    from robot_rsi.skill_adapters import RobotScene
    async def observe():
        state = (await adapter.owner.state()).model_dump()
        context.store.put("observation", state["id"], state)
        return state
    adapter.bindings = replace(adapter.bindings, camera=camera, scene=RobotScene(adapter.owner, observe))
    return adapter


factory.config_model = Options
