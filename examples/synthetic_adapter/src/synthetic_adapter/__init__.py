"""Independent synthetic adapter: no hardware SDK, no core device branches."""

import asyncio
import io
import math
import time

from PIL import Image
from pydantic import Field, model_validator

from robot_rsi.adapters import Adapter
from robot_rsi.cameras import save_camera
from robot_rsi.capabilities import Capability, SkillBindings
from robot_rsi.contracts import Contract, HarnessError, Intrinsics, VersionRef
from robot_rsi.device_validation import validate_state_age
from robot_rsi.perception import CameraObservation, CaptureTime, ImageFrame
from robot_rsi.resources import (
    CalibrationRecord,
    CommandFeedback,
    CommandReceipt,
    FKResult,
    IKResult,
    Joint,
    Resource,
    RobotModel,
    RobotState,
    Tool,
    Transform,
)
from robot_rsi.storage import uid

FACTORY_CALLS = 0


def caps(provider, names):
    return tuple(
        Capability(
            name=name,
            contract_version=1,
            provider_id=provider,
            implementation_version="fixture_v1",
            qualification="synthetic",
        )
        for name in names
    )


def model(version):
    return RobotModel(
        reference=VersionRef(id="fixture_model", version=version),
        resource_id="bench",
        joints=[
            Joint(id="carriage_x", kind="prismatic", lower=0, upper=1),
            Joint(id="carriage_y", kind="prismatic", lower=0, upper=1),
            Joint(id="turntable", kind="revolute", lower=-math.pi, upper=math.pi),
        ],
        manipulators={"bridge_3": ["carriage_x", "carriage_y", "turntable"]},
        tools=[
            Tool(
                id="vacuum",
                manipulator="bridge_3",
                commands=["pressure"],
                unit="Pa",
                lower=0,
                upper=120000,
            )
        ],
        frames=["world", "tool_tip"],
        parameters={"tip_height_m": {"v1": 0.25, "v2": 0.30}[version]},
        source="known Cartesian/yaw fixture; not general kinematics",
        qualification="synthetic",
    )


def transform(target, source, x=0, y=0, z=0, yaw=0):
    c, s = math.cos(yaw), math.sin(yaw)
    return Transform(
        target_frame=target,
        source_frame=source,
        matrix=((c, -s, 0, x), (s, c, 0, y), (0, 0, 1, z), (0, 0, 0, 1)),
    )


class Options(Contract):
    robot: bool = True
    camera: bool = True
    kinematics: bool = True
    shared_owner: bool = True
    delay_s: float = Field(default=0.01, ge=0, le=2)
    disconnect_after_submit: bool = False

    @model_validator(mode="after")
    def composition(self):
        if self.shared_owner and self.camera and not self.robot:
            raise ValueError("Independent camera must select shared_owner=false")
        return self


class Connection:
    def __init__(self):
        self.connected = False
        self.connect_count = self.close_count = 0

    async def connect(self):
        if not self.connected:
            self.connected = True
            self.connect_count += 1

    async def close(self):
        self.close_count += 1
        self.connected = False

    def require_connection(self):
        if not self.connected:
            raise HarnessError("resource_not_connected", "Explicitly connect this resource")


