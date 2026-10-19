"""Shared static tool contracts; Runtime remains the sole dispatcher."""

from .contracts import (
    ArtifactQuery,
    AssistanceClaim,
    AssistanceReturn,
    CameraCapture,
    DepthEstimate,
    DepthPoint,
    Describe,
    Empty,
    Gripper,
    MemoryRecall,
    MemoryRemember,
    Move,
    Observe,
    OperationCancel,
    OperationQuery,
    PickPlace,
    SkillQuery,
    TaskBegin,
    TaskCheckpointGet,
    TaskCheckpointSave,
    TaskContext,
    TaskFinish,
    TaskIdentity,
    TaskRecover,
    TaskRun,
    WorkspaceNote,
    WorkspaceRead,
)
from .feedback import IssueCreate, IssueGet
from .project import ProjectFiles, ProjectRead
from .resources import (
    CalibrationQuery,
    FKRequest,
    IKRequest,
    ModelQuery,
    ResourceQuery,
    RobotCommand,
)
from .scene_bindings import BindingRead, BindingSave, BindingValidate, CandidateQuery

TOOLS = {
    "project_files": (ProjectFiles, "List one selected project directory or read one UTF-8 source; use next_offset to continue; onboarding/improve only"),
    "project_read": (ProjectRead, "Read purpose-selected project material cards or one body; host fixes purpose"),
    "issue_create": (IssueCreate, "Save a reasoned local/framework issue with selected evidence; no automatic diagnosis"),
    "issue_get": (IssueGet, "Read at most 3 issue/update states or one linked issue and replies"),
    "binding_candidates": (CandidateQuery, "Find at most 3 relevant installed read/compute candidates; no connection"),
    "binding_read": (BindingRead, "Read saved scene binding and only selected dependency changes; no trial"),
    "binding_validate": (BindingValidate, "Check a proposed selection and explicitly try read/compute ports; host evidence"),
    "binding_save": (BindingSave, "Save the exact host-validated selection as an immutable version; optional first task attachment"),
    "model_get": (
        ModelQuery,
        "Read one immutable model definition; task-bound requests keep their selected version",
    ),
    "resources_list": (Empty, "Describe configured named resources without connecting"),
    "resource_connect": (ResourceQuery, "Explicitly connect a configured resource owner"),
    "robot_state": (ResourceQuery, "Read sampled named joint/tool state after explicit connection"),
    "robot_command": (
        RobotCommand,
        "Submit a named device command through Operations; receipt is not measured completion or goal success",
    ),
    "kinematics_fk": (FKRequest, "Compute FK for the selected model; no device access"),
    "kinematics_ik": (IKRequest, "Compute IK for the selected model; no motion or collision claim"),
    "calibration_get": (
        CalibrationQuery,
        "Read versioned calibration and check applicability; no sampling",
    ),
    "task_finish": (
        TaskFinish,
        "Record the whole current goal outcome with applicable evidence; operation success/archive alone is not goal completion",
    ),
    "agent_session_get": (
        TaskIdentity,
        "Read latest client session and original task/assistance state",
    ),
    "assistance_get": (
        TaskIdentity,
        "Read deterministic role, evidence and allowed actions for a task",
    ),
    "assistance_claim": (
        AssistanceClaim,
        "Host-only operator: revoke old model decisions and claim a task",
    ),
    "assistance_return": (
        AssistanceReturn,
        "Host-only operator: explicitly return control or hand off unknown effects",
    ),
    "task_begin": (
        TaskBegin,
        "Establish a task checkpoint and host-enforced budget; repeated calls do not reset it",
    ),
    "task_context": (
        TaskContext,
        "Read bounded workspace, checkpoint, 3 skill cards, 1 selected contract and 3 memories",
    ),
    "task_recover": (
        TaskRecover,
        "Select an allowed recovery for this task's latest failed operation; use current task epoch",
    ),
    "task_cancel": (
        TaskIdentity,
        "Revoke task admission, cancel its operations and retain evidence",
    ),
    "workspace_read": (
        WorkspaceRead,
        "Read a workspace brief (no arguments), find note cards (query), or read one note (key)",
    ),
    "workspace_note": (
        WorkspaceNote,
        "Save or update a sourced workspace note by key; archive=true removes it from retrieval",
    ),
    "task_checkpoint_save": (
        TaskCheckpointSave,
        "Create or replace one task checkpoint; retain unresolved operations and fixed versions",
    ),
    "task_checkpoint_get": (
        TaskCheckpointGet,
        "Read one explicit task's checkpoint with current operation status; never resumes actions",
    ),
    "task_archive": (
        TaskIdentity,
        "Archive a task checkpoint and return its reference; does not declare operation success",
    ),
    "camera_describe": (
        Empty,
        "Describe semantic camera roles and supported input types without capturing",
    ),
    "camera_capture": (
        CameraCapture,
        "Capture a configured semantic view with typed frames, calibration and clock provenance",
    ),
    "capabilities_list": (
        Empty,
        "List bound capability contracts, versions, providers and skill compatibility",
    ),
    "station_describe": (
        Empty,
        "Discover station, supported tasks, frames and integration blockers; no hardware access",
    ),
    "task_run": (
        TaskRun,
        "Submit skill parameters; declared capabilities/resources determine admission and device use",
    ),
    "tools_list": (Empty, "List capabilities, availability and compact descriptions"),
    "tool_describe": (Describe, "Get one tool's complete input schema"),
    "robot_observe": (Observe, "Read fresh state and image references; no implicit connection"),
    "robot_move": (
        Move,
        "Move in the synthetic workspace; requires fresh observation and request ID",
    ),
    "robot_gripper": (
        Gripper,
        "Set synthetic gripper opening; requires fresh observation and request ID",
    ),
    "robot_hold": (Empty, "Cancel active work and request hold; check confirmed in the result"),
    "skills_list": (
        SkillQuery,
        "Find a bounded set of compatible skill cards by task; load instructions with skill_describe",
    ),
    "skill_describe": (Describe, "Read a skill's contract and operating instructions"),
    "skill_pick_place": (
        PickPlace,
        "Run bounded pick/place code; poll operation for verified outcome",
    ),
    "operation_get": (OperationQuery, "Read operation status; optionally wait up to 30 seconds"),
    "operation_cancel": (
        OperationCancel,
        "Cancel one operation; only declared manipulator ownership requests HOLD",
    ),
    "artifact_info": (ArtifactQuery, "Read artifact metadata; fetch images separately when needed"),
    "depth_estimate": (
        DepthEstimate,
        "Run the host-selected depth backend on typed camera input; return metric depth references",
    ),
    "depth_point": (
        DepthPoint,
        "Project a pixel in the named source stream to its optical frame in metres",
    ),
    "memory_recall": (MemoryRecall, "Retrieve this runtime's scoped evidence; verified by default"),
    "memory_remember": (MemoryRemember, "Save a candidate lesson backed by a completed operation"),
}
MOTION_TOOLS = {"task_recover", "robot_move", "robot_gripper", "skill_pick_place", "robot_command"}
OPERATION_TOOLS = MOTION_TOOLS | {"task_run"}
