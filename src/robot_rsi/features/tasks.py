from __future__ import annotations
from dataclasses import dataclass
from ..contracts import Describe, HarnessError, OperationCancel, OperationQuery, SkillQuery, TaskBegin, TaskCheckpointGet, TaskCheckpointSave, TaskFinish, TaskIdentity, TaskRecover, TaskRun


@dataclass
class TaskTools:
    _run_skill: object
    _bindings: object
    environment: object
    operations: object
    profile: object
    registry: object
    scenes: object
    select_skill: object
    store: object
    task_memory: object

    definitions = {
        'task_begin': (
        TaskBegin,
        "Establish a task checkpoint and host-enforced budget; repeated calls do not reset it",
    ),
        'task_finish': (
        TaskFinish,
        "Record the whole current goal outcome with applicable evidence; operation success/archive alone is not goal completion",
    ),
        'task_cancel': (
        TaskIdentity,
        "Revoke task admission, cancel its operations and retain evidence",
    ),
        'task_recover': (
        TaskRecover,
        "Select an allowed recovery for this task's latest failed operation; use current task epoch",
    ),
        'task_checkpoint_save': (
        TaskCheckpointSave,
        "Create or replace one task checkpoint; retain unresolved operations and fixed versions",
    ),
        'task_checkpoint_get': (
        TaskCheckpointGet,
        "Read one explicit task's checkpoint with current operation status; never resumes actions",
    ),
        'task_archive': (
        TaskIdentity,
        "Archive a task checkpoint and return its reference; does not declare operation success",
    ),
        'task_run': (
        TaskRun,
        "Submit skill parameters; declared capabilities/resources determine admission and device use",
    ),
        'operation_get': (OperationQuery, "Read operation status; optionally wait up to 30 seconds"),
        'operation_cancel': (
        OperationCancel,
        "Cancel one operation; only declared manipulator ownership requests HOLD",
    ),
        'skills_list': (
        SkillQuery,
        "Find a bounded set of compatible skill cards by task; load instructions with skill_describe",
    ),
        'skill_describe': (Describe, "Read a skill's contract and operating instructions"),
    }

    native_tools = ('task_checkpoint_get', 'task_run', 'task_recover', 'task_cancel', 'task_archive', 'task_finish', 'operation_get', 'operation_cancel', 'skills_list', 'skill_describe')
    motion_tools = {'task_recover'}
    operation_tools = {'task_run', 'task_recover'}

    @property
    def bindings(self):
        return self._bindings()

    async def dispatch(self, name, args, *, task_id=None):
        if name == "task_begin":
            versions = None
            capabilities = None
            if args.binding_ref:
                bound, versions = self.scenes.resolve(args.binding_ref, args.binding_conditions)
                capabilities = bound.catalog()
            try:
                previous = self.task_memory._load(args.task_id)
            except HarnessError as exc:
                if exc.code != "not_found":
                    raise
            else:
                if args.binding_ref and previous.get("execution") and previous.get("resource_versions", {}).get("scene_binding") != args.binding_ref.model_dump():
                    raise HarnessError("task_conflict", "Task binding is already fixed")
            result = self.task_memory.begin(args, self.profile.task_limits, versions, capabilities)
            return {**result, "environment": self.environment(args.task_id)}
        if name == "task_finish":
            result = self.task_memory.finish(args)
            self.registry.release(args.task_id)
            return result
        if name == "task_cancel":
            record = self.task_memory.cancel(args.task_id)
            self.registry.release(args.task_id)
            for identity in record["operation_ids"]:
                await self.operations.cancel(identity)
            return self.task_memory.get(
                TaskCheckpointGet(task_id=args.task_id, include_archived=True)
            )
        if name == "task_recover":
            if prior := self.operations.existing(name, args):
                return prior
            original = self.store.get("operation", args.operation_id)
            if original.get("task_id") != args.task_id:
                raise HarnessError("task_mismatch", "Recovery evidence belongs to another task")
            stored_args = dict(original["args"])
            if "parameters" not in stored_args:
                stored_args["parameters"] = {
                    key: stored_args.pop(key)
                    for key in ("arm", "object_id", "target_region", "grasp_retries")
                    if key in stored_args
                }
            goal = TaskRun.model_validate(
                {
                    **stored_args,
                    "request_id": args.request_id,
                    "task_id": args.task_id,
                    "task_epoch": args.task_epoch,
                }
            )
            return await self._run_skill(
                goal, tool=name, recovery_of=args.operation_id, dedup_args=args
            )
        if name == "task_checkpoint_save":
            self.task_memory.skill_versions = self.registry.versions()
            result = self.task_memory.save(args)
            for skill, version in result["skill_versions"].items():
                self.registry.pin(args.task_id, skill, version)
            return result
        if name == "task_checkpoint_get":
            return self.task_memory.get(args)
        if name == "task_archive":
            result = self.task_memory.archive(args.task_id)
            self.registry.release(args.task_id)
            return result
        if name == "task_run":
            return await self._run_skill(args)
        if name == "operation_get":
            return await self.operations.get(args.operation_id, args.wait_s)
        if name == "operation_cancel":
            operation = self.store.get("operation", args.operation_id)
            if operation.get("task_id"):
                self.task_memory.cancel(operation["task_id"])
                self.registry.release(operation["task_id"])
            return await self.operations.cancel(args.operation_id)
        if name == "skills_list":
            return self.registry.cards(args, bindings=self.bindings)
        if name == "skill_describe":
            return (
                self.select_skill(task_id, args.name)
                if task_id
                else self.registry.resolve(args.name, bindings=self.bindings)
            )
        raise HarnessError("unknown_tool", "Tool not found")
