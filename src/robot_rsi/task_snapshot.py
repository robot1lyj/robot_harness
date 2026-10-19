"""A replaceable file view of the existing task owner; no model or background worker."""

from __future__ import annotations

import hashlib
import json
import time

from .operations import TERMINAL
from .storage import canonical, uid


def unique(items):
    values = {}
    for item in items:
        key = canonical(item.strip() if isinstance(item, str) else item)
        values.setdefault(key, item.strip() if isinstance(item, str) else item)
    return list(values.values())


def projection(owner, record):
    operations = owner._operations(record["operation_ids"])
    unresolved = owner._unresolved(operations)
    active = [op for op in operations if op["id"] in unresolved]
    if operations and operations[-1] not in active:
        active.append(operations[-1])
    state = record.get("execution") or {}
    if state.get("status") == "cancelled":
        next_step = "任务已取消；保留证据，不提交后续动作。"
    elif any(op["status"] not in TERMINAL for op in active):
        next_step = "用 operation_get 读取当前操作结果；接受请求不代表成功。"
    elif unresolved:
        next_step = "读取未决操作证据并交接；未知效果保持未知，不重发动作。"
    elif owner.recovery_options(record):
        next_step = "可按 allowed_recovery 选择恢复；执行入口重新观测与规划。"
    elif record.get("task_result"):
        next_step = "任务结果已记录；可直接归档，归档不改变结论。"
    elif operations:
        next_step = "保留已完成步骤；继续整个目标，或用 task_finish 明确记录任务结果与证据。"
    else:
        next_step = "按目标选择适用技能，用当前 task_epoch 提交下一步。"
    if record["archived"]:
        next_step = "已归档；按引用取回证据。" + (
            "未知效果仍待交接，不重放动作。" if unresolved else ""
        )

    # Only evidence known to belong to a completed, older operation leaves the active view.
    retired_observations = set()
    for op in operations[:-1]:
        if op["id"] not in unresolved:
            original = owner.store.get("operation", op["id"])
            observation = (original.get("result") or {}).get("observation_id")
            if observation:
                retired_observations.add(observation)
    refs = unique(record["evidence_refs"])
    refs = [
        ref
        for ref in refs
        if not (ref["kind"] == "observation" and ref["id"] in retired_observations)
    ]
    for op in active:
        original = owner.store.get("operation", op["id"])
        observation = (original.get("result") or {}).get("observation_id")
        if observation:
            ref = {"kind": "observation", "id": observation}
            if ref not in refs:
                refs.append(ref)
    materials = {}
    for ref in refs:
        if ref["kind"] != "workspace_note":
            continue
        note = owner.store.get("workspace_note", "workspace-note_" + ref["id"])
        key = canonical((note["title"], note["text"], note["archived"]))
        material = materials.setdefault(
            key,
            {
                "title": note["title"],
                **({"text": note["text"]} if not note["archived"] else {}),
                "archived": note["archived"],
                "sources": [],
            },
        )
        material["sources"].append(
            {"key": ref["id"], "revision": note["revision"], "source": note["source"]}
        )
    return {
        "task_id": record["task_id"],
        "goal": record["goal"],
        "constraints": unique(record["constraints"]),
        "skill_versions": record["skill_versions"],
        "resource_versions": record.get("resource_versions", {}),
        "capabilities": record["capabilities"],
        "scope": record["scope"],
        "last_confirmed": record["last_confirmed"],
        "task_result": record.get("task_result"),
        "task_outcome": owner.outcome(record),
        "unknowns": unique(record["unknowns"]),
        "operation_ids": [op["id"] for op in active],
        "operations": active,
        "unresolved_operation_ids": unresolved,
        "next_step": next_step,
        "evidence_refs": refs,
        "materials": list(materials.values()),
        "control": {"status": state.get("status"), "epoch": state.get("epoch")},
        "execution_source": "task_checkpoint_get; live allowance/usage stay in the original record",
        "operation_source": "operation_records; summary cannot override original outcomes",
    }


