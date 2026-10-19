"""Task/session association and deterministic assistance; no model or device transport."""

from __future__ import annotations

import json
import time

from .contracts import HarnessError, SkillQuery, TaskCheckpointGet
from .operations import TERMINAL
from .storage import canonical, uid

SESSION_READS = {
    "task_context",
    "task_checkpoint_get",
    "operation_get",
    "assistance_get",
}
OPERATOR_TOOLS = {"assistance_claim", "assistance_return"}


class AgentState:
    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        # A new host never resumes a model process or repeats its action requests.
        for session in self.store.all("agent_session"):
            if session["status"] == "running":
                session.update(status="disconnected", ended_at=time.time())
                self.store.put("agent_session", session["id"], session)

    def control(self, task_id):
        try:
            return self.store.get("agent_control", "agent-control_" + task_id)
        except HarnessError as exc:
            if exc.code != "not_found":
                raise
            return {
                "id": "agent-control_" + task_id,
                "task_id": task_id,
                "active_session": None,
                "request_id": None,
                "attempts": [],
            }

    def save_control(self, control):
        self.store.put("agent_control", control["id"], control)

    def guard(self, session_id, task_id, name):
        session = self.store.get("agent_session", session_id)
        record = self.runtime.task_memory._load(task_id)
        lease = (
            session["task_id"] == task_id
            and session["status"] == "running"
            and self.control(task_id)["active_session"] == session_id
            and session["task_epoch"] == record["execution"]["epoch"]
        )
        valid = lease and record["execution"]["status"] == "active" and not record["archived"]
        ending = (
            lease
            and record["execution"]["status"] == "finished"
            and name in ("task_finish", "task_archive")
        )
        if not valid and not ending and name not in SESSION_READS:
            raise HarnessError("stale_agent_session", "Session no longer owns task decisions")
        if name in OPERATOR_TOOLS:
            raise HarnessError("operator_required", "Operator control is host-only")

    def request(self, task_id):
        record = self.runtime.task_memory._load(task_id)
        if "execution" not in record:
            return {
                "task_id": task_id,
                "task_epoch": None,
                "role": "closed" if record["archived"] else "operator",
                "reason": "archive_without_task_result"
                if record["archived"]
                else "task_not_started",
                "task_outcome": self.runtime.task_memory.outcome(record),
                "configured": True,
                "remaining_budget": record["remaining_budget"],
                "allowed_recovery": [],
                "checkpoint": {"tool": "task_checkpoint_get", "task_id": task_id},
            }
        context = (
            self.runtime.task_memory.get(TaskCheckpointGet(task_id=task_id, include_archived=True))
            if record["archived"]
            else self.runtime.task_memory.context(task_id)
        )
        state = record["execution"]
        operations = [self.store.get("operation", i) for i in record["operation_ids"]]
        unknown = any(
            op["status"] == "unknown"
            or (op.get("error") or {}).get("code") == "verification_unknown"
            for op in operations
        )
        options = self.runtime.task_memory.recovery_options(record)
        if state["status"] == "operator" and state.get("operator_request"):
            owned = self.store.get("assistance_request", state["operator_request"])
            return {
                **owned,
                "role": "operator",
                "reason": "operator_owned",
                "task_epoch": state["epoch"],
                "remaining_budget": context["remaining_budget"],
                "configured": True,
            }
        outcome = self.runtime.task_memory.outcome(record)
        if record.get("task_result") or record["archived"] or state["status"] == "cancelled":
            role = "complete" if outcome["outcome"] == "succeeded" else "closed"
            reason = "task_" + outcome["outcome"]
        elif state["status"] == "operator" or unknown:
            role, reason = (
                "operator",
                "operator_owned" if state["status"] == "operator" else "unknown_effect",
            )
        elif operations and any(op["status"] not in TERMINAL for op in operations):
            role, reason = "fast", "query_existing_operation"
        elif operations and operations[-1]["status"] == "succeeded":
            role, reason = "fast", "task_unfinished"
        elif options:
            role, reason = "fast", "allowed_recovery"
        else:
            cards = self.runtime.registry.cards(SkillQuery(query=record["goal"][:200]),
                                                bindings=self.runtime.task_bindings(task_id))
            role, reason = (
                ("teacher", "capability_gap")
                if not cards["skills"]
                else (
                    ("teacher", "failure_without_recovery")
                    if operations
                    else ("fast", "initial_goal")
                )
            )
        material = {
            "task_id": task_id,
            "task_epoch": state["epoch"],
            "role": role,
            "reason": reason,
            "last_confirmed": record["last_confirmed"],
            "task_outcome": outcome,
            "task_result": record.get("task_result"),
            "unknowns": record["unknowns"],
            "operations": context["operations"],
            "skill_versions": record["skill_versions"],
            "allowed_recovery": options,
            "checkpoint": {"tool": "task_checkpoint_get", "task_id": task_id},
            "remaining_budget": context["remaining_budget"],
        }
        # Volatile remaining seconds/call counts are not a new escalation event.
        fingerprint = canonical(
            {
                k: v
                for k, v in material.items()
                if k
                in (
                    "task_id",
                    "task_epoch",
                    "role",
                    "reason",
                    "operations",
                    "allowed_recovery",
                    "unknowns",
                )
            }
        )
        control = self.control(task_id)
        if role == "fast" and ["fast", fingerprint] in control["attempts"]:
            role = "teacher"
            material.update(role=role, reason="unchanged_after_fast")
        if role == "teacher" and ["teacher", fingerprint] in control["attempts"]:
            role = "operator"
            material.update(role=role, reason="unchanged_after_teacher")
        configured = (
            role not in ("fast", "teacher") or getattr(self.runtime.profile.agent, role) is not None
        )
        if control["request_id"]:
            prior = self.store.get("assistance_request", control["request_id"])
            if prior["fingerprint"] == fingerprint and prior["role"] == role:
                return {
                    **prior,
                    "remaining_budget": material["remaining_budget"],
                    "configured": configured,
                }
        result = {
            **material,
            "id": uid("assistance"),
            "fingerprint": fingerprint,
            "configured": configured,
            "status": "pending" if role in ("fast", "teacher", "operator") else role,
            "created_at": time.time(),
        }
        self.store.put("assistance_request", result["id"], result)
        control["request_id"] = result["id"]
        self.save_control(control)
        self.store.event(
            "agent.assistance_requested",
            {k: result[k] for k in ("id", "task_id", "task_epoch", "role", "reason", "configured")},
        )
        return result

    def start(self, task_id, role, binding):
        control = self.control(task_id)
        if control["active_session"]:
            prior = self.store.get("agent_session", control["active_session"])
            if prior["status"] == "running":
                raise HarnessError("agent_busy", "One model decision already owns this task")
        record = self.runtime.task_memory._load(task_id)
        if record["archived"] or record["execution"]["status"] != "active":
            raise HarnessError("task_closed", "Task is not available to a model")
        self.runtime.task_memory.check_budget(record)
        session = {
            "id": uid("session"),
            "task_id": task_id,
            "task_epoch": record["execution"]["epoch"],
            "start_epoch": record["execution"]["epoch"],
            "role": role,
            "binding": binding,
            "status": "running",
            "native_session_id": None,
            "started_at": time.time(),
            "usage": None,
            "request_fingerprint": self.request(task_id)["fingerprint"],
            "tool_calls": 0,
            "client_events": 0,
        }
        self.store.put("agent_session", session["id"], session)
        control["active_session"] = session["id"]
        self.save_control(control)
        self.store.event("agent.session_started", session)
        return session

    def advance(self, session_id):
        session = self.store.get("agent_session", session_id)
        record = self.runtime.task_memory._load(session["task_id"])
        # Only the Runtime that accepted this session's operation advances its lease.
        session["task_epoch"] = record["execution"]["epoch"]
        self.store.put("agent_session", session_id, session)

    def client_event(self, session, event):
        kind = event.get("type")
        if kind == "thread.started":
            session["native_session_id"] = event.get("thread_id")
        if kind == "turn.started":
            session["native_turn_id"] = event.get("turn_id")
        if kind == "client.transport":
            session["transport"] = {k: v for k, v in event.items() if k != "type"}
        item = event.get("item", {})
        if kind == "item.completed" and item.get("type") == "mcp_tool_call":
            session["tool_calls"] += 1
            self.store.event(
                "agent.client_tool",
                {
                    "session_id": session["id"],
                    "task_id": session["task_id"],
                    "client_item_id": item.get("id"),
                    "tool": item.get("tool"),
                    "status": item.get("status"),
                },
            )
        if kind in ("turn.completed", "turn.failed"):
            session["usage"] = event.get("usage")
        # Ignore reasoning and arbitrary client filesystem/command payloads.
        if kind in ("thread.started", "turn.started", "turn.completed", "turn.failed", "error",
                    "client.transport"):
            self.store.event(
                "agent.client_event",
                {
                    "session_id": session["id"],
                    "type": kind,
                    "native_session_id": session["native_session_id"],
                    "usage": session["usage"],
                    "native_turn_id": session.get("native_turn_id"),
                    "error": event.get("error") or event.get("message"),
                },
            )
        session["client_events"] += 1

    def finish(self, session, result, *, elapsed_s):
        prior = self.store.get("agent_session", session["id"])
        if prior["status"] != "running":
            return prior
        record = self.runtime.task_memory._load(session["task_id"])
        session.update(
            status="completed" if result.get("completed") else "disconnected",
            ended_at=time.time(),
            elapsed_s=elapsed_s,
            returncode=result.get("returncode"),
            message=result.get("message", "")[:2000],
        )
        session["task_epoch"] = prior["task_epoch"]
        if session["task_epoch"] != record["execution"]["epoch"]:
            session["status"] = "superseded"
        if session["usage"]:
            self.runtime.task_memory.record_usage(
                session["task_id"],
                session["usage"],
                client=session["binding"]["client"],
                model=session["binding"].get("model") or "client_default_unreported",
            )
        rows = self.store.db.execute(
            "SELECT body FROM events WHERE kind='tool.finished' AND at>=?", (session["started_at"],)
        )
        tool_ms = sum(
            e.get("duration_ms", 0)
            for e in (json.loads(row[0]) for row in rows)
            if e.get("agent_session_id") == session["id"]
        )
        session["metrics"] = {
            "tool_ms": tool_ms,
            "client_and_transport_ms": max(0, elapsed_s * 1000 - tool_ms),
            "queue_ms": 0,
            "meaning": "client/transport residual, not measured model-only latency",
        }
        self.store.put("agent_session", session["id"], session)
        control = self.control(session["task_id"])
        if control["active_session"] == session["id"]:
            control["active_session"] = None
        self.save_control(control)
        if session["status"] == "completed":
            item = [session["role"], session["request_fingerprint"]]
            if item not in control["attempts"]:
                control["attempts"].append(item)
        self.save_control(control)
        self.store.event("agent.session_finished", session)
        return session

    def status(self, task_id):
        record = self.runtime.task_memory.get(
            TaskCheckpointGet(task_id=task_id, include_archived=True)
        )
        sessions = [s for s in self.store.all("agent_session") if s["task_id"] == task_id]
        return {
            "task_id": task_id,
            "checkpoint": record,
            "latest_session": sessions[-1] if sessions else None,
            "assistance": self.request(task_id),
        }

    async def claim(self, args):
        request = self.store.get("assistance_request", args.request_id)
        record = self.runtime.task_memory._load(args.task_id)
        if (
            request["task_id"] != args.task_id
            or record["archived"]
            or args.task_epoch != record["execution"]["epoch"]
        ):
            raise HarnessError("stale_assistance", "Claim differs from current task/epoch")
        if record.get("task_result") or record["execution"]["status"] == "cancelled":
            raise HarnessError("task_closed", "Cancelled task cannot be claimed")
        control = self.control(args.task_id)
        if control["request_id"] != args.request_id:
            raise HarnessError("stale_assistance", "Request was replaced")
        record["execution"].update(
            status="operator",
            epoch=args.task_epoch + 1,
            operator=args.operator,
            operator_request=args.request_id,
        )
        self.runtime.task_memory._write(record)
        for identity in record["operation_ids"]:
            op = self.store.get("operation", identity)
            if op["status"] not in TERMINAL:
                await self.runtime.operations.cancel(identity)
        request.update(
            status="claimed", operator=args.operator, claim_epoch=record["execution"]["epoch"]
        )
        self.store.put("assistance_request", request["id"], request)
        return {k: request[k] for k in ("id", "task_id", "operator", "claim_epoch", "status")}

    def give_back(self, args):
        record = self.runtime.task_memory._load(args.task_id)
        state = record["execution"]
        if (
            state["status"] != "operator"
            or state.get("operator") != args.operator
            or state.get("operator_request") != args.request_id
            or state["epoch"] != args.task_epoch
        ):
            raise HarnessError("stale_assistance", "Return differs from current operator/epoch")
        unknown = any(
            op["status"] == "unknown" or op["error_code"] == "verification_unknown"
            for op in self.runtime.task_memory._operations(record["operation_ids"])
        )
        if args.disposition == "resume" and unknown:
            raise HarnessError(
                "unresolved_effects", "Unknown effects require handoff; return cannot replay them"
            )
        state.update(
            status="active" if args.disposition == "resume" else "cancelled",
            epoch=state["epoch"] + 1,
        )
        self.runtime.task_memory._write(record)
        request = self.store.get("assistance_request", args.request_id)
        request.update(status="returned", disposition=args.disposition, note=args.note)
        self.store.put("assistance_request", args.request_id, request)
        if args.disposition == "handoff":
            self.runtime.registry.release(args.task_id)
        return {
            "task_id": args.task_id,
            "task_epoch": state["epoch"],
            "status": state["status"],
            "unknowns_retained": unknown,
        }
