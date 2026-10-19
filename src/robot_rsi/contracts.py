from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    model_validator,
)

Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")]
Finite = Annotated[float, Field(allow_inf_nan=False)]
Point = tuple[Finite, Finite, Finite]


def http_endpoint(value: str) -> str:
    return str(TypeAdapter(AnyHttpUrl).validate_python(value))


HttpEndpoint = Annotated[str, AfterValidator(http_endpoint)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class HarnessError(Exception):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or {}

    def payload(self):
        return {"code": self.code, "message": self.message, "details": self.details}


class FFSConfig(Contract):
    python: str | None = None
    repository: str | None = None
    checkpoint: str | None = None
    valid_iters: int = Field(default=8, ge=1, le=32)
    max_disp: int = Field(default=192, ge=32, le=512)
    scale: float = Field(default=1, ge=0.25, le=1, allow_inf_nan=False)
    timeout_s: float = Field(default=120, gt=0, le=600, allow_inf_nan=False)


class Intrinsics(Contract):
    width: int = Field(gt=0, le=8192)
    height: int = Field(gt=0, le=8192)
    fx: float = Field(gt=0, allow_inf_nan=False)
    fy: float = Field(gt=0, allow_inf_nan=False)
    cx: Finite
    cy: Finite


class StereoCalibration(Contract):
    version: Identifier
    frame_id: Identifier
    left: Intrinsics
    right: Intrinsics
    baseline_m: float = Field(gt=0, le=2, allow_inf_nan=False)
    rectified: bool
    source: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_pair(self):
        if not self.rectified:
            raise ValueError("FFS requires rectified, undistorted stereo images")
        if (self.left.width, self.left.height) != (self.right.width, self.right.height):
            raise ValueError("Stereo image sizes must match")
        for field in ("fx", "fy", "cy"):
            if abs(getattr(self.left, field) - getattr(self.right, field)) > 1e-3:
                raise ValueError(f"Rectified pair must share {field}; rectify before import")
        return self


class CameraConfig(Contract):
    """Only role mapping and capture budget; the project's device owner supplies frames."""

    roles: dict[Identifier, Identifier] = Field(
        default_factory=lambda: {"top": "top", "left": "left", "right": "right"}, min_length=1
    )
    default_camera: Identifier = "top"
    capture_timeout_s: float = Field(default=5, gt=0, le=120, allow_inf_nan=False)

    @model_validator(mode="after")
    def default_is_bound(self):
        if self.default_camera not in self.roles:
            raise ValueError("default_camera must name a configured semantic role")
        return self


class VersionRef(Contract):
    id: Identifier
    version: Identifier


class AdapterConfig(Contract):
    factory: Identifier
    options: dict = Field(default_factory=dict)


class HostConfig(Contract):
    schema_version: Literal[1] = 1
    adapter: AdapterConfig | None = None
    cameras: CameraConfig | None = Field(default_factory=CameraConfig)
    depth: FFSConfig | None = Field(default_factory=FFSConfig)
    model_ref: VersionRef | None = None
    calibration_refs: list[VersionRef] = Field(default_factory=list)


class WorkspaceConfig(Contract):
    title: str = Field(default="", max_length=120)
    purpose: str = Field(default="", max_length=500)
    constraints: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list, max_length=12
    )


ProjectPurpose = Literal["onboarding", "run", "improve", "evaluate"]


class ProjectMaterial(Contract):
    key: Identifier
    title: str = Field(min_length=1, max_length=160)
    path: str = Field(min_length=1, max_length=240)
    purposes: list[ProjectPurpose] = Field(min_length=1, max_length=4)


class ProjectConfig(Contract):
    id: Identifier
    root: str
    purpose: ProjectPurpose = "run"
    common_key: Identifier
    materials: list[ProjectMaterial] = Field(max_length=1000)


class TaskLimits(Contract):
    tool_calls: int = Field(default=40, ge=1, le=1000)
    duration_s: float = Field(default=180, gt=0, le=3600, allow_inf_nan=False)
    recoveries: int = Field(default=1, ge=0, le=10)
    context_bytes: int = Field(default=20000, ge=1000, le=100000)

    @model_validator(mode="before")
    @classmethod
    def ignore_legacy_tokens(cls, value):
        if isinstance(value, dict):
            return {key: item for key, item in value.items() if key != "model_tokens"}
        return value


