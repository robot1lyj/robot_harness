"""Pure application tool parameters; no Store, feature implementation or client imports."""
from typing import Literal
from pydantic import Field, model_validator
from .contracts import Contract, Identifier, TaskEvidence, VersionRef, Finite
Role = Literal["camera", "kinematics"]
Conditions = dict[Identifier, str | int | Finite | bool]

class IssueCreate(Contract):
    route: Literal["local_input", "local_extension", "framework_contract", "framework_core"]
    rationale: str = Field(min_length=1, max_length=1200)
    component: str = Field(min_length=1, max_length=120)
    expected: str = Field(min_length=1, max_length=1200)
    actual: str = Field(min_length=1, max_length=1200)
    reproducer: str = Field(min_length=1, max_length=2400)
    source_task: Identifier | None = None
    operation_ids: list[Identifier] = Field(default_factory=list, max_length=8)
    evidence_refs: list[TaskEvidence] = Field(default_factory=list, max_length=8)

class IssueGet(Contract):
    issue_id: Identifier | None = None
    kind: Literal["all", "updates", "pending"] = "all"
    limit: int = Field(default=3, ge=1, le=3)
    cursor: str | None = Field(default=None, pattern=r"^\d+:\d+$", max_length=50)

    @model_validator(mode="after")
    def one_lookup(self):
        if self.issue_id is not None and (self.cursor or self.kind != "all"):
            raise ValueError("Use issue_id for details or kind/cursor for a listing")
        return self

class HarnessRead(Contract):
    guide: Literal["integration", "interfaces"] = "integration"
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=6000, ge=1, le=16000)

class ProjectRead(Contract):
    key: Identifier | None = None
    query: str = Field(default="", max_length=200)
    limit: int = Field(default=3, ge=1, le=3)

class ProjectFiles(Contract):
    path: str = Field(default=".", min_length=1, max_length=240)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=6000, ge=1, le=16000)

class CandidateInfo(Contract):
    reference: VersionRef
    description: str = Field(min_length=1, max_length=500)
    distribution: str = Field(min_length=1, max_length=120)
    resource_ids: list[Identifier] = Field(default_factory=list, max_length=8)
    model_refs: list[VersionRef] = Field(default_factory=list, max_length=8)
    calibration_refs: list[VersionRef] = Field(default_factory=list, max_length=8)
    conditions: Conditions = Field(default_factory=dict, max_length=12)

class CandidateQuery(Contract):
    requirements: dict[str, int] = Field(min_length=1, max_length=8)
    query: str = Field(default="", max_length=200)
    limit: int = Field(default=3, ge=1, le=3)

class BindingSelection(Contract):
    scene_id: Identifier
    purpose: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=500)
    roles: dict[Role, Identifier] = Field(min_length=1, max_length=2)
    requirements: dict[str, int] = Field(min_length=1, max_length=8)
    model_ref: VersionRef | None = None
    calibration_refs: list[VersionRef] = Field(default_factory=list, max_length=8)
    conditions: Conditions = Field(default_factory=dict, max_length=12)
    skills: list[Identifier] = Field(default_factory=list, max_length=8)

class BindingRead(Contract):
    scene_id: Identifier | None = None
    reference: VersionRef | None = None
    conditions: Conditions | None = None

class BindingTrial(Contract):
    role: Role
    tool: Literal["camera_capture", "kinematics_fk", "kinematics_ik"]
    arguments: dict = Field(default_factory=dict)

class BindingValidate(Contract):
    selection: BindingSelection
    trials: list[BindingTrial] = Field(default_factory=list, max_length=8)
    previous: VersionRef | None = None

class BindingSave(Contract):
    reference: VersionRef
    validation_id: Identifier
    attach_to_task: bool = False
