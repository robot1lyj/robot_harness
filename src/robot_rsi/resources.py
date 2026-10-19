"""Public resource, command, kinematics and calibration data; SI units and named resources."""

from __future__ import annotations

from typing import Literal, Protocol

import numpy as np
from pydantic import Field, model_validator

from .capabilities import Capability
from .contracts import (
    Contract,
    Finite,
    HarnessError,
    Identifier,
    Intrinsics,
    Motion,
    StereoCalibration,
    VersionRef,
)
from .perception import CaptureTime

Matrix4 = tuple[
    tuple[Finite, Finite, Finite, Finite],
    tuple[Finite, Finite, Finite, Finite],
    tuple[Finite, Finite, Finite, Finite],
    tuple[Finite, Finite, Finite, Finite],
]


class Transform(Contract):
    """T_target_source: p_target = matrix @ p_source; translations in metres."""

    target_frame: Identifier
    source_frame: Identifier
    matrix: Matrix4

    @model_validator(mode="after")
    def rigid(self):
        m = np.asarray(self.matrix)
        r = m[:3, :3]
        if not (
            np.allclose(m[3], [0, 0, 0, 1], atol=1e-8)
            and np.allclose(r.T @ r, np.eye(3), atol=1e-6)
            and abs(np.linalg.det(r) - 1) < 1e-6
        ):
            raise ValueError("Transform must be a rigid T_target_source in metres")
        return self


