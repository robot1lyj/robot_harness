"""Coding delivery consistency from existing source, validation and task facts."""
import json
import shlex
import stat
from pathlib import Path
from typing import Literal
from pydantic import Field
from .contracts import Contract, GoalDelivery, CriterionResult
from .coding_install import CandidateInstall
from .host_boundary import sha256
from .coding_git import git, local_file, same_repository, host_commit, unreported_source_changes
from .delivery import preserve, check_method, check_results
from .project import source_profile
from .project_setup import default_project_profile
from .storage import Store

class Check(Contract):
    command: str
    evidence: str | None = None
    result: Literal["passed", "not_applicable"]
    detail: str = ""

class UnrelatedChange(Contract):
    path: str
    reason: str = Field(min_length=1)

class Handoff(Contract):
    candidate_install: CandidateInstall | None = None
    blocked_reason: str | None = Field(default=None, description="Only a genuine credential/network/device or host-authorization blocker; normal installation and fixable method/dependency metadata are work to continue")
    delivery: GoalDelivery | None = None
    criterion_results: list[CriterionResult] = Field(default_factory=list, description="Sourced domain results for an explicit one-off delivery; ordinary fixes may leave empty")
    method_evidence: str | None = Field(default=None, description="Project-relative JSON file with existing Registry registration and two different-input operation reports")
    kind: Literal["development", "runtime"] = "development"
    summary: str = ""
    paths: list[str] = Field(default_factory=list)
    validation: list[Check] = Field(default_factory=list)
    checkpoint: str = ""
    commits: list[str] = Field(default_factory=list)
    commit_message: str = ""
    no_commit_reason: str | None = Field(default=None, description="Only an explicit user instruction not to commit; never a Git/permission blocker")
    unrelated_changes: list[UnrelatedChange] = Field(default_factory=list, description="Explicit path/reason for observed changes outside this session's source delivery; never automatically claimed or committed")

def read_handoff(text, record):
    """Accept a JSON code fence and repair narrative shape only, retaining domain fields."""
    value = text.strip()
    if value.startswith("```") and value.endswith("```"):
        if "\n" not in value:
            raise ValueError("JSON code fence requires a body")
        value = value.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    body = json.loads(value)
    if isinstance(body, dict):
        for field in ("summary", "checkpoint", "commit_message"):
            if field in body and not isinstance(body[field], str):
                body.pop(field)
                record.setdefault("handoff_notes", []).append("Narrative field omitted: " + field)
    return Handoff.model_validate(body)


def shell_command(value):
    words = shlex.split(value)
    return words[-1] if len(words) >= 3 and Path(words[0]).name in {"bash", "sh"} and words[1] in {"-lc", "-c"} else value

def source_fingerprints(project, paths):
    facts = {}
    for name in paths:
        target = local_file(project, name)
        facts[name] = {"sha256": sha256(target), "executable": bool(target.stat().st_mode & stat.S_IXUSR)} if target.is_file() else None
    return facts


def validation_basis(project, record, handoff):
    checked = []
    for check in handoff.validation:
        observed = next((c for c in record["commands"] if c["exit_code"] == 0 and check.command.strip()
                         and shell_command(c["command"]) == shell_command(check.command)), None)
        if observed:
            checked.append({"basis": "host_observed_command", **observed, "detail": check.detail})
        elif check.evidence and local_file(project, check.evidence, evidence=True).is_file():
            checked.append({"basis": "agent_report_with_readable_reference", **check.model_dump()})
        elif check.result == "not_applicable" and check.detail.strip():
            checked.append({"basis": "agent_report_not_applicable", **check.model_dump()})
    scope = record.get("validation_scope", {})
    if (not handoff.validation and checked_delivery_scope(record)
            and scope.get("owner_id") == record.get("id")
            and scope.get("session_id") == record.get("session_id")
            and scope.get("files") == source_fingerprints(project, handoff.paths)):
        checked.extend(record.get("validation_basis", []))
    return checked

def checked_delivery_scope(record):
    scope = record.get("delivery_scope", {})
    if (record.get("session_id") and scope.get("session_id") == record["session_id"]
            and scope.get("owner_id") == record.get("id")
            and scope.get("start_head") == record["start"].get("head")):
        return scope
    return {}

def remember_delivery_scope(record, handoff, *, commits=None):
    if record.get("session_id"):
        record["delivery_scope"] = {
            "owner_id": record.get("id"), "session_id": record["session_id"],
            "start_head": record["start"].get("head"),
            "paths": list(handoff.paths),
            "commits": list(handoff.commits if commits is None else commits),
        }

