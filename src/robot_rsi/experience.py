"""Thin host-only experience, evaluation attestation and release records over Store."""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

from pydantic import Field

from .contracts import Contract, HarnessError
from .registry import Manifest
from .storage import canonical, uid


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def require(condition, message):
    if not condition:
        raise HarnessError("invalid_experience_evidence", message)


def package_identity(directory):
    directory = Path(directory)
    manifest = Manifest.model_validate_json((directory / "skill.json").read_text())
    content = {"skill.json": manifest.model_dump(),
               "SKILL.md": (directory / "SKILL.md").read_text()}
    if (directory / "skill.py").exists():
        content["skill.py"] = (directory / "skill.py").read_text()
    return {"name": manifest.name, "version": manifest.version, "digest": digest(content)}


class Lesson(Contract):
    claim: str = Field(min_length=1, max_length=1600)
    conditions: str = Field(min_length=1, max_length=1000)
    invalidated_by: str = Field(min_length=1, max_length=1000)
    hypothesis: str = Field(default="", max_length=1000)
    suggestion: str = Field(default="", max_length=1000)
    counterexamples: list[str] = Field(default_factory=list, max_length=8)


class Experience:
    def __init__(self, store):
        self.store = store

    def export(self, task_id):
        task = self.store.get("task_checkpoint", "task-checkpoint_" + task_id)
        require(task["archived"] or task.get("execution", {}).get("status") in
                ("finished", "cancelled"), "Select an ended task")
        operations = [self.store.get("operation", key) for key in task["operation_ids"]]
        require(operations and all(op["status"] not in ("accepted", "running") for op in operations),
                "Select terminal operation evidence")
        body = {"schema_version": 1, "source_scope": self.store.scope,
                "task": {key: task.get(key) for key in
                         ("task_id", "goal", "constraints", "skill_versions", "task_result", "archived")},
                "operations": operations}
        return {"evidence": body, "sha256": digest(body)}

    def consolidate(self, selected, lesson: Lesson):
        require(self.store.scope["role"] == "development", "Consolidate in development scope")
        body = selected["evidence"]
        require(digest(body) == selected["sha256"], "Selected snapshot digest changed")
        operations = body["operations"]
        require(0 < len(operations) <= 16 and all(op["status"] not in
                ("accepted", "running") for op in operations), "Terminal evidence required")
        task = body["task"]
        require(task.get("archived") or task.get("task_result"), "Select ended task evidence")
        first = operations[0]
        require(task["skill_versions"].get(first["skill_name"]) == first["skill_version"],
                "Source operation must match the task's fixed version")
        require(all(op.get("task_id") == task["task_id"] and
                    op.get("skill_name") == first["skill_name"] and
                    op.get("skill_version") == first["skill_version"] and
                    op.get("capabilities") == first.get("capabilities") for op in operations),
                "Do not combine different task/version/binding evidence")
        key = digest({"claim": " ".join(lesson.claim.lower().split()),
                      "conditions": lesson.conditions, "invalidated_by": lesson.invalidated_by,
                      "skill": first["skill_name"], "version": first["skill_version"],
                      "capabilities": first.get("capabilities", [])})
        identity = "memory_" + key
        existing = next((r for r in self.store.all("memory") if r["id"] == identity), None)
        known_source = existing and next((ref for ref in existing["sources"]
                                         if ref["sha256"] == selected["sha256"]), None)
        counterexamples = list(dict.fromkeys((existing or {}).get("counterexamples", [])
                                             + lesson.counterexamples))
        if known_source and counterexamples == existing["counterexamples"]:
            return {"changed": False, "record": existing}
        require(len(counterexamples) <= 8, "Select a narrower counterexample set")
        require(not existing or known_source or len(existing["sources"]) < 16,
                "Select a narrower evidence set")
        # Facts remain immutable selected snapshots; hypotheses never replace them.
        source = known_source or {"scope": body["source_scope"], "task_id": task["task_id"],
                  "operation_ids": [op["id"] for op in operations],
                  "sha256": selected["sha256"], "artifact_id":
                  self.store.artifact(canonical(body).encode(), "application/json")}
        record = existing or {
            "id": identity, "scope": self.store.scope, "status": "candidate",
            "claim": lesson.claim, "conditions": lesson.conditions,
            "invalidated_by": lesson.invalidated_by, "tags": ["consolidated"],
            "hypothesis": lesson.hypothesis, "suggestion": lesson.suggestion,
            "counterexamples": lesson.counterexamples, "sources": [],
            "evidence_operation": first["id"], "evidence_outcome": first["status"],
            "skill_name": first["skill_name"], "skill_version": first["skill_version"],
            "capabilities": first.get("capabilities", []),
            "qualification": first.get("qualification", "unknown"),
            "tool": first.get("tool"), "recorded_at": time.time(), "review": None,
        }
        # A new source/contradiction invalidates the previous consolidated validation.
        record.update(status="candidate", review=None, counterexamples=counterexamples,
                      revision=record.get("revision", 0) + 1)
        if not known_source:
            record["sources"].append(source)
        with self.store.transaction():
            self.store.put("memory", identity, record)
            self.store.event("experience.consolidated", {"id": identity, "source": source})
        return {"changed": True, "record": record}

    def verify(self, identity, evaluation):
        require(self.store.scope["role"] == "development", "Verify in development scope")
        decision = assess(evaluation)
        record = self.store.get("memory", identity)
        require(decision["passed"] and
                {"id": identity, "revision": record["revision"]} in evaluation["experience_links"],
                "Independent comparison must support this experience revision")
        # Verification is bounded to the selected claim/conditions, never the cause hypothesis.
        record.update(status="verified", review={"evaluation": evaluation["id"],
                      "report_sha256": evaluation["report_sha256"], "at": time.time(),
                      "validated_fields": ["claim", "conditions"],
                      "qualification": "synthetic_fixed_suite_only"})
        self.store.put("memory", identity, record)
        self.store.event("experience.verified", {"id": identity, "review": record["review"]})
        return record


