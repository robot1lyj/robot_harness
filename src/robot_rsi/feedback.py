"""Selected issue/reply files in the original Store, without a notification service."""
from __future__ import annotations
from .feature_contracts import IssueCreate, IssueGet

import json
from typing import Literal

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import Field, model_validator

from .contracts import Contract, HarnessError, Identifier, TaskEvidence
from .installation import installation_identity
from .storage import canonical, uid






UPDATE_STATES = {"update_available", "component_not_installed", "installed_needs_validation"}


class FrameworkReply(Contract):
    id: Identifier
    issue_id: Identifier
    project_id: Identifier
    disposition: Literal["needs_information", "declined", "answered", "update_available"]
    handling: Literal["project", "extension", "framework"]
    component: str = Field(min_length=1, max_length=120)
    affected_versions: str = Field(min_length=1, max_length=120)
    target_version: str | None = None
    contracts: list[str] = Field(default_factory=list, max_length=12)
    providers: list[str] = Field(default_factory=list, max_length=12)
    skills: list[str] = Field(default_factory=list, max_length=12)
    summary: str = Field(min_length=1, max_length=300)
    details: str = Field(min_length=1, max_length=2400)
    migration: str = Field(min_length=1, max_length=1200)
    validation: str = Field(min_length=1, max_length=1200)
    provenance: Literal["simulated_fixture", "maintainer_file"]