class MemoryMaintenance(Contract):
    interval_s: float = Field(default=30, gt=0, allow_inf_nan=False)
    max_interval_s: float = Field(default=300, gt=0, allow_inf_nan=False)
    batch_changes: int = Field(default=4, ge=1)
    duplicate_checks: int = Field(default=4, ge=1)

    @model_validator(mode="after")
    def valid_interval(self):
        if self.max_interval_s < self.interval_s:
            raise ValueError("max_interval_s must be at least interval_s")
        return self


class AgentRole(Contract):
    model: str | None = None
    reasoning_effort: str | None = None


class AgentConfig(Contract):
    client: Literal["codex"] = "codex"
    fast: AgentRole | None = Field(default_factory=AgentRole)
    teacher: AgentRole | None = None
    operator: Literal["manual"] = "manual"


class Profile(Contract):
    role: Literal["development", "runtime", "evaluation"] = "runtime"
    station: Identifier = "yam-lab"
    namespace: Identifier = "mock-demo"
    state_root: str = "~/.local/state/robot-rsi"
    robot: Identifier | None = "mock"
    yam_url: HttpEndpoint = "http://127.0.0.1:8766"
    yam_allow_hold: bool = False
    observation_max_age_s: float = Field(default=3, gt=0, le=30, allow_inf_nan=False)
    mock_step_s: float = Field(default=0.03, ge=0, le=5, allow_inf_nan=False)
    mock_grasp_failures: int = Field(default=0, ge=0, le=10)
    host: HostConfig = Field(default_factory=HostConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    project: ProjectConfig | None = None
    task_limits: TaskLimits = Field(default_factory=TaskLimits)
    active_task_id: Identifier | None = None
    memory_maintenance: MemoryMaintenance = Field(default_factory=MemoryMaintenance)
    agent: AgentConfig = Field(default_factory=AgentConfig)


class Empty(Contract):
    pass


class Describe(Contract):
    name: Identifier


class SkillQuery(Contract):
    query: str = Field(default="", max_length=200)
    available_only: bool = True
    limit: int = Field(default=3, ge=1, le=10)
    offset: int = Field(default=0, ge=0)


class WorkspaceRead(Contract):
    query: str | None = Field(default=None, max_length=200)
    key: Identifier | None = None
    limit: int = Field(default=3, ge=1, le=10)
    offset: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def one_lookup(self):
        if self.key is not None and self.query is not None:
            raise ValueError("Use query for note cards or key for one full note")
        return self


class WorkspaceNote(Contract):
    key: Identifier
    title: str | None = Field(default=None, min_length=1, max_length=160)
    text: str | None = Field(default=None, min_length=1, max_length=2400)
    source: str | None = Field(default=None, min_length=1, max_length=240)
    archive: bool = False

    @model_validator(mode="after")
    def complete_note(self):
        if not self.archive and not all(
            value and value.strip() for value in (self.title, self.text, self.source)
        ):
            raise ValueError("Saving a note requires title, text and source")
        return self


class TaskBudget(Contract):
    # Null means unmeasured, rather than an invented zero or unlimited allowance.
    tool_calls: int | None = Field(ge=0)
    time_s: float | None = Field(ge=0, allow_inf_nan=False)
    model_tokens: int | None = Field(ge=0)


class TaskEvidence(Contract):
    kind: Literal[
        "observation", "camera_observation", "depth", "artifact", "memory", "workspace_note"
    ]
    id: Identifier


class TaskCheckpointSave(Contract):
    task_id: Identifier
    goal: str = Field(min_length=1, max_length=500)
    constraints: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(max_length=12)
    skill_versions: dict[Identifier, Annotated[str, Field(min_length=1, max_length=80)]] = Field(
        max_length=8
    )
    last_confirmed: str = Field(min_length=1, max_length=1000)
    operation_ids: list[Identifier] = Field(max_length=16)
    unknowns: list[Annotated[str, Field(min_length=1, max_length=240)]] = Field(max_length=12)
    remaining_budget: TaskBudget
    evidence_refs: list[TaskEvidence] = Field(max_length=16)


class TaskIdentity(Contract):
    task_id: Identifier


class TaskFinish(TaskIdentity):
    task_epoch: int = Field(ge=0)
    outcome: Literal["succeeded", "failed", "cancelled", "unknown"]
    summary: str = Field(
        min_length=1,
        max_length=1000,
        description="Conclusion for the entire current goal, not one step",
    )
    operation_ids: list[Identifier] = Field(default_factory=list, max_length=16)
    evidence_refs: list[TaskEvidence] = Field(default_factory=list, max_length=16)


class TaskCheckpointGet(TaskIdentity):
    include_archived: bool = False
    archive_id: Identifier | None = None


class TaskBegin(TaskIdentity):
    goal: str = Field(min_length=1, max_length=500)
    recipe: Literal["pick_place_v1"] | None = None
    binding_ref: VersionRef | None = None
    binding_conditions: dict[str, str | int | float | bool] = Field(default_factory=dict)
    constraints: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list, max_length=12
    )