def assess(evaluation):
    """Check comparison integrity/costs. Scoring truth stays with the host-selected evaluator."""
    require(evaluation["scope"]["role"] == "evaluation", "Independent evaluation scope required")
    report = evaluation["comparison"]
    require(digest(report) == evaluation["comparison_sha256"], "Comparison changed")
    require(digest(report["frozen"]) == evaluation["frozen_sha256"], "Frozen conditions changed")
    if report.get("installation"):
        require(report["installation"] == report["frozen"].get("installation"),
                "Compared installation must match frozen baseline")
    groups = {mode: {row["case"]: row for row in report["rows"] if row["mode"] == mode}
              for mode in ("baseline_independent", "baseline_assisted", "candidate_independent")}
    baseline, assisted, candidate = groups.values()
    require(baseline and baseline.keys() == assisted.keys() == candidate.keys(),
            "Compare identical fixed scenarios in all three modes")
    require(len(report["rows"]) == len(baseline) * 3, "Duplicate or unrecognized comparison rows")
    budgets = report["frozen"]["budgets"]
    require(set(budgets) == {"commands", "grasp_attempts", "recoveries", "duration_s", "tool_calls"},
            "Fixed total action/recovery/time conditions required")
    for rows in groups.values():
        artifacts = {canonical(row["skill"]) for row in rows.values()}
        require(len(artifacts) == 1, "Each mode must run one immutable version")
        for row in rows.values():
            require(row["budgets"] == budgets and row["outcome"] in ("succeeded", "failed", "unknown"),
                    "Conditions/outcomes must match fixed suite")
            require(all(0 <= row["cost"][key] <= cap for key, cap in budgets.items()),
                    "Comparison exceeded its original budget")
            require(row["scope"]["role"] == "evaluation", "Evaluation input scope required")
            require(row["mode"] == "baseline_assisted" or
                    row["cost"]["assistance_decisions"] == row["cost"]["model_calls"] == 0,
                    "Independent mode must not hide assistance")
    require(next(iter(baseline.values()))["skill"] == next(iter(assisted.values()))["skill"],
            "Assisted baseline must use identical version")
    improved = [key for key in baseline if baseline[key]["target_gap"] and
                baseline[key]["outcome"] != "succeeded" and candidate[key]["outcome"] == "succeeded"]
    regressions = [key for key in baseline if
                   (baseline[key]["outcome"] == "succeeded" and candidate[key]["outcome"] != "succeeded") or
                   (baseline[key]["outcome"] != "unknown" and candidate[key]["outcome"] == "unknown")]
    require(all(baseline[k]["target_gap"] == candidate[k]["target_gap"] == assisted[k]["target_gap"]
                for k in baseline), "Target gap selection changed between modes")
    return {"passed": bool(improved) and not regressions, "improved": improved,
            "regressions": regressions,
            "baseline": next(iter(baseline.values()))["skill"],
            "candidate": next(iter(candidate.values()))["skill"]}


def import_evaluation(store, report, experience_ids=()):
    require(store.scope["role"] == "evaluation", "Import report in evaluation scope")
    links = []
    for reference in experience_ids:
        identity, separator, revision = reference.rpartition("@")
        require(separator and identity and revision.isdigit() and int(revision) > 0,
                "Link selected experience as id@revision")
        links.append({"id": identity, "revision": int(revision)})
    # Raw observations/scenario evidence remain in evaluation storage, never release storage.
    report_hash = digest(report)
    identity = "evaluation_" + report_hash
    existing = next((r for r in store.all("evaluation_report") if r["id"] == identity), None)
    if existing:
        require(existing["experience_links"] == links, "Evaluation linkage is immutable")
        return existing
    comparison = {"frozen": report["frozen"], "qualification": report["qualification"],
                  "installation": report.get("installation"),
                  "rows": [{key: row[key] for key in
                            ("case", "mode", "outcome", "target_gap", "cost", "budgets", "skill", "scope")}
                           for row in report["rows"]]}
    record = {"id": identity, "scope": store.scope, "report_sha256": report_hash,
              "comparison": comparison, "comparison_sha256": digest(comparison),
              "frozen_sha256": digest(report["frozen"]),
              "experience_links": links}
    decision = assess(record)
    artifact = store.artifact(canonical(report).encode(), "application/json")
    record.update(artifact_id=artifact, decision=decision, at=time.time())
    with store.transaction():
        store.put("evaluation_report", identity, record)
        store.event("evaluation.recorded", {"id": identity, "report_sha256": report_hash})
    return record


