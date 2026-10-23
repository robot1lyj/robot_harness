from __future__ import annotations

import asyncio
import hashlib
import time

from .capabilities import ExecutionOwner
from .contracts import HarnessError, Motion, TaskRun
from .storage import Store, canonical, uid

TERMINAL = {"succeeded", "needs_assistance", "failed", "cancelled", "unknown", "timed_out"}


def operation_resources(record):
    # Records predating resource declarations represent robot motion.
    return record.get("resources", ["manipulator"])


def request_body(args):
    # Preserve deduplication fingerprints written before the optional continuation field.
    return args.model_dump(
        exclude={"after_operation_id"}
        if getattr(args, "after_operation_id", None) is None
        else set()
    )


class Operations:
    def __init__(self, store: Store, robot: ExecutionOwner):
        self.store, self.robot = store, robot
        self.tasks: dict[str, asyncio.Task] = {}
        self.resources: dict[str, frozenset] = {}
        self.blocked = False
        self.closed = False
        self.on_finished = lambda _record: None
        self.on_stage = lambda _record: None
        for record in store.all("operation"):
            if record["status"] not in TERMINAL:
                record.update(
                    status="unknown", error={"code": "runtime_interrupted"}, ended_at=time.time()
                )
                store.put("operation", record["id"], record)
                store.event("operation.interrupted", {"id": record["id"]})
            if (
                record["status"] == "unknown"
                and "manipulator" in operation_resources(record)
                and not record.get("hold_reconciled")
            ):
                self.blocked = True

    def stage(self, identity: str, name: str, data: dict | None = None):
        record = self.store.get("operation", identity)
        record["stage"] = name
        record["stage_data"] = data or {}
        self.store.put("operation", identity, record)
        self.on_stage(record)
        self.store.event(
            "operation.stage", {"operation_id": identity, "stage": name, "data": data or {}}
        )

    def existing(self, name: str, args):
        fingerprint = hashlib.sha256(
            canonical({"tool": name, "args": request_body(args)}).encode()
        ).hexdigest()
        if prior := self.store.deduplicate(args.request_id, fingerprint):
            return self.store.get("operation", prior)
        return None

    def submit(
        self,
        name: str,
        args: Motion | TaskRun,
        runner,
        *,
        observation: dict | None = None,
        resources=("manipulator",),
        skill_name: str | None = None,
        skill_version: str | None = None,
        capabilities: list[dict] | None = None,
        deadline_at: float | None = None,
        recovery_of: str | None = None,
        dedup_args=None,
    ):
        request = dedup_args or args
        if prior := self.existing(name, request):
            return prior
        fingerprint = hashlib.sha256(
            canonical({"tool": name, "args": request_body(request)}).encode()
        ).hexdigest()
        timeout = args.timeout_s
        if deadline_at is not None:
            timeout = min(timeout, deadline_at - time.time())
            if timeout <= 0:
                raise HarnessError(
                    "task_budget_exhausted", "Task deadline exhausted before submission"
                )
        resources = frozenset(resources)
        motion = "manipulator" in resources
        if self.closed:
            raise HarnessError("runtime_closed", "Runtime is shutting down")
        if motion and self.blocked:
            raise HarnessError("hold_required", "Runtime needs a confirmed hold before new motion")
        if any(
            not task.done() and resources.intersection(self.resources[identity])
            for identity, task in self.tasks.items()
        ):
            raise HarnessError("resource_busy", "A declared resource is already in use")
        if motion:
            if self.robot is None or not self.robot.supports_motion:
                raise HarnessError("unsupported", "Legacy YAM needs a lease-aware motion bridge")
            if hasattr(args, "arm"):
                self.robot.validate_arm(args.arm)
            if observation is None:
                raise HarnessError(
                    "observation_required", "Motion needs a fresh admission observation"
                )
            self.robot.validate_observation(observation)
        identity = uid("op")
        record = {
            "id": identity,
            "task_id": getattr(args, "task_id", None),
            "task_epoch": getattr(args, "task_epoch", None),
            "recovery_of": recovery_of,
            "tool": name,
            "skill_name": skill_name,
            "skill_version": skill_version,
            "capabilities": capabilities or [],
            "resources": sorted(resources),
            "status": "accepted",
            "stage": "accepted",
            "args": args.model_dump(),
            "scope": self.store.scope,
            "qualification": getattr(self.robot, "qualification", "provider_dependent"),
            "created_at": time.time(),
            "result": None,
            "error": None,
            "assistance": "external_agent" if recovery_of else "none",
        }
        with self.store.transaction():
            self.store.put("operation", identity, record)
            self.store.bind_request(args.request_id, fingerprint, identity)
            self.store.event("operation.accepted", record)
        self.resources[identity] = resources
        self.tasks[identity] = asyncio.create_task(self._run(identity, timeout, runner))
        self.tasks[identity].add_done_callback(lambda _task: self._retire(identity))
        return record

    def _retire(self, identity):
        # Durable outcomes/dedup stay in Store; only live coordination uses handles.
        self.tasks.pop(identity, None)
        self.resources.pop(identity, None)

    async def _stop(self, record):
        if "manipulator" not in operation_resources(record):
            return None
        try:
            stopped = await self.robot.hold()
        except Exception:
            stopped = {"confirmed": False}
        if not stopped.get("confirmed"):
            self.blocked = True
            record["status"] = "unknown"
        return stopped

    async def _run(self, identity, timeout, runner):
        record = self.store.get("operation", identity)
        record["status"] = "running"
        self.store.put("operation", identity, record)
        try:
            async with asyncio.timeout(timeout):
                result = await runner(identity)
            record.update(status="succeeded", result=result)
        except asyncio.CancelledError:
            record.update(status="cancelled", error={"code": "cancelled"})
        except TimeoutError:
            record.update(status="timed_out", error={"code": "deadline_exceeded"})
        except HarnessError as exc:
            status = (
                "needs_assistance"
                if exc.code
                in ("grasp_failed", "verification_unknown", "target_missing", "path_unavailable")
                else "failed"
            )
            record.update(status=status, error=exc.payload())
            if status == "needs_assistance":
                record["suggested_assistance"] = "luna_class_agent"
        except Exception as exc:
            record.update(
                status="unknown", error={"code": "execution_error", "type": type(exc).__name__}
            )
            if "manipulator" in operation_resources(record):
                self.blocked = True
        finally:
            stopped = await self._stop(record)
            record.update(
                hold=stopped,
                ended_at=time.time(),
                stage=self.store.get("operation", identity)["stage"],
                stage_data=self.store.get("operation", identity).get("stage_data", {}),
            )
            self.store.put("operation", identity, record)
            self.on_finished(record)
            self.store.event("operation.finished", record)

    async def get(self, identity: str, wait_s: float = 0):
        self.store.get("operation", identity)
        task = self.tasks.get(identity)
        if task and wait_s:
            await asyncio.wait({task}, timeout=wait_s)
        return self.store.get("operation", identity)

    async def cancel(self, identity: str):
        record = self.store.get("operation", identity)
        task = self.tasks.get(identity)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                # Cancel before coroutine's first step: no finally block ran.
                record["status"] = "cancelled"
                stopped = await self._stop(record)
                record.update(hold=stopped, ended_at=time.time())
                self.store.put("operation", identity, record)
                self.on_finished(record)
                self.store.event("operation.finished", record)
        return self.store.get("operation", identity)

    async def hold(self):
        if self.robot is None:
            raise HarnessError("unsupported", "No execution owner is configured")
        self.blocked = True
        for identity, task in list(self.tasks.items()):
            if not task.done() and "manipulator" in self.resources[identity]:
                await self.cancel(identity)
        stopped = await self.robot.hold()
        self.blocked = not stopped.get("confirmed", False)
        if not self.blocked:
            for record in self.store.all("operation"):
                if record["status"] == "unknown" and "manipulator" in operation_resources(record):
                    record["hold_reconciled"] = True
                    self.store.put("operation", record["id"], record)
        self.store.event("robot.hold", stopped)
        return stopped

    async def close(self):
        self.closed = True
        for identity, task in list(self.tasks.items()):
            if not task.done():
                await self.cancel(identity)
