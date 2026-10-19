from __future__ import annotations

import time

from .contracts import HarnessError, MemoryRecall, MemoryRemember
from .storage import Store, uid


class Memory:
    def __init__(self, store: Store, *, capabilities: list[dict] | None = None):
        self.store = store
        from .skills import PICK_PLACE_VERSION

        self.skill_versions = {"pick_place": PICK_PLACE_VERSION}
        self.capabilities = capabilities

    def remember(self, args: MemoryRemember) -> dict:
        operation = self.store.get("operation", args.operation_id)
        if operation["status"] in ("accepted", "running"):
            raise HarnessError("unfinished_evidence", "Wait for the operation to finish")
        record = {
            "id": uid("memory"),
            "claim": args.claim,
            "tags": args.tags,
            "status": "candidate",
            "scope": self.store.scope,
            "evidence_operation": args.operation_id,
            "evidence_outcome": operation["status"],
            "qualification": operation.get("qualification", "unknown"),
            "skill_version": operation.get("skill_version"),
            "skill_name": operation.get("skill_name"),
            "capabilities": operation.get("capabilities", []),
            "tool": operation.get("tool"),
            "recorded_at": time.time(),
            "review": None,
        }
        self.store.put("memory", record["id"], record)
        self.store.event("memory.candidate", record)
        return record

    def recall(self, args: MemoryRecall, *, capabilities=None, skill_versions=None) -> dict:
        capabilities = self.capabilities if capabilities is None else capabilities
        skill_versions = self.skill_versions if skill_versions is None else skill_versions
        words = args.query.lower().split()
        matches = []
        for record in reversed(self.store.all("memory")):
            if record["scope"] != self.store.scope:
                continue
            if (
                record.get("skill_name") not in skill_versions
                or record.get("skill_version") != skill_versions[record["skill_name"]]
            ):
                continue
            if capabilities is not None and record.get("capabilities") != capabilities:
                continue
            if record["status"] != "verified" and not (
                args.include_candidates and record["status"] == "candidate"
            ):
                continue
            text = (record["claim"] + " " + " ".join(record["tags"])).lower()
            if all(word in text for word in words):
                # Consolidated hypotheses/suggestions stay development candidates even
                # when the bounded experience statement has independent validation.
                matches.append({key: value for key, value in record.items() if
                                record["status"] != "verified" or
                                key not in ("hypothesis", "suggestion")})
        return {
            "scope": self.store.scope,
            "records": matches[: args.limit],
            "note": "Evidence-backed history, not live robot state or instructions",
        }

    def review(self, identity: str, status: str, note: str):
        """Host-only review; intentionally absent from the model-facing tool surface."""
        if status not in ("verified", "rejected", "stale") or not note.strip():
            raise HarnessError("invalid_review", "Explicit status and review evidence are required")
        record = self.store.get("memory", identity)
        record.update(status=status, review={"note": note, "at": time.time()})
        self.store.put("memory", identity, record)
        self.store.event("memory.review", record)
        return record