class Joint(Contract):
    id: Identifier
    kind: Literal["revolute", "prismatic"]
    lower: Finite
    upper: Finite
    tolerance: float = Field(default=1e-6, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def limits(self):
        if self.lower >= self.upper:
            raise ValueError("Joint lower must be below upper")
        return self


class Tool(Contract):
    id: Identifier
    manipulator: Identifier | None = None
    commands: list[Identifier]
    unit: str = Field(min_length=1, max_length=40)
    lower: Finite
    upper: Finite
    tolerance: float = Field(default=1e-6, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def limits(self):
        if self.lower >= self.upper or not self.commands:
            raise ValueError("Tool requires commands and a nonempty range")
        return self


class RobotModel(Contract):
    reference: VersionRef
    resource_id: Identifier
    joints: list[Joint]
    manipulators: dict[Identifier, list[Identifier]]
    tools: list[Tool] = Field(default_factory=list)
    frames: list[Identifier]
    parameters: dict[str, Finite] = Field(default_factory=dict)
    artifact_id: Identifier | None = None
    source: str
    qualification: str

    @model_validator(mode="after")
    def names(self):
        names = [j.id for j in self.joints]
        if len(set(names)) != len(names) or len({t.id for t in self.tools}) != len(self.tools):
            raise ValueError("Duplicate joint/tool names")
        if any(
            not ids or len(set(ids)) != len(ids) or not set(ids) <= set(names)
            for ids in self.manipulators.values()
        ):
            raise ValueError("Manipulator must name model joints in explicit order")
        return self

    def validate_joints(self, group, positions):
        if group not in self.manipulators or set(positions) != set(self.manipulators[group]):
            raise HarnessError(
                "joint_mapping", "Provide exactly the named joints for this manipulator"
            )
        limits = {j.id: j for j in self.joints}
        if any(
            not limits[name].lower <= value <= limits[name].upper
            for name, value in positions.items()
        ):
            raise HarnessError("joint_limit", "Joint position violates the selected model limits")


class Resource(Contract):
    resource_id: Identifier
    kind: Identifier
    roles: list[Identifier]
    provider: Identifier
    implementation_version: str
    capabilities: list[str]
    qualification: str
    frames: list[Identifier] = Field(default_factory=list)
    model_ref: VersionRef | None = None
    owner_id: Identifier | None = None


class RobotState(Contract):
    id: Identifier
    resource_id: Identifier
    model_ref: VersionRef
    joints: dict[Identifier, Finite]
    tools: dict[Identifier, Finite] = Field(default_factory=dict)
    capture_time: CaptureTime
    received_at: Finite
    boot_id: Identifier
    epoch: int = Field(ge=0)
    source: str
    qualification: str


class Waypoint(Contract):
    time_from_start_s: float = Field(gt=0, allow_inf_nan=False)
    positions: dict[Identifier, Finite]


class EffectorCommand(Contract):
    tool_id: Identifier
    command: Identifier
    value: Finite
    unit: str


class RobotCommand(Motion):
    resource_id: Identifier
    model_ref: VersionRef
    arm: Identifier
    trajectory: list[Waypoint] = Field(default_factory=list, max_length=1000)
    effector: EffectorCommand | None = None

    @model_validator(mode="after")
    def one_command(self):
        if bool(self.trajectory) == bool(self.effector):
            raise ValueError("Provide exactly one trajectory or effector command")
        times = [p.time_from_start_s for p in self.trajectory]
        if times != sorted(set(times)):
            raise ValueError("Trajectory times must strictly increase")
        return self


class CommandReceipt(Contract):
    command_id: Identifier
    operation_id: Identifier
    request_id: Identifier
    resource_id: Identifier
    status: Literal["accepted", "running"]
    boot_id: Identifier
    epoch: int = Field(ge=0)
    source: str
    qualification: str


class CommandFeedback(Contract):
    command_id: Identifier
    operation_id: Identifier
    status: Literal["completed", "failed", "unknown"]
    state: RobotState | None = None
    reason: str | None = None
    source: str
    qualification: str

    @model_validator(mode="after")
    def completion_requires_measurement(self):
        if self.status == "completed" and self.state is None:
            raise ValueError(
                "Completed command requires sampled state, not submission acknowledgement"
            )
        return self


class ResourceQuery(Contract):
    resource_id: Identifier


class ModelQuery(Contract):
    reference: VersionRef


class FKRequest(Contract):
    model_ref: VersionRef
    arm: Identifier
    joints: dict[Identifier, Finite]
    base_frame: Identifier
    target_frame: Identifier


class FKResult(Contract):
    model_ref: VersionRef
    pose: Transform
    source: str
    qualification: str


class IKRequest(Contract):
    model_ref: VersionRef
    arm: Identifier
    target: Transform
    seed: dict[Identifier, Finite]
    joint_constraints: dict[Identifier, tuple[Finite, Finite]] = Field(default_factory=dict)


class IKResult(Contract):
    model_ref: VersionRef
    status: Literal["solved", "no_solution", "not_converged"]
    joints: dict[Identifier, Finite] | None = None
    translation_residual_m: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    rotation_residual_rad: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    residual_source: str | None = None
    reason: str | None = None
    qualification: str

    @model_validator(mode="after")
    def outcome(self):
        if (self.status == "solved") != (self.joints is not None):
            raise ValueError("Only solved IK may return joint positions")
        if (
            self.translation_residual_m is not None or self.rotation_residual_rad is not None
        ) and not self.residual_source:
            raise ValueError("Measured residuals require a source")
        return self


class CalibrationRecord(Contract):
    reference: VersionRef
    kind: Literal["intrinsics", "stereo", "fixed_extrinsics", "hand_eye", "tcp"]
    resource_id: Identifier
    model_ref: VersionRef | None = None
    stream_id: Identifier | None = None
    resolution: tuple[int, int] | None = None
    target_frame: Identifier | None = None
    source_frame: Identifier | None = None
    intrinsics: Intrinsics | None = None
    stereo: StereoCalibration | None = None
    transform: Transform | None = None
    artifact_id: Identifier | None = None
    method: str
    sample_source: str
    error: dict[str, Finite] = Field(default_factory=dict)
    qualification: str

    @model_validator(mode="after")
    def parameters(self):
        expected = (
            "intrinsics"
            if self.kind == "intrinsics"
            else "stereo"
            if self.kind == "stereo"
            else "transform"
        )
        if getattr(self, expected) is None and self.artifact_id is None:
            raise ValueError("Calibration requires its typed parameters or an artifact")
        if self.resolution and any(x <= 0 for x in self.resolution):
            raise ValueError("Resolution must be positive")
        intr = self.intrinsics or (self.stereo.left if self.stereo else None)
        if intr and self.resolution and self.resolution != (intr.width, intr.height):
            raise ValueError("Applicability resolution differs from calibration pixels")
        if (
            expected == "transform"
            and self.transform is None
            and not (self.target_frame and self.source_frame)
        ):
            raise ValueError("Transform artifact needs explicit applicability frames")
        if self.transform and any(
            getattr(self, field) not in (None, getattr(self.transform, field))
            for field in ("source_frame", "target_frame")
        ):
            raise ValueError("Applicability frames differ from the transform")
        return self


class CalibrationQuery(Contract):
    reference: VersionRef
    resource_id: Identifier | None = None
    model_ref: VersionRef | None = None
    stream_id: Identifier | None = None
    resolution: tuple[int, int] | None = None
    target_frame: Identifier | None = None
    source_frame: Identifier | None = None


class Lifecycle(Protocol):
    async def connect(self) -> None: ...
    async def close(self) -> None: ...


class RobotPort(Lifecycle, Protocol):
    capabilities: tuple[Capability, ...]
    model: RobotModel

    async def state(self) -> RobotState: ...
    async def hold(self) -> dict: ...
    async def submit(self, command: RobotCommand, operation_id: str) -> CommandReceipt: ...
    async def wait(self, receipt: CommandReceipt) -> CommandFeedback: ...


class KinematicsPort(Protocol):
    capabilities: tuple[Capability, ...]

    async def fk(self, request: FKRequest) -> FKResult: ...
    async def ik(self, request: IKRequest) -> IKResult: ...