def include_checked_scope(record, handoff):
    prior_scope = checked_delivery_scope(record)
    if prior_scope:
        handoff.paths = list(dict.fromkeys([*prior_scope["paths"], *handoff.paths]))
        handoff.commits = list(dict.fromkeys([*prior_scope["commits"], *handoff.commits]))
        if handoff.paths or handoff.commits:
            handoff.kind = "development"
    return prior_scope

def complete_description(record, handoff):
    """Formatting is derived from existing facts; gaps remain data, not another model turn."""
    if not handoff.summary.strip():
        handoff.summary = record.get("goal", "Development handoff")
    if not handoff.checkpoint.strip():
        handoff.checkpoint = json.dumps({
            "goal": record.get("goal"), "result": handoff.summary,
            "sources": {"paths": handoff.paths, "commits": handoff.commits,
                        "candidate_installs": record.get("candidate_installs", [])},
            "remaining": (record.get("delivery") or {}).get("remaining", []),
        }, ensure_ascii=False)
        record.setdefault("handoff_notes", [])[:] = ["Checkpoint assembled from observed delivery facts; optional narrative omitted"]


def observed_commits(project, record, handoff):
    """Derive actual SHAs only for an explicitly selected, clean-at-start path scope."""
    start = record["start"]
    checked_commits = set(checked_delivery_scope(record).get("commits", []))
    if set(handoff.commits) - checked_commits or not handoff.paths or start["error"]:
        return
    same_repository(project, start)
    if set(handoff.paths) & set(start["dirty"]):
        return
    commits = git(project, "rev-list", "--reverse", start["head"] + "..HEAD").splitlines()
    coverage = set()
    for sha in commits:
        coverage.update(git(project, "diff-tree", "--no-ext-diff", "--no-textconv", "--no-commit-id", "--name-only", "-r", sha).splitlines())
    if commits and coverage == set(handoff.paths):
        handoff.commits = commits


def inspect_handoff(project, record, handoff, *, no_commit=False):
    missing, checked = _inspect_handoff(project, record, handoff, no_commit=no_commit)
    scope = checked_delivery_scope(record)
    if checked:
        if scope and scope["paths"] == handoff.paths and scope["commits"] == handoff.commits:
            record["validation_scope"] = {"owner_id": record.get("id"), "session_id": record.get("session_id"),
                                          "files": source_fingerprints(project, handoff.paths)}
    complete_description(record, handoff)
    return missing, checked


