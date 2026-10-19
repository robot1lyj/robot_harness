"""Device-independent contracts; no robot, transport or model implementation imports."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Literal, Protocol

from pydantic import ConfigDict, Field

from .contracts import Contract, HarnessError, Identifier

if TYPE_CHECKING:
    from .perception import CameraPort, DepthPort
    from .resources import KinematicsPort, RobotPort

PICK_PLACE_REQUIREMENTS = {
    "scene.observe": 1,
    "manipulation.plan_pick_place": 1,
    "manipulation.execute_stage": 1,
    "outcome.verify": 1,
}
Stage = Literal["approach", "grasp", "lift", "transport", "place", "release", "withdraw"]
Criterion = Literal["grasp", "placement"]


class Capability(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    contract_version: int = Field(ge=1)
    provider_id: Identifier
    implementation_version: str
    qualification: str
    configuration_id: str | None = None


class ObservationRef(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: Identifier
    qualification: str


class PickPlaceGoal(Contract):
    arm: Identifier
    object_id: Identifier
    target_region: Identifier


class PlanRef(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: Identifier
    provider_id: Identifier
    observation_id: Identifier
    frame_id: Identifier
    qualification: str


class Verification(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True)
    outcome: Literal["passed", "failed", "unknown"]
    observation_id: Identifier
    method: str
    qualification: str
    retryable: bool = False
    reason: str | None = None


class Scene(Protocol):
    capabilities: tuple[Capability, ...]

    async def observe(self) -> ObservationRef: ...


class Manipulation(Protocol):
    capabilities: tuple[Capability, ...]

    def validate_goal(self, goal: PickPlaceGoal) -> None: ...

    async def plan(self, goal: PickPlaceGoal, observation: ObservationRef) -> PlanRef: ...

    async def execute(self, plan: PlanRef, stage: Stage) -> None: ...


class OutcomeVerifier(Protocol):
    capabilities: tuple[Capability, ...]

    async def verify(
        self, plan: PlanRef, criterion: Criterion, observation: ObservationRef
    ) -> Verification: ...


class ExecutionOwner(Protocol):
    qualification: str
    supports_motion: bool

    def validate_arm(self, arm: str) -> None: ...

    def validate_observation(self, observation: dict) -> None: ...

    async def hold(self) -> dict: ...


@dataclass(frozen=True)
class SkillBindings:
    """Host-selected ports. Missing or incompatible providers fail before execution."""

    scene: Scene | None
    manipulation: Manipulation | None = None
    verification: OutcomeVerifier | None = None
    camera: CameraPort | None = None
    depth: DepthPort | None = None
    robot: RobotPort | None = None
    kinematics: KinematicsPort | None = None
    calibration: object | None = None

    def catalog(self) -> list[dict]:
        entries = {}
        for port in (self.scene, self.manipulation, self.verification, self.camera, self.depth,
                     self.robot, self.kinematics, self.calibration):
            if port is None:
                continue
            for capability in port.capabilities:
                value = capability.model_dump()
                if capability.name in entries and entries[capability.name] != value:
                    raise HarnessError("ambiguous_binding", "Conflicting capability providers")
                entries[capability.name] = value
        return [entries[key] for key in sorted(entries)]

    def missing(self, requirements: dict[str, int]) -> list[dict]:
        # Check each port, not merely a union of self-reported capabilities.
        routes = {
            "scene.observe": (self.scene, ("observe",)),
            "manipulation.plan_pick_place": (self.manipulation, ("validate_goal", "plan")),
            "manipulation.execute_stage": (self.manipulation, ("execute",)),
            "outcome.verify": (self.verification, ("verify",)),
            "camera.describe": (self.camera, ("describe",)),
            "camera.capture": (self.camera, ("capture",)),
            "depth.estimate": (self.depth, ("estimate",)),
            "robot.state": (self.robot, ("state",)),
            "robot.command": (self.robot, ("submit", "wait")),
            "kinematics.fk": (self.kinematics, ("fk",)),
            "kinematics.ik": (self.kinematics, ("ik",)),
            "calibration.read": (self.calibration, ("get",)),
            "model.read": (self.calibration, ("model",)),
        }
        missing = []
        for name, version in requirements.items():
            port, methods = routes.get(name, (None, ()))
            actual = (
                next((c.contract_version for c in port.capabilities if c.name == name), None)
                if port is not None
                else None
            )
            absent = [method for method in methods if not callable(getattr(port, method, None))]
            if actual != version or absent:
                missing.append(
                    {
                        "name": name,
                        "required_version": version,
                        "provided_version": actual,
                        **({"missing_methods": absent} if absent and port is not None else {}),
                    }
                )
        return missing

    def require(self, requirements: dict[str, int]):
        self.catalog()  # ambiguity is also rejected for an executable binding
        if missing := self.missing(requirements):
            raise HarnessError(
                "unsupported",
                "Required capabilities are missing or incompatible",
                details={"missing_capabilities": missing},
            )


@dataclass(frozen=True)
class SkillContext:
    bindings: SkillBindings
    stage: Callable[[str, dict | None], None]
    resource_versions: dict = field(default_factory=dict)