class TaskContext(TaskIdentity):
    query: str | None = Field(default=None, max_length=200)
    selected_skill: Identifier | None = None
    blocks: bool = False
    delivered: dict[str, str] | None = None


class TaskRecover(TaskIdentity):
    operation_id: Identifier
    action: Literal["retry_pick_place"]
    task_epoch: int = Field(ge=0)
    request_id: Identifier


class AssistanceClaim(TaskIdentity):
    request_id: Identifier
    task_epoch: int = Field(ge=0)
    operator: Identifier


class AssistanceReturn(AssistanceClaim):
    disposition: Literal["resume", "handoff"]
    note: str = Field(min_length=1, max_length=500)


class Observe(Contract):
    camera: Identifier | None = None


class CameraCapture(Observe):
    require: Literal["any", "preview", "stereo"] = "any"
    max_age_s: float | None = Field(default=None, gt=0, le=60, allow_inf_nan=False)


class Motion(Contract):
    request_id: Identifier
    observation_id: Identifier
    arm: Identifier = "left"
    timeout_s: float = Field(default=20, gt=0, le=120, allow_inf_nan=False)


class Move(Motion):
    position_m: Point
    frame_id: Identifier = "mock_table"


class Gripper(Motion):
    opening: float = Field(ge=0, le=1, allow_inf_nan=False)


class PickPlace(Motion):
    object_id: Identifier = "red_block"
    target_region: Identifier = "blue_box"
    grasp_retries: int = Field(default=1, ge=0, le=1)


class TaskRun(Contract):
    request_id: Identifier
    task_id: Identifier | None = None
    task_epoch: int | None = Field(default=None, ge=0)
    skill: Identifier = "pick_place"
    parameters: dict = Field(default_factory=dict)
    after_operation_id: Identifier | None = Field(
        default=None,
        description="For a new step, reference the last successful operation; do not repeat that step",
    )
    timeout_s: float = Field(default=20, gt=0, le=120, allow_inf_nan=False)


class OperationQuery(Contract):
    operation_id: Identifier
    wait_s: float = Field(default=0, ge=0, le=30, allow_inf_nan=False)


class OperationCancel(Contract):
    operation_id: Identifier


class ArtifactQuery(Contract):
    artifact_id: Identifier


class DepthEstimate(Contract):
    observation_id: Identifier
    max_depth_m: float = Field(default=5, gt=0, le=100, allow_inf_nan=False)


class DepthPoint(Contract):
    depth_id: Identifier
    stream_id: Identifier
    u: int = Field(ge=0)
    v: int = Field(ge=0)
    radius: int = Field(default=1, ge=0, le=5)


class MemoryRecall(Contract):
    query: str = Field(default="", max_length=200)
    include_candidates: bool = False
    limit: int = Field(default=5, ge=1, le=10)


class MemoryRemember(Contract):
    claim: str = Field(min_length=1, max_length=1600)
    operation_id: Identifier
    tags: list[Identifier] = Field(default_factory=list, max_length=8)