def _inspect_handoff(project, record, handoff, *, no_commit=False):
    missing, checked = [], []
    record["blocked"] = False
    prior_scope = include_checked_scope(record, handoff)
    observed = unreported_source_changes(project, record, handoff)
    if observed:
        return ["Observed source changes need a scoped development handoff or unrelated_changes path/reason: " + ", ".join(observed) + ". Preserve other actors' changes; do not claim or commit them automatically."], checked
    if handoff.delivery:
        try:
            prior = record.get("delivery")
            record["delivery"] = preserve(prior, handoff.delivery, record["goal"])
            if handoff.delivery.kind == "one_off":
                missing.extend(check_results(record["delivery"], [r.model_dump() for r in handoff.criterion_results]))
            if handoff.delivery.kind == "reusable_method":
                report = None
                if handoff.method_evidence:
                    path = local_file(project, handoff.method_evidence, evidence=True)
                    if path.stat().st_size > 8 * 1024 * 1024:
                        raise ValueError("Method report exceeds 8 MiB")
                    report = json.loads(path.read_text())
                missing.extend(check_method(record["delivery"], report))
                checked.append({"basis": "existing_method_report_structure_only", "path": handoff.method_evidence})
        except (ValueError, OSError) as exc:
            missing.append("Whole-goal delivery: " + str(exc))
    elif record.get("delivery"):
        missing.append("Keep the original reusable/one-off delivery contract in the handoff")
    if handoff.kind == "runtime":
        return missing, checked  # Robot task/operation conclusions keep their original semantics.
    if not handoff.paths and not handoff.commits and not (no_commit or handoff.no_commit_reason):
        return missing, checked  # No source delivery, empty commit or clean-tree gate.
    for name in handoff.paths:
        local_file(project, name)
    checked.extend(validation_basis(project, record, handoff))
    if handoff.paths and not checked:
        missing.append("Reference existing successful validation (exact command or readable file), or explain why not applicable; do not rerun a valid check.")
    start = record["start"]
    if not start["error"]:
        try:
            same_repository(project, start)
        except (OSError, ValueError) as exc:
            record["blocked"] = True
            return [*missing, "Repository handoff: " + str(exc)], checked
    if no_commit or handoff.no_commit_reason:
        checked.append({"basis":"host_no_commit_option" if no_commit else "agent_report_user_no_commit", "reason":handoff.no_commit_reason})
        if not start["error"] and git(project, "rev-parse", "HEAD").strip() != record.get("invocation_head", start["head"]):
            missing.append("HEAD changed despite the no-commit instruction; retain the actual history and explain the discrepancy.")
        if not missing:
            # No-commit authorizes path continuity, not unchecked commit claims.
            remember_delivery_scope(record, handoff, commits=prior_scope.get("commits", []))
        return missing, checked
    if start["error"]:
        record["blocked"] = True
        missing.append("Git handoff: " + start["error"])
        return missing, checked
    observed_commits(project, record, handoff)
    if not handoff.commits and start["external_git"] and not missing:
        try:
            handoff.commits.append(host_commit(project, start, handoff))
        except (OSError, ValueError) as exc:
            record["blocked"] = True
            missing.append("Local host commit: " + str(exc))
    if not handoff.commits:
        missing.append("Commit the selected source changes locally, or report the exact commit blocker.")
        return missing, checked
    try:
        if git(project, "symbolic-ref", "--short", "HEAD").strip() != start["branch"]:
            record["blocked"] = True
            raise ValueError("Selected branch changed")
        coverage = set()
        for sha in handoff.commits:
            if not all(c in "0123456789abcdef" for c in sha) or len(sha) not in (40, 64):
                raise ValueError("Report the full actual commit SHA")
            git(project, "merge-base", "--is-ancestor", start["head"], sha)
            git(project, "merge-base", "--is-ancestor", sha, "HEAD")
            if sha == start["head"]:
                raise ValueError("Starting HEAD is not a new delivery commit")
            coverage.update(git(project, "diff-tree", "--no-ext-diff", "--no-textconv", "--no-commit-id", "--name-only", "-r", sha).splitlines())
        if coverage != set(handoff.paths):
            raise ValueError("Commit paths differ from the claimed delivery: " + ", ".join(sorted(coverage ^ set(handoff.paths))))
        if not coverage:
            raise ValueError("An empty commit is not a source delivery")
        overlap = coverage & set(start["dirty"])
        if overlap:
            record["blocked"] = True
            raise ValueError("Initial mixed paths require scoped hunk evidence: " + ", ".join(sorted(overlap)))
        for name in handoff.paths:
            target = local_file(project, name)
            entry = git(project, "ls-tree", "--format=%(objectmode) %(objectname)", "HEAD", "--", name).strip()
            if target.is_file():
                blob = git(project, "hash-object", "--no-filters", "--stdin", input=target.read_bytes()).strip()
                mode = "100755" if target.stat().st_mode & stat.S_IXUSR else "100644"
                if entry != mode + " " + blob:
                    raise ValueError("Selected path still differs from the delivered commit: " + name)
            elif entry:
                raise ValueError("Selected deletion was not committed: " + name)
        # Git scope is checked independently of a still-missing whole-goal report.
        remember_delivery_scope(record, handoff)
    except (OSError, ValueError) as exc:
        missing.append("Commit correspondence: " + str(exc))
    return missing, checked

def selected_profile(project):
    profile = default_project_profile(project) or source_profile(project, state_root=str(project / ".rsi/state"))
    if not Path(profile.state_root).resolve().is_relative_to(project):
        raise ValueError("Coding delivery requires the selected development Store inside this project")
    return profile.model_copy(update={"role": "development", "project": profile.project.model_copy(
        update={"purpose": "improve"}) if profile.project else None})

def save(profile, record):
    with_store = Store(profile)
    try:
        with_store.put("coding_delivery", record["id"], record)
    finally:
        with_store.close()

def prior_record(profile, session):
    store = Store(profile)
    try:
        row = store.db.execute("SELECT body FROM records WHERE kind='coding_delivery' AND json_extract(body,'$.session_id')=? ORDER BY rowid DESC LIMIT 1", (session,)).fetchone()
        if row is None:
            raise ValueError("No coding handoff for this session in the selected development scope")
        return json.loads(row[0])
    finally:
        store.close()