class Device(Connection):
    qualification = "synthetic"
    provider_id = "synthetic_fixture"
    supports_motion = True
    supports_hold = True
    arms = ("bridge_3",)
    frames = ("world", "tool_tip")

    def __init__(self, context, bound_model):
        super().__init__()
        self.context = context
        self.model = bound_model
        self.capabilities = caps(self.provider_id, ["robot.state", "robot.command"])
        self.joints = {"carriage_x": 0.2, "carriage_y": 0.1, "turntable": 0.0}
        self.tools = {"vacuum": 0.0}
        self.epoch = 0
        self.boot_id = uid("boot")
        self.commands = []
        self.pending = {}
        self.hold_count = 0

    def validate_arm(self, arm):
        if arm not in self.arms:
            raise HarnessError("unsupported_arm", "Unknown named manipulator")

    def validate_observation(self, observation):
        validate_state_age(
            observation, self.boot_id, self.epoch, self.context.profile.observation_max_age_s
        )
        if observation["resource_id"] != "bench":
            raise HarnessError("resource_mismatch", "State belongs to another resource")

    async def state(self):
        self.require_connection()
        return RobotState(
            id=uid("state"),
            resource_id="bench",
            model_ref=self.model.reference,
            joints=dict(self.joints),
            tools=dict(self.tools),
            capture_time=CaptureTime(
                value_s=time.time(), kind="unix", clock_domain="utc", uncertainty_s=0
            ),
            received_at=time.time(),
            boot_id=self.boot_id,
            epoch=self.epoch,
            source="synthetic_sensor_sample",
            qualification="synthetic",
        )

    async def hold(self):
        self.require_connection()
        self.epoch += 1
        self.hold_count += 1
        return {"confirmed": True, "epoch": self.epoch, "qualification": "synthetic"}

    async def submit(self, command, operation_id):
        self.require_connection()
        record = self.context.store.get("operation", operation_id)
        if record["status"] != "running" or "manipulator" not in record["resources"]:
            raise HarnessError("owner_required", "Commands require the original active operation")
        receipt = CommandReceipt(
            command_id=uid("command"),
            operation_id=operation_id,
            request_id=command.request_id,
            resource_id="bench",
            status="accepted",
            boot_id=self.boot_id,
            epoch=self.epoch,
            source="synthetic_sdk_ack",
            qualification="synthetic",
        )
        self.pending[receipt.command_id] = command
        self.commands.append(
            {
                "command_id": receipt.command_id,
                "operation_id": operation_id,
                "request_id": command.request_id,
            }
        )
        return receipt

    async def wait(self, receipt):
        command = self.pending.pop(receipt.command_id)
        await asyncio.sleep(self.context.options.delay_s)
        if command.trajectory:
            previous = 0
            for point in command.trajectory:
                await asyncio.sleep(point.time_from_start_s - previous)
                self.joints.update(point.positions)
                previous = point.time_from_start_s
        else:
            self.tools[command.effector.tool_id] = command.effector.value
        if self.context.options.disconnect_after_submit:
            raise ConnectionError("synthetic link lost after possible effects")
        return CommandFeedback(
            command_id=receipt.command_id,
            operation_id=receipt.operation_id,
            status="completed",
            state=await self.state(),
            source="synthetic_measured_feedback",
            qualification="synthetic",
        )


class Camera:
    def __init__(self, store, connection):
        self.store, self.connection = store, connection
        self.capabilities = caps("independent_camera", ["camera.describe", "camera.capture"])
        self.default_camera = "inspection"
        self.capture_count = 0

    def describe(self):
        return {
            "default_camera": "inspection",
            "cameras": [
                {"role": "inspection", "inputs": ["preview"], "availability": "not_checked"}
            ],
        }

    async def capture(self, camera=None, require="any", max_age_s=None):
        self.connection.require_connection()
        if camera not in (None, "inspection") or require not in ("any", "preview"):
            raise HarnessError(
                "unsupported_camera_input", "Fixture provides inspection preview only"
            )
        self.capture_count += 1
        buffer = io.BytesIO()
        Image.new("RGB", (16, 12), "blue").save(buffer, format="PNG")
        captured_at = time.time()
        artifact = self.store.artifact(
            buffer.getvalue(), "image/png", {"source": "synthetic_camera"}
        )
        observation = CameraObservation(
            id=uid("camera_obs"),
            camera="inspection",
            source="synthetic_camera",
            qualification="synthetic",
            received_at=time.time(),
            frames=[
                ImageFrame(
                    stream_id="rectified_preview",
                    artifact_id=artifact,
                    width=16,
                    height=12,
                    encoding="rgb8",
                    frame_id="fixture_optical",
                    capture_time=CaptureTime(
                        value_s=captured_at, kind="unix", clock_domain="utc", uncertainty_s=0
                    ),
                    intrinsics=Intrinsics(width=16, height=12, fx=10, fy=10, cx=8, cy=6),
                )
            ],
        )
        return save_camera(self.store, observation)


class Kinematics:
    def __init__(self, models):
        self.models = {m.reference.version: m for m in models}
        self.capabilities = caps("known_solution_fixture", ["kinematics.fk", "kinematics.ik"])
        self.fk_calls = self.ik_calls = 0

    async def fk(self, request):
        self.fk_calls += 1
        q = request.joints
        if (request.base_frame, request.target_frame) != ("world", "tool_tip"):
            raise HarnessError("unsupported_frame", "Fixture provides world <- tool_tip only")
        bound = self.models[request.model_ref.version]
        bound.validate_joints(request.arm, q)
        return FKResult(
            model_ref=request.model_ref,
            pose=transform(
                request.base_frame,
                request.target_frame,
                q["carriage_x"],
                q["carriage_y"],
                bound.parameters["tip_height_m"],
                q["turntable"],
            ),
            source="known Cartesian/yaw formula",
            qualification="synthetic",
        )

    async def ik(self, request):
        self.ik_calls += 1
        if (request.target.target_frame, request.target.source_frame) != ("world", "tool_tip"):
            raise HarnessError("unsupported_frame", "Fixture solves world <- tool_tip only")
        t = request.target.matrix
        q = {
            "carriage_x": t[0][3],
            "carriage_y": t[1][3],
            "turntable": math.atan2(t[1][0], t[0][0]),
        }
        z = self.models[request.model_ref.version].parameters["tip_height_m"]
        status = "solved"
        reason = None
        if not (0 <= q["carriage_x"] <= 1 and 0 <= q["carriage_y"] <= 1) or abs(t[2][3] - z) > 1e-8:
            status, reason = "no_solution", "Target is outside this known fixture solution family"
        elif any(not lo <= q[name] <= hi for name, (lo, hi) in request.joint_constraints.items()):
            status, reason = "no_solution", "Requested constraint excludes the known solution"
        elif abs(t[2][0]) + abs(t[2][1]) + abs(t[0][2]) + abs(t[1][2]) > 1e-8:
            status, reason = "not_converged", "Fixture does not solve out-of-plane rotation"
        return IKResult(
            model_ref=request.model_ref,
            status=status,
            joints=q if status == "solved" else None,
            translation_residual_m=0 if status == "solved" else None,
            rotation_residual_rad=0 if status == "solved" else None,
            residual_source="analytic fixture equality" if status == "solved" else None,
            reason=reason,
            qualification="synthetic",
        )


