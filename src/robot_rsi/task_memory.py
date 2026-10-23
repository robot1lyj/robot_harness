"""One current checkpoint per explicit task ID, in the host's existing scope."""

from __future__ import annotations

import time
import json

from .contracts import (
    HarnessError,
    MemoryMaintenance,
    TaskCheckpointGet,
    TaskCheckpointSave,
    TaskLimits,
)
from .delivery import preserve, check_results, check_method, operation_results
from .operations import TERMINAL, operation_resources
from .storage import Store, canonical
from .task_snapshot import maintain, unique


class TaskMemory:
    def __init__(
        self,
        store: Store,
        *,
        skill_versions: dict[str, str],
        capabilities: list[dict],
        maintenance: MemoryMaintenance | None = None,
    ):
        self.store = store
        self.skill_versions, self.capabilities = skill_versions, capabilities
        self.maintenance = maintenance or MemoryMaintenance()
        self._checked_files: set[str] = set()
        self.installation_digest = None
        self.resource_versions = {}

    def _load(self, task_id: str):
        return self.store.get("task_checkpoint", "task-checkpoint_" + task_id)

    def _operations(self, identities: list[str]) -> list[dict]:
        summaries = []
        for identity in identities:
            record = self.store.get("operation", identity)
            summaries.append(
                {
                    "id": identity,
                    "tool": record.get("tool"),
                    "status": record["status"],
                    "stage": record.get("stage"),
                    **({"stage_data": record["stage_data"]} if "stage_data" in record else {}),
                    "skill_name": record.get("skill_name"),
                    "skill_version": record.get("skill_version"),
                    "qualification": record.get("qualification", "unknown"),
                    "error_code": (record.get("error") or {}).get("code"),
                    "verified": (record.get("result") or {}).get("verified"),
                    "hold_confirmed": (record.get("hold") or {}).get("confirmed"),
                }
            )
        return summaries

    @staticmethod
    def _unresolved(operations: list[dict]) -> list[str]:
        return [
            op["id"]
            for op in operations
            if op["status"] not in TERMINAL
            or op["status"] == "unknown"
            or op["error_code"] == "verification_unknown"
        ]

    def _view(self, record: dict) -> dict:
        operations = self._operations(record["operation_ids"])
        return {
            **{key: value for key, value in record.items() if key != "memory_state"},
            **({"remaining_budget": self.remaining(record)} if "execution" in record else {}),
            "operations": operations,
            "unresolved_operation_ids": self._unresolved(operations),
            "operation_source": "operation_records; summaries do not change execution state",
            "current_memory": self.memory_view(record),
            "task_outcome": self.outcome(record),
        }

    def get(self, args: TaskCheckpointGet) -> dict:
        record = self._load(args.task_id)
        if record["archived"] and not args.include_archived:
            raise HarnessError("not_found", "Task checkpoint is archived")
        if args.archive_id:
            archive = self.store.get("task_memory_archive", args.archive_id)
            if archive["task_id"] != args.task_id:
                raise HarnessError("not_found", "No archive belongs to this task")
            return archive
        self._write(record)
        return self._view(record)

    def save(self, args: TaskCheckpointSave) -> dict:
        try:
            previous = self._load(args.task_id)
        except HarnessError as exc:
            if exc.code != "not_found":
                raise
            previous = None
        if previous and previous.get("task_result"):
            raise HarnessError("task_finished", "Task result is final; read or archive it")
        if previous and previous["archived"]:
            raise HarnessError("task_archived", "Use a new task ID after archival")

        if previous and previous["goal"] != args.goal:
            raise HarnessError("task_conflict", "Keep the original goal; record phase progress in last_confirmed")
        try:
            delivery = preserve(previous.get("delivery") if previous else None, args.delivery, args.goal)
        except ValueError as exc:
            raise HarnessError("delivery_conflict", str(exc)) from exc

        # Carry unresolved evidence forward even when the caller omits it in a new summary.
        unresolved = (
            self._unresolved(self._operations(previous["operation_ids"])) if previous else []
        )
        identities = list(dict.fromkeys([*args.operation_ids, *unresolved]))
        if previous and "execution" in previous:
            identities = list(dict.fromkeys([*previous["operation_ids"], *identities]))
            for identity in identities:
                if self.store.get("operation", identity).get("task_id") != args.task_id:
                    raise HarnessError("task_mismatch", "Operation belongs to another task")
        if len(identities) > 16:
            raise HarnessError(
                "checkpoint_full", "Unresolved operations exceed the checkpoint limit"
            )
        operations = self._operations(identities)
        versions = dict(previous["skill_versions"]) if previous else {}
        operation_versions = {}
        for op in operations:
            if op["skill_name"] and op["skill_version"]:
                name, version = op["skill_name"], op["skill_version"]
                if name in operation_versions and operation_versions[name] != version:
                    raise HarnessError(
                        "skill_version_conflict", "Task evidence uses different versions"
                    )
                operation_versions[name] = version
        for name, version in {**args.skill_versions, **operation_versions}.items():
            if name in args.skill_versions and args.skill_versions[name] != version:
                raise HarnessError(
                    "skill_version_conflict", "Version differs from operation evidence"
                )
            if name in versions and versions[name] != version:
                raise HarnessError("skill_version_conflict", "Task skill versions are fixed")
            if (
                name not in versions
                and name not in operation_versions
                and self.skill_versions.get(name) != version
            ):
                raise HarnessError("skill_version_conflict", "Select a published skill version")
            versions[name] = version
        if len(versions) > 8:
            raise HarnessError("checkpoint_full", "Selected skills exceed the checkpoint limit")
        for ref in args.evidence_refs:
            self.store.get(
                ref.kind, "workspace-note_" + ref.id if ref.kind == "workspace_note" else ref.id
            )

        content = {
            **args.model_dump(),
            "delivery": delivery,
            "operation_ids": identities,
            "skill_versions": versions,
            "capabilities": previous["capabilities"] if previous else self.capabilities,
            "constraints": unique(args.constraints),
            "unknowns": unique(args.unknowns),
            "evidence_refs": unique([ref.model_dump() for ref in args.evidence_refs]),
        }
        if previous and "execution" in previous:
            content.update(
                execution=previous["execution"], remaining_budget=previous["remaining_budget"]
            )
        if previous and "resource_versions" in previous:
            content["resource_versions"] = previous["resource_versions"]
        if previous and "memory_state" in previous:
            content["memory_state"] = previous["memory_state"]
        if previous and all(previous.get(key) == value for key, value in content.items()):
            self._write(previous)
            return {**self._view(previous), "changed": False}
        record = {
            **content,
            "scope": self.store.scope,
            "revision": previous["revision"] + 1 if previous else 1,
            "updated_at": time.time(),
            "archived": False,
        }
        self._write(record)
        return {**self._view(record), "changed": True}

    def outcome(self, record):
        if record.get("task_result"):
            return {"outcome": record["task_result"]["outcome"], "source": "task_finish"}
        if record.get("execution", {}).get("status") == "cancelled":
            return {"outcome": "cancelled", "source": "execution"}
        if record["archived"]:
            operations = self._operations(record["operation_ids"])
            if self._unresolved(operations) or record["unknowns"]:
                return {"outcome": "unknown", "source": "unresolved_archived_evidence"}
            if operations and operations[-1]["status"] in (
                "failed",
                "needs_assistance",
                "timed_out",
                "cancelled",
            ):
                return {
                    "outcome": "cancelled" if operations[-1]["status"] == "cancelled" else "failed",
                    "source": "archived_operation",
                }
            return {"outcome": "incomplete", "source": "archive_without_task_result"}
        return {"outcome": "pending", "source": "no_task_result"}

    def finish(self, args):
        record = self._load(args.task_id)
        conclusion = {
            "outcome": args.outcome,
            "summary": args.summary,
            "operation_ids": unique(args.operation_ids),
            "evidence_refs": unique([r.model_dump() for r in args.evidence_refs]),
            "criterion_results": [r.model_dump() for r in args.criterion_results],
            "method_evidence": args.method_evidence.model_dump() if args.method_evidence else None,
        }
        selected_ids = [identity for identity in record["operation_ids"] if identity in conclusion["operation_ids"]]
        if not args.criterion_results:
            conclusion["criterion_results"] = operation_results(self.store, selected_ids)
        if record.get("task_result"):
            if all(record["task_result"].get(k, [] if k == "criterion_results" else None) == v for k, v in conclusion.items()):
                return self._view(record)
            raise HarnessError("task_result_conflict", "A final task conclusion cannot be replaced")
        state = record.get("execution")
        if record["archived"] or state is None or state["status"] not in ("active", "cancelled"):
            raise HarnessError("task_closed", "Task does not accept an ending conclusion")
        if args.task_epoch != state["epoch"]:
            raise HarnessError("stale_task_epoch", "Task ending must use the current epoch")
        if state["status"] == "cancelled" and args.outcome != "cancelled":
            raise HarnessError(
                "task_closed", "Cancellation cannot be changed into success or failure"
            )
        operations = self._operations(record["operation_ids"])
        if any(op["status"] not in TERMINAL for op in operations):
            raise HarnessError(
                "operation_active", "Wait or cancel active operations before ending the task"
            )
        sources = []
        for identity in conclusion["operation_ids"]:
            if identity not in record["operation_ids"]:
                raise HarnessError("task_mismatch", "Ending evidence is not linked to this task")
            op = self.store.get("operation", identity)
            if (
                op.get("task_id") != args.task_id
                or op.get("capabilities") != record["capabilities"]
                or record["skill_versions"].get(op.get("skill_name")) != op.get("skill_version")
            ):
                raise HarnessError(
                    "inapplicable_evidence", "Operation task/version/binding differs"
                )
            sources.append(op)
        materials = []
        for ref in args.evidence_refs:
            value = self.store.get(
                ref.kind, "workspace-note_" + ref.id if ref.kind == "workspace_note" else ref.id
            )
            materials.append((ref, value))
            if args.outcome == "succeeded" and value.get("archived"):
                raise HarnessError(
                    "inapplicable_evidence", "Archived material cannot support current completion"
                )
        if args.outcome == "succeeded":
            if self._unresolved(operations) or record["unknowns"]:
                raise HarnessError(
                    "unresolved_effects", "Unknown task effects cannot be declared successful"
                )
            if not sources and not args.evidence_refs:
                raise HarnessError(
                    "completion_evidence_required",
                    "A whole-goal conclusion requires applicable evidence",
                )
            if operations and record["operation_ids"][-1] not in conclusion["operation_ids"]:
                raise HarnessError(
                    "inapplicable_evidence",
                    "Include the latest step; an earlier success cannot hide unfinished work",
                )
            for op in sources:
                result = op.get("result") or {}
                if op["status"] != "succeeded" or (
                    op["id"] == record["operation_ids"][-1]
                    and (
                        result.get("completed") is False
                        or result.get("verified") is False
                        or (result.get("prepared") is True and result.get("completed") is not True)
                    )
                ):
                    raise HarnessError(
                        "inapplicable_evidence", "Evidence reports a failed or intermediate step"
                    )
                if "manipulator" in operation_resources(op) and (
                    result.get("verified") is not True
                    or not (op.get("hold") or {}).get("confirmed")
                ):
                    raise HarnessError(
                        "inapplicable_evidence",
                        "Motion completion requires verification and confirmed HOLD",
                    )
        delivery = record.get("delivery")
        # Operation-owned criterion observations cannot be replaced by a caller's assertion.
        authoritative = {result["criterion_id"]: result for result in operation_results(self.store, selected_ids)}
        supplied = {r["criterion_id"]: r for r in conclusion["criterion_results"]}
        if any(supplied.get(key) != value for key, value in authoritative.items()):
            raise HarnessError("criterion_conflict", "Use original operation criterion results")
        for result in conclusion["criterion_results"]:
            from .contracts import CriterionResult
            CriterionResult.model_validate(result)
            for ref in result["evidence_refs"]:
                value = self.store.get(ref["kind"], "workspace-note_" + ref["id"] if ref["kind"] == "workspace_note" else ref["id"])
                if args.outcome == "succeeded" and (value.get("archived") or value.get("task_id", args.task_id) != args.task_id):
                    raise HarnessError("inapplicable_evidence", "Criterion evidence is archived or belongs to another task")
        if delivery and args.outcome == "succeeded":
            issues = check_results(delivery, conclusion["criterion_results"])
            if delivery["kind"] == "reusable_method":
                report = None
                if args.method_evidence:
                    if args.method_evidence.kind != "artifact":
                        raise HarnessError("inapplicable_evidence", "Method evidence must be a fixed JSON artifact")
                    report = json.loads(self.store.read_artifact(args.method_evidence.id))
                issues += check_method(delivery, report)
                entry = delivery.get("entry")
                if entry and record["skill_versions"].get(entry["skill"]) != entry["version"]:
                    issues.append("Method entry differs from task skill versions")
            if issues:
                raise HarnessError("goal_not_satisfied", "; ".join(issues))
        snapshots = []
        for ref, value in materials:
            if ref.kind in ("workspace_note", "memory"):
                identity = self.store.artifact(
                    canonical(value).encode(),
                    "application/json",
                    {"kind": ref.kind, "id": ref.id, "revision": value.get("revision")},
                )
                snapshots.append(
                    {
                        "kind": ref.kind,
                        "id": ref.id,
                        "revision": value.get("revision"),
                        "artifact_id": identity,
                    }
                )
        record["task_result"] = {
            **conclusion,
            "goal": record["goal"],
            "delivery": delivery,
            "constraints": record["constraints"],
            "skill_versions": record["skill_versions"],
            "at": time.time(),
            "source": "explicit_whole_goal_conclusion",
            "evidence_snapshots": snapshots,
        }
        state["status"] = "finished" if state["status"] != "cancelled" else "cancelled"
        record.update(revision=record["revision"] + 1, updated_at=time.time())
        self._write(record)
        self.store.event("task.finished", {"task_id": args.task_id, **record["task_result"]})
        return self._view(record)

    def archive(self, task_id: str) -> dict:
        record = self._load(task_id)
        if "execution" in record and any(
            op["status"] not in TERMINAL for op in self._operations(record["operation_ids"])
        ):
            raise HarnessError("operation_active", "Cancel or wait for active work before archival")
        if not record["archived"]:
            record.update(archived=True, revision=record["revision"] + 1, updated_at=time.time())
            self._write(record)
        self._checked_files.discard(task_id)
        return {
            "task_id": task_id,
            "revision": record["revision"],
            "archived": True,
            "task_outcome": self.outcome(record),
            "reference": {
                "tool": "task_checkpoint_get",
                "arguments": {"task_id": task_id, "include_archived": True},
            },
        }

    def _write(self, record: dict):
        # Legacy quotas remain readable provenance only; they never reach admission.
        if "execution" in record:
            record["execution"].pop("model_token_limit", None)
        record["remaining_budget"]["model_tokens"] = None
        maintain(self, record)
        self.store.put("task_checkpoint", "task-checkpoint_" + record["task_id"], record)

    def memory_view(self, record: dict):
        state = record.get("memory_state")
        if not state:
            return None
        return {
            key: state[key]
            for key in (
                "revision",
                "file_ref",
                "updated_at",
                "archive_head",
                "view",
                "writes",
                "model_calls",
                "decision",
                "duration_ms",
            )
        }

    def context(self, task_id: str):
        full = self.get(TaskCheckpointGet(task_id=task_id))
        current = full["current_memory"]
        return {
            **current["view"],
            "revision": full["revision"],
            "archived": full["archived"],
            "execution": full.get("execution"),
            "remaining_budget": full["remaining_budget"],
            "memory": {key: value for key, value in current.items() if key != "view"},
        }

    def begin(self, args, limits: TaskLimits, resource_versions=None, capabilities=None):
        try:
            record = self._load(args.task_id)
        except HarnessError as exc:
            if exc.code != "not_found":
                raise
            self.save(
                TaskCheckpointSave(
                    task_id=args.task_id,
                    goal=args.goal,
                    delivery=args.delivery,
                    constraints=args.constraints,
                    skill_versions={},
                    last_confirmed="任务已建立，尚未执行",
                    operation_ids=[],
                    unknowns=[],
                    remaining_budget={"tool_calls": None, "time_s": None, "model_tokens": None},
                    evidence_refs=[],
                )
            )
            record = self._load(args.task_id)
            if args.recipe:
                record["recipe"] = args.recipe
        try:
            delivery = preserve(record.get("delivery"), args.delivery, args.goal)
        except ValueError as exc:
            raise HarnessError("delivery_conflict", str(exc)) from exc
        if (
            record["archived"]
            or record["goal"] != args.goal
            or record["constraints"] != unique(args.constraints)
            or record.get("recipe") != args.recipe
        ):
            raise HarnessError("task_conflict", "Task ID already has different or archived content")
        if delivery != record.get("delivery"):
            if record.get("task_result"):
                raise HarnessError("task_finished", "A finished task cannot add a delivery contract")
            record["delivery"] = delivery
            self._write(record)
        if "execution" not in record:
            record["resource_versions"] = resource_versions if resource_versions is not None else self.resource_versions
            if capabilities is not None:
                record["capabilities"] = capabilities
            # Adopting an H02 record cannot replenish its recorded remaining allowance.
            remaining = record["remaining_budget"]
            calls = remaining["tool_calls"]
            seconds = remaining["time_s"]
            record["execution"] = {
                "status": "active",
                "epoch": 0,
                "started_at": time.time(),
                "deadline_at": time.time()
                + min(limits.duration_s, seconds if seconds is not None else limits.duration_s),
                "tool_call_limit": min(
                    limits.tool_calls, calls if calls is not None else limits.tool_calls
                ),
                "tool_calls": 1,
                "recovery_limit": limits.recoveries,
                "recoveries": 0,
                "model_usage": None,
                "installation_digest": self.installation_digest,
            }
            self._write(record)
        return self._view(record)

    @staticmethod
    def remaining(record: dict):
        state = record["execution"]
        return {
            "tool_calls": max(0, state["tool_call_limit"] - state["tool_calls"]),
            "time_s": max(0, state["deadline_at"] - time.time()),
            "model_tokens": None,
        }

    def charge(self, task_id: str, name: str):
        record = self._load(task_id)
        if "execution" not in record:
            return  # H02 standalone checkpoints retain their original semantics.
        state = record["execution"]
        state["tool_calls"] += 1
        self._write(record)
        evidence_tools = {
            "task_checkpoint_get",
            "task_checkpoint_save",
            "task_context",
            "task_archive",
            "task_finish",
            "task_cancel",
            "operation_get",
            "operation_cancel",
            "robot_hold",
            "assistance_get",
            "agent_session_get",
            "assistance_claim",
            "assistance_return",
        }
        if name in evidence_tools:
            return
        self.check_budget(record)
        if record["archived"] or state["status"] != "active":
            raise HarnessError("task_closed", "Task no longer admits work")

    def check_budget(self, record: dict):
        state = record["execution"]
        if state["tool_calls"] > state["tool_call_limit"] or time.time() >= state["deadline_at"]:
            raise HarnessError("task_budget_exhausted", "Task call allowance or deadline exhausted")

    def select_skill(self, task_id: str, name: str, version: str, capabilities=None):
        record = self._load(task_id)
        if name not in record["skill_versions"] and len(record["skill_versions"]) >= 8:
            raise HarnessError("checkpoint_full", "Selected skills exceed the checkpoint limit")
        if record["skill_versions"].get(name, version) != version:
            raise HarnessError("skill_version_conflict", "Task is pinned to another skill version")
        if record["capabilities"] != (self.capabilities if capabilities is None else capabilities):
            raise HarnessError(
                "binding_changed", "Task capability binding differs from this runtime"
            )
        record["skill_versions"][name] = version
        self._write(record)

    def recovery_options(self, record: dict) -> list[dict]:
        if "execution" not in record or record["archived"]:
            return []
        state = record["execution"]
        if state["status"] != "active" or state["recoveries"] >= state["recovery_limit"]:
            return []
        try:
            self.check_budget(record)
        except HarnessError:
            return []
        if not record["operation_ids"]:
            return []
        op = self.store.get("operation", record["operation_ids"][-1])
        error = op.get("error") or {}
        if (
            op["status"] == "needs_assistance"
            and error.get("code") == "grasp_failed"
            and error.get("details", {}).get("retryable") is True
            and (op.get("hold") or {}).get("confirmed") is True
        ):
            return [
                {
                    "action": "retry_pick_place",
                    "operation_id": op["id"],
                    "task_epoch": state["epoch"],
                }
            ]
        return []

    def admit(
        self,
        task_id: str,
        epoch: int | None,
        *,
        recovery_of: str | None = None,
        after_operation_id: str | None = None,
    ):
        record = self._load(task_id)
        if "execution" not in record:
            raise HarnessError(
                "task_not_started", "Use task_begin to establish an execution budget"
            )
        state = record["execution"]
        if record["archived"] or state["status"] != "active":
            raise HarnessError("task_closed", "Task was cancelled or archived")
        if epoch != state["epoch"]:
            raise HarnessError(
                "stale_task_epoch", "Read the current task context before submitting"
            )
        if state.get("installation_digest") not in (None, self.installation_digest):
            raise HarnessError(
                "installation_changed",
                "Installed modules/dependencies changed; inspect old evidence and end the task before new work",
            )
        self.check_budget(record)
        if recovery_of:
            if not any(
                option["operation_id"] == recovery_of for option in self.recovery_options(record)
            ):
                raise HarnessError(
                    "recovery_not_allowed",
                    "Failure, ownership or recovery allowance does not permit this action",
                )
        elif record["operation_ids"]:
            latest = self.store.get("operation", record["operation_ids"][-1])
            if (
                after_operation_id != latest["id"]
                or latest["status"] != "succeeded"
                or self._unresolved(self._operations(record["operation_ids"]))
            ):
                raise HarnessError(
                    "task_recovery_required",
                    "Use allowed recovery or explicitly reference the last successful step for continuation",
                )
        elif after_operation_id:
            raise HarnessError("task_mismatch", "No previous step belongs to this task")
        return record

    def attach(self, task_id: str, operation: dict, *, recovery_of: str | None = None):
        record = self._load(task_id)
        if operation["id"] in record["operation_ids"]:
            return
        record["operation_ids"].append(operation["id"])
        record["execution"]["epoch"] += 1
        if recovery_of:
            record["execution"]["recoveries"] += 1
        record["last_confirmed"] = "操作已接受，尚未确认结果：" + operation["id"]
        self._write(record)

    def finished(self, operation: dict):
        if not operation.get("task_id"):
            return
        record = self._load(operation["task_id"])
        record["last_confirmed"] = (
            f"{operation['id']}: {operation['status']} / {operation.get('stage')}"
        )
        observation = (operation.get("result") or {}).get("observation_id")
        if observation:
            ref = {"kind": "observation", "id": observation}
            if ref not in record["evidence_refs"] and len(record["evidence_refs"]) < 16:
                record["evidence_refs"].append(ref)
        self._write(record)

    def stage(self, operation: dict):
        if operation.get("task_id"):
            self._write(self._load(operation["task_id"]))

    def cancel(self, task_id: str):
        record = self._load(task_id)
        if record.get("task_result"):
            return record
        if "execution" in record and record["execution"]["status"] != "cancelled":
            record["execution"]["status"] = "cancelled"
            record["execution"]["epoch"] += 1
            self._write(record)
        return record

    def record_usage(self, task_id: str, usage: dict, *, client: str, model: str):
        """Host-only accounting from client reports; never accepts a model estimate."""
        record = self._load(task_id)
        measured = {key: usage[key] for key in ("input_tokens", "output_tokens")}
        if any(not isinstance(value, int) or value < 0 for value in measured.values()):
            raise HarnessError(
                "invalid_usage", "Client usage must contain measured nonnegative tokens"
            )
        previous = record["execution"]["model_usage"] or {}
        for key in measured:
            measured[key] += previous.get(key, 0)
        record["execution"]["model_usage"] = {
            **measured,
            "total_tokens": sum(measured.values()),
            "client": client,
            "model": model,
            "measurement": "client_report; observation_only",
        }
        for key in ("cached_input_tokens", "cache_write_input_tokens", "reasoning_output_tokens"):
            if key in usage:
                value = usage[key]
                if not isinstance(value, int) or value < 0:
                    raise HarnessError(
                        "invalid_usage", "Client cache usage must be nonnegative tokens"
                    )
                record["execution"]["model_usage"][key] = previous.get(key, 0) + value
            elif key in previous:
                record["execution"]["model_usage"][key] = previous[key]
        self._write(record)