class Feedback:
    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        self.project = runtime.profile.project
        self.installation = installation_identity()
        self.versions = {d["name"].lower().replace("_", "-"): d["version"]
                         for d in self.installation["dependencies"]}

    def _project(self):
        if self.project is None:
            raise HarnessError("project_required", "Host has not selected a project")
        return self.project.id

    def create(self, args):
        project_id = self._project()
        task = self.runtime.task_memory._load(args.source_task) if args.source_task else None
        for identity in args.operation_ids:
            operation = self.store.get("operation", identity)
            if not args.source_task or operation.get("task_id") != args.source_task:
                raise HarnessError("task_mismatch", "Issue operations must belong to source_task")
        for ref in args.evidence_refs:
            self.store.get(ref.kind, ref.id)
        record = {"id": uid("issue"), "project_id": project_id, "scope": self.store.scope,
                  **args.model_dump(), "installation_digest": self.installation["digest"],
                  "installed_versions": self.versions,
                  "source_installation_digest": task.get("execution", {}).get("installation_digest") if task else None,
                  "capabilities": task["capabilities"] if task else self.runtime.bindings.catalog(),
                  "skill_versions": task["skill_versions"] if task else self.runtime.registry.versions(),
                  "resource_versions": task.get("resource_versions", {}) if task else self.runtime.resource_versions(None)}
        self.store.put("project_issue", record["id"], record)
        return record

    def issue(self, identity):
        issue = self.store.get("project_issue", identity)
        if issue["project_id"] != self._project():
            raise HarnessError("project_mismatch", "Issue belongs to another project")
        return issue

    def import_reply(self, value):
        reply = FrameworkReply.model_validate(value)
        issue = self.issue(reply.issue_id)
        if reply.project_id != issue["project_id"] or reply.component != issue["component"]:
            raise HarnessError("reply_mismatch", "Reply project/component must match the issue")
        try:
            SpecifierSet(reply.affected_versions)
            if reply.target_version is not None:
                Version(reply.target_version)
        except (InvalidSpecifier, InvalidVersion) as exc:
            raise HarnessError("invalid_version", "Use a package version specifier and version") from exc
        if reply.disposition == "update_available" and reply.target_version is None:
            raise HarnessError("invalid_reply", "An update needs a target version")
        previous = next((r for r in self.store.all("framework_reply") if r["id"] == reply.id), None)
        body = reply.model_dump()
        if previous:
            if previous["reply"] != body:
                raise HarnessError("reply_conflict", "Replies are immutable; use a new reply ID")
            return self.state(previous)
        record = {"id": reply.id, "reply": body, "validation_evidence": None}
        self.store.put("framework_reply", reply.id, record)
        return self.state(record)

    def dependencies(self, task_id=None):
        bound = self.runtime.task_bindings(task_id)
        caps = bound.catalog()
        if not task_id:
            latest = {r["selection"]["scene_id"]: r for r in self.store.all("scene_binding")}
            for scene in latest.values():
                for name in scene["selection"]["roles"].values():
                    candidate = self.runtime.scenes.candidates.get(name)
                    if candidate:
                        caps += candidate.bindings.catalog()
        skills = self.runtime.registry.versions()
        if task_id:
            skills = self.runtime.task_memory._load(task_id)["skill_versions"]
        return caps, skills

    def relevant(self, reply, task_id=None):
        caps, skills = self.dependencies(task_id)
        names = {c["name"] for c in caps}
        providers = {c.get("provider_id") for c in caps}
        return ((not reply["contracts"] or bool(names.intersection(reply["contracts"]))) and
                (not reply["providers"] or bool(providers.intersection(reply["providers"]))) and
                (not reply["skills"] or bool(set(skills).intersection(reply["skills"]))))

    def state(self, record, task_id=None):
        reply = record["reply"]
        issue = self.issue(reply["issue_id"])
        key = reply["component"].lower().replace("_", "-")
        installed = self.versions.get(key)
        source = issue["installed_versions"].get(key)
        spec = SpecifierSet(reply["affected_versions"])
        applicable = bool(source and Version(source) in spec)
        relevant = self.relevant(reply, task_id)
        status = reply["disposition"]
        if not applicable:
            status = "version_not_applicable"
        elif not relevant:
            status = "unrelated_dependencies"
        elif reply["target_version"]:
            if installed != reply["target_version"]:
                status = "update_available" if installed else "component_not_installed"
            else:
                evidence = record.get("validation_evidence")
                caps, skills = self.dependencies(task_id)
                status = ("installed_selected_validation" if evidence and
                          evidence["installation_digest"] == self.installation["digest"] and
                          all(c in {canonical(c) for c in caps} for c in evidence["capabilities"]) and
                          all(skills.get(k) == v for k, v in evidence["skill_versions"].items())
                          else "installed_needs_validation")
        return {"issue_id": issue["id"], "reply_id": reply["id"], "status": status,
                "summary": reply["summary"], "installed_version": installed,
                "target_version": reply["target_version"], "provenance": reply["provenance"],
                "details": "issue_get(issue_id=...)"}

    def _entries(self, task_id=None, through=None):
        if not self.project:
            return [], 0
        # Existing immutable record positions supply a keyset cursor; no paging state owner.
        rows = self.store.db.execute(
            "SELECT rowid, kind, body FROM records WHERE kind IN ('project_issue', 'framework_reply') ORDER BY rowid"
        ).fetchall()
        through = through if through is not None else (rows[-1][0] if rows else 0)
        records = [(row[0], row[1], json.loads(row[2])) for row in rows if row[0] <= through]
        replies = [(seq, r) for seq, kind, r in records if kind == "framework_reply"
                   and r["reply"]["project_id"] == self.project.id]
        answered = {r["reply"]["issue_id"] for _, r in replies}
        entries = [(seq, self.state(r, task_id)) for seq, r in replies]
        entries = [(seq, card) for seq, card in entries if card["status"] != "unrelated_dependencies"]
        entries += [(seq, {"issue_id": i["id"], "status": "pending", "route": i["route"],
                           "details": "issue_get(issue_id=...)"})
                    for seq, kind, i in records if kind == "project_issue"
                    and i["project_id"] == self.project.id and i["id"] not in answered]
        return sorted(entries, key=lambda item: item[0]), through

    @staticmethod
    def _counts(entries):
        return {"total": len(entries),
                "update_count": sum(c["status"] in UPDATE_STATES for _, c in entries),
                "pending_count": sum(c["status"] == "pending" for _, c in entries)}

    def brief(self, task_id=None):
        entries, _ = self._entries(task_id)
        # Actionable changes stay discoverable; other records remain in the paged index.
        priority = sorted(entries, key=lambda item: (item[1]["status"] not in UPDATE_STATES, -item[0]))
        return {"issues": [c for _, c in priority[:3]], **self._counts(entries),
                "browse": {"tool": "issue_get", "arguments": {}},
                "updates": {"tool": "issue_get", "arguments": {"kind": "updates"}},
                "pending": {"tool": "issue_get", "arguments": {"kind": "pending"}}}

    def cards(self, task_id=None):
        return self.brief(task_id)["issues"]

    def get(self, args, task_id=None):
        if args.issue_id is None:
            through, after = map(int, args.cursor.split(":")) if args.cursor else (None, 0)
            entries, through = self._entries(task_id, through)
            counts = self._counts(entries)
            matches = [(seq, c) for seq, c in entries if args.kind == "all" or
                       (c["status"] in UPDATE_STATES if args.kind == "updates" else c["status"] == "pending")]
            remaining = [(seq, c) for seq, c in matches if seq > after]
            page = remaining[:args.limit]
            return {"issues": [c for _, c in page], "total": len(matches), "counts": counts,
                    "kind": args.kind,
                    "next_cursor": f"{through}:{page[-1][0]}" if len(remaining) > len(page) else None}
        issue = self.issue(args.issue_id)
        replies = [r for r in self.store.all("framework_reply")
                   if r["reply"]["issue_id"] == args.issue_id]
        return {"issue": issue, "replies": [{**r, "state": self.state(r, task_id)} for r in replies]}

    def attest(self, reply_id, task_id, operation_id, note):
        record = self.store.get("framework_reply", reply_id)
        if self.state(record, task_id)["status"] != "installed_needs_validation":
            raise HarnessError("version_not_installed", "Attest only the actual installed target version")
        task = self.runtime.task_memory._load(task_id)
        op = self.store.get("operation", operation_id)
        if (task.get("task_result", {}).get("outcome") != "succeeded" or
                op.get("task_id") != task_id or op["status"] != "succeeded" or
                task["execution"].get("installation_digest") != self.installation["digest"]):
            raise HarnessError("validation_required", "Use successful selected task/operation from this installation")
        record["validation_evidence"] = {"task_id": task_id, "operation_id": operation_id,
                                         "installation_digest": self.installation["digest"],
                                         "capabilities": [canonical(c) for c in op["capabilities"]],
                                         "skill_versions": task["skill_versions"],
                                         "note": note}
        self.store.put("framework_reply", reply_id, record)
        return self.state(record, task_id)