def factory(context):
    global FACTORY_CALLS
    FACTORY_CALLS += 1
    options = context.options
    models = (model("v1"), model("v2"))
    selected = context.profile.host.model_ref or models[0].reference
    bound = next((m for m in models if m.reference == selected), None)
    if bound is None:
        raise HarnessError("model_unavailable", "Select an installed fixture model version")
    device = Device(context, bound) if options.robot else None
    camera_owner = (
        device
        if options.shared_owner and options.camera
        else Connection()
        if options.camera
        else None
    )
    camera = Camera(context.store, camera_owner) if camera_owner else None
    kinematics = Kinematics(models) if options.kinematics else None
    lifecycles = {}
    resources = []
    if device:
        lifecycles["device"] = device
        resources.append(
            Resource(
                resource_id="bench",
                kind="robot",
                roles=["bridge_3"],
                provider="synthetic_fixture",
                implementation_version="1",
                capabilities=["robot.state", "robot.command"],
                qualification="synthetic",
                frames=list(device.frames),
                model_ref=selected,
                owner_id="device",
            )
        )
    if camera:
        shared = camera_owner is device
        if not shared:
            lifecycles["collector"] = camera_owner
        resources.append(
            Resource(
                resource_id="sensor",
                kind="camera",
                roles=["inspection"],
                provider="independent_camera",
                implementation_version="1",
                capabilities=["camera.describe", "camera.capture"],
                qualification="synthetic",
                frames=["fixture_optical"],
                owner_id="device" if shared else "collector",
            )
        )
    calibrations = tuple(
        CalibrationRecord(
            reference=VersionRef(id="fixture_extrinsics", version=version),
            kind="fixed_extrinsics",
            resource_id="sensor",
            model_ref=VersionRef(id="fixture_model", version=version),
            stream_id="rectified_preview",
            resolution=(16, 12),
            transform=transform("world", "fixture_optical", z=0.1 if version == "v1" else 0.2),
            method="known_fixture_transform",
            sample_source="fixed synthetic data; no calibration sampling",
            error={"fixture_residual_m": 0},
            qualification="synthetic",
        )
        for version in ["v1", "v2"]
    )
    calibrations += tuple(
        CalibrationRecord(
            reference=VersionRef(id="fixture_" + kind, version="v1"),
            kind=kind,
            resource_id=resource,
            model_ref=models[0].reference,
            stream_id="rectified_preview" if kind == "hand_eye" else None,
            resolution=(16, 12) if kind == "hand_eye" else None,
            transform=transform("tool_tip", source, z=0.01),
            method="known_fixture_transform",
            sample_source="fixed synthetic data; no sampling",
            qualification="synthetic",
        )
        for kind, resource, source in [
            ("hand_eye", "sensor", "fixture_optical"),
            ("tcp", "bench", "calibrated_tcp"),
        ]
    )
    return Adapter(
        SkillBindings(scene=None, robot=device, camera=camera, kinematics=kinematics),
        tuple(resources),
        device,
        lifecycles,
        models,
        calibrations,
    )


factory.config_model = Options


class FKParameters(Contract):
    arm: str = "bridge_3"
    joints: dict[str, float] = Field(
        default_factory=lambda: {"carriage_x": 0.2, "carriage_y": 0.1, "turntable": 0.0}
    )


async def check_fk(context, parameters):
    from robot_rsi.resources import FKRequest

    reference = context.resource_versions.get("model")
    if reference is None:
        raise HarnessError("model_not_bound", "Configure a model before starting this task")
    context.stage("compute_fk", None)
    result = await context.bindings.kinematics.fk(
        FKRequest(
            model_ref=reference,
            arm=parameters.arm,
            joints=parameters.joints,
            base_frame="world",
            target_frame="tool_tip",
        )
    )
    return result.model_dump()