class Releases:
    def __init__(self, store, registry):
        self.store, self.registry = store, registry

    def publish(self, evaluation, directory):
        require(self.store.scope["role"] == "runtime", "Publish to a runtime workspace")
        from .installation import installation_identity

        installation = installation_identity()
        require((evaluation["comparison"].get("installation") or {}).get("digest") ==
                installation["digest"], "Compare again using this installed runtime/dependencies")
        decision = assess(evaluation)
        artifact = package_identity(directory)
        require(artifact == decision["candidate"], "Candidate does not match evaluated package")
        current = self.registry.describe(artifact["name"])
        prior = {key: current[key] for key in ("name", "version", "digest")}
        require(prior == decision["baseline"], "Workspace baseline changed; compare again")
        require(not any(r["status"] == "pending" and r["candidate"]["name"] == artifact["name"]
                        for r in self.store.all("release")), "Reconcile interrupted release first")
        record = {"id": uid("release"), "scope": self.store.scope, "prior": prior,
                  "action": "publish", "installation_digest": installation["digest"],
                  "candidate": artifact, "evaluation": {"id": evaluation["id"],
                    "scope": evaluation["scope"], "report_sha256": evaluation["report_sha256"]},
                  "decision": decision, "status": "pending", "at": time.time()}
        if not decision["passed"]:
            record["status"] = "rejected"
        else:
            self.store.put("release", record["id"], record)
            try:
                self.registry.register(directory)
                self.registry.enable(artifact["name"], artifact["version"])
            except HarnessError as error:
                record.update(status="blocked", error=error.payload())
            else:
                record["status"] = "published"
        self.store.put("release", record["id"], record)
        self.store.event("release." + record["status"], record)
        return record

    def rollback(self, release_id):
        release = self.store.get("release", release_id)
        require(release["status"] == "published", "Select a published release")
        from .installation import installation_identity

        require(release.get("installation_digest") == installation_identity()["digest"],
                "Restore the original installed image before rollback; changed modules need comparison")
        if release.get("rollback_id"):
            previous = self.store.get("release", release["rollback_id"])
            if previous["status"] not in ("not_applied", "blocked"):
                return previous
        current = self.registry.describe(release["candidate"]["name"])
        require(all(current[k] == release["candidate"][k] for k in ("version", "digest")),
                "Workspace changed since this release")
        old = self.registry.describe(release["prior"]["name"], release["prior"]["version"])
        require(old["digest"] == release["prior"]["digest"], "Prior immutable package changed")
        record = {"id": uid("release"), "scope": self.store.scope, "status": "pending",
                  "action": "rollback", "installation_digest": release.get("installation_digest"),
                  "from_release": release_id, "prior": release["candidate"],
                  "candidate": release["prior"], "evaluation": release["evaluation"],
                  "at": time.time()}
        release["rollback_id"] = record["id"]
        with self.store.transaction():
            self.store.put("release", record["id"], record)
            self.store.put("release", release_id, release)
            self.store.event("release.rollback_intent", record)
        try:
            self.registry.enable(old["name"], old["version"])
        except HarnessError as error:
            record.update(status="blocked", error=error.payload())
            self.store.put("release", record["id"], record)
            raise
        record["status"] = "rolled_back"
        self.store.put("release", record["id"], record)
        self.store.event("release.rolled_back", record)
        return record

    def reconcile(self, identity=None):
        records = ([self.store.get("release", identity)] if identity else self.store.all("release"))
        for record in records:
            require(record["scope"] == self.store.scope, "Release belongs to another workspace")
            if record["status"] != "pending":
                continue
            try:
                current = self.registry.describe(record["candidate"]["name"])
                current = {k: current[k] for k in ("name", "version", "digest")}
            except HarnessError:
                current = None
            if current == record["candidate"]:
                status = "rolled_back" if record.get("action") == "rollback" else "published"
            elif current == record["prior"]:
                status = "not_applied"
            else:
                status = "conflict"
            record.update(status=status, reconciliation={"binding": current, "at": time.time(),
                          "action": "inspected original binding; no enable or operation replay"})
            self.store.put("release", record["id"], record)
            self.store.event("release.reconciled", record)
        return records