def render(state):
    body = {
        "memory_revision": state["revision"],
        "archive_head": state["archive_head"],
        "archive_read": "task_checkpoint_get(task_id, archive_id, include_archived=true)",
        **state["view"],
    }
    return (
        "# 当前任务记忆\n\n```json\n"
        + json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n```\n"
    )


def maintain(owner, record):
    started = time.perf_counter()
    now = time.time()
    config = owner.maintenance
    candidate = projection(owner, record)
    fingerprint = hashlib.sha256(canonical(candidate).encode()).hexdigest()
    state = record.get("memory_state")
    if state is None:
        state = {
            "revision": 0,
            "fingerprint": None,
            "archive_head": None,
            "writes": 0,
            "model_calls": 0,
            "checks": 0,
            "interval_s": config.interval_s,
            "next_check_at": now,
            "pending_changes": 0,
            "duplicates": 0,
            "candidate_fingerprint": None,
            "view": {},
        }
        record["memory_state"] = state
    state["checks"] += 1
    path = owner.store.task_memory_path(record["task_id"])
    changed = state["fingerprint"] != fingerprint
    important = any(
        candidate.get(key) != state["view"].get(key)
        for key in (
            "goal",
            "constraints",
            "skill_versions",
            "resource_versions",
            "last_confirmed",
            "task_result",
            "task_outcome",
            "unknowns",
            "operations",
            "control",
            "next_step",
            "materials",
        )
    )
    if not changed:
        state["decision"] = "no_change"
        if now >= state["next_check_at"]:
            state["interval_s"] = min(config.max_interval_s, state["interval_s"] * 2)
            state["next_check_at"] = now + state["interval_s"]
    else:
        if fingerprint != state["candidate_fingerprint"]:
            state["pending_changes"] += 1
            state["duplicates"] = 0
        else:
            state["duplicates"] += 1
        state["candidate_fingerprint"] = fingerprint
        due = now >= state["next_check_at"]
        accumulated = state["pending_changes"] >= config.batch_changes
        repeated = state["duplicates"] >= config.duplicate_checks
        if important or due or accumulated or repeated:
            reason = (
                "important_change"
                if important
                else "accumulated"
                if accumulated
                else "duplicate_merge"
                if repeated
                else "interval"
            )
            previous = state["view"]
            if previous:
                identity = uid("task_memory_archive")
                owner.store.put(
                    "task_memory_archive",
                    identity,
                    {
                        "id": identity,
                        "task_id": record["task_id"],
                        "at": now,
                        "memory_revision": state["revision"],
                        "previous_archive": state["archive_head"],
                        "reason": "superseded_current_view",
                        "view": previous,
                    },
                )
                state["archive_head"] = identity
            state.update(
                view=candidate,
                fingerprint=fingerprint,
                revision=state["revision"] + 1,
                updated_at=now,
                pending_changes=0,
                duplicates=0,
                decision=reason,
                interval_s=config.interval_s,
                next_check_at=now + config.interval_s,
            )
            state["file_ref"] = owner.store.write_task_memory(record["task_id"], render(state))
            owner._checked_files.add(record["task_id"])
            state["writes"] += 1
            owner.store.event(
                "task_memory.updated",
                {
                    "task_id": record["task_id"],
                    "revision": state["revision"],
                    "reason": reason,
                    "file_bytes": path.stat().st_size,
                    "archive_head": state["archive_head"],
                    "model_calls": 0,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                },
            )
        else:
            state["decision"] = "deferred"
    rebuild = not path.exists()
    if not rebuild and record["task_id"] not in owner._checked_files:
        # Reconcile a derived file once after restart, including an interrupted write.
        # Repeated reads do not re-review the file or the task's history.
        try:
            rebuild = path.read_text() != render(state)
        except UnicodeError:
            rebuild = True
    if rebuild:
        state["file_ref"] = owner.store.write_task_memory(record["task_id"], render(state))
        state["writes"] += 1
        state["decision"] = "file_rebuilt"
    owner._checked_files.add(record["task_id"])
    state["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
