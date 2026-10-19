"""Bounded context assembled from existing scoped owners, without loading episode history."""

from __future__ import annotations

import hashlib
import time

from .contracts import HarnessError, MemoryRecall, SkillQuery
from .scene_bindings import BindingRead
from .storage import canonical

INSTRUCTIONS = "Use the current task facts below; unchanged blocks remain valid. Read referenced details only when needed."


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def block_view(runtime, args, context):
    """Whole-block delivery projection, never another source of task facts."""
    checkpoint = context["checkpoint"]
    execution = checkpoint.get("execution") or {}
    blocks = {
        "instructions": INSTRUCTIONS,
        "workspace": context["workspace"],
        "scene_binding": context["scene_binding"],
        "task": {
            "task_id": args.task_id,
            "goal": checkpoint["goal"],
            "constraints": checkpoint["constraints"],
            "skill_versions": checkpoint["skill_versions"],
            "resource_versions": checkpoint.get("resource_versions", {}),
            "execution": {k: v for k, v in execution.items()
                          if k not in ("tool_calls", "model_usage")},
            "handoff": context["handoff"],
        },
        "skills": {key: context[key] for key in
                   ("skill_candidates", "selected_skill", "capability_gap", "omitted")},
        "experiences": context["memories"],
        "results": {
            **{key: value for key, value in checkpoint.items()
               if key not in ("goal", "constraints", "skill_versions", "execution",
                              "remaining_budget", "memory", "updated_at", "resource_versions")},
            "allowed_recovery": context["allowed_recovery"],
        },
    }
    identity = digest([runtime.store.scope, args.task_id, args.query, args.selected_skill])
    versions = {key: digest(value) for key, value in blocks.items()}
    prior = args.delivered or {}
    full = prior.get("identity") != identity
    changed = {key: value for key, value in blocks.items()
               if full or prior.get(key) != versions[key]}
    result = {
        "task_id": args.task_id, "full": full, "blocks": changed,
        "delivered": {"identity": identity, **versions},
        "unchanged": [key for key in blocks if key not in changed],
        "details": {"tool": "task_checkpoint_get", "arguments": {"task_id": args.task_id}},
    }
    result["metrics"] = {"block_bytes": len(canonical(changed).encode()),
                         "full_block_bytes": len(canonical(blocks).encode()), "model_tokens": None}
    return result


def reference_context(runtime, args, context):
    """A delivery entry, not shortened task facts; details stay in their original owners."""
    full = block_view(runtime, args.model_copy(update={"delivered": None}), context)
    reads = [
        {"tool": "workspace_read", "arguments": {}},
        {"tool": "task_checkpoint_get", "arguments": {"task_id": args.task_id}},
        {"tool": "skill_describe", "arguments": {"name": args.selected_skill}}
        if args.selected_skill else {"tool": "skills_list", "arguments": {}},
    ]
    entry = {
        "instruction": "Read these original records before deciding; apply all constraints, unknowns, "
        "epoch/pins/budget. Never replay unknown actions. Finish the whole goal with task_finish, then archive.",
        "read_first": reads,
        "source_version": digest(full["delivered"]),
    }
    if not args.blocks:
        result = {"task_id": args.task_id, "details_required": True, **entry,
                  "metrics": {"required_bytes": context["metrics"]["returned_bytes"],
                              "limit_bytes": runtime.profile.task_limits.context_bytes,
                              "returned_bytes": 0, "model_tokens": None}}
        for _ in range(3):
            result["metrics"]["returned_bytes"] = len(canonical(result).encode())
        return result
    # The cursor acknowledges the entry only, never pretends that referenced facts were sent.
    delivered = {"identity": digest([full["delivered"]["identity"], "references"]),
                 "required_details": digest(entry)}
    prior = args.delivered or {}
    is_full = prior.get("identity") != delivered["identity"]
    changed = is_full or prior.get("required_details") != delivered["required_details"]
    return {
        "task_id": args.task_id, "full": is_full,
        "blocks": {"required_details": entry} if changed else {}, "delivered": delivered,
        "unchanged": [] if changed else ["required_details"],
        "metrics": {"block_bytes": len(canonical({"required_details": entry} if changed else {}).encode()),
                    "full_block_bytes": full["metrics"]["full_block_bytes"], "model_tokens": None},
    }


def build_task_context(runtime, args):
    started = time.perf_counter()
    bindings = runtime.task_bindings(args.task_id)
    if args.selected_skill:
        selected = runtime.select_skill(args.task_id, args.selected_skill)
        if not selected["available"]:
            raise HarnessError("unsupported", "Selected skill capabilities are unavailable")
    else:
        selected = None
    checkpoint = runtime.task_memory.context(args.task_id)
    query = args.query if args.query is not None else checkpoint["goal"][:200]
    cards = runtime.registry.cards(SkillQuery(query=query, limit=3), bindings=bindings)
    memories = runtime.recall_memory(MemoryRecall(query=query, limit=3), args.task_id)
    record = runtime.task_memory._load(args.task_id)
    result = {
        "scene_binding": runtime.scenes.read(BindingRead(), args.task_id),
        "workspace": runtime.workspace_brief(args.task_id, bindings),
        "checkpoint": checkpoint,
        "skill_candidates": cards["skills"],
        "selected_skill": selected,
        "capability_gap": None
        if selected or cards["skills"]
        else {
            "reason": "no_matching_skill",
            "query": query,
            "next": "clarify the goal or hand off with this checkpoint",
        },
        "memories": memories["records"],
        "allowed_recovery": runtime.task_memory.recovery_options(record),
        "handoff": {
            "task_id": args.task_id,
            "task_epoch": record.get("execution", {}).get("epoch"),
            "checkpoint_tool": "task_checkpoint_get",
            "evidence_tool": "operation_get",
        },
        "omitted": [],
        "metrics": {"returned_bytes": 0, "retrieval_ms": 0.0, "model_tokens": None},
    }
    limit = runtime.profile.task_limits.context_bytes
    # Remove whole optional records only; never truncate a constraint, unknown or selected contract.
    for field, tool in (("memories", "memory_recall"), ("skill_candidates", "skills_list")):
        if len(canonical(result).encode()) > limit and result[field]:
            result[field] = []
            result["omitted"].append({"field": field, "reason": "context_bytes", "read_with": tool})
    result["metrics"]["retrieval_ms"] = round((time.perf_counter() - started) * 1000, 3)
    for _ in range(3):
        result["metrics"]["returned_bytes"] = len(canonical(result).encode())
    if result["metrics"]["returned_bytes"] > limit:
        return reference_context(runtime, args, result)
    return block_view(runtime, args, result) if args.blocks else result
