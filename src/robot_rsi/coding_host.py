"""Local coding exit seam: one handoff, fixed Git operations, at most one same-session repair."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from .coding_install import CandidateInstall, CandidateNoProgress, install as install_candidate, resume_host
from .contracts import Contract, HarnessError, GoalDelivery, CriterionResult
from .delivery import preserve, check_method, check_results
from .host_boundary import command, sha256
from .project import source_profile
from .project_setup import default_project_profile, guide
from .prompts import base_prompt
from .storage import Store


class Check(Contract):
    command: str
    evidence: str | None
    result: Literal["passed", "not_applicable"]
    detail: str


class UnrelatedChange(Contract):
    path: str
    reason: str = Field(min_length=1)


class Handoff(Contract):
    candidate_install: CandidateInstall | None = None
    blocked_reason: str | None = Field(default=None, description="Only a genuine credential/network/device or host-authorization blocker; normal installation and fixable method/dependency metadata are work to continue")
    delivery: GoalDelivery | None = None
    criterion_results: list[CriterionResult] = Field(default_factory=list, description="Sourced domain results for an explicit one-off delivery; ordinary fixes may leave empty")
    method_evidence: str | None = Field(default=None, description="Project-relative JSON file with existing Registry registration and two different-input operation reports")
    kind: Literal["development", "runtime"]
    summary: str
    paths: list[str]
    validation: list[Check]
    checkpoint: str
    commits: list[str]
    commit_message: str
    no_commit_reason: str | None = Field(description="Only an explicit user instruction not to commit; never a Git/permission blocker")
    unrelated_changes: list[UnrelatedChange] = Field(default_factory=list, description="Explicit path/reason for observed changes outside this session's source delivery; never automatically claimed or committed")


def git(project, *args, input=None, env=None):
    # No project hooks, filters, external diff, pager or fsmonitor commands on the host.
    environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    environment.update(env or {})
    argv = ["/usr/bin/git", "--no-pager", "--literal-pathspecs", "-C", str(project),
            "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "commit.gpgSign=false"]
    filters = subprocess.run([*argv, "config", "--name-only", "--get-regexp", r"^filter\..*\.(clean|smudge|process|required)$"],
                             capture_output=True, env=environment)
    if filters.returncode not in (0, 1):
        raise ValueError(filters.stderr.decode(errors="replace"))
    for key in filters.stdout.decode().splitlines():
        argv += ["-c", key + ("=false" if key.endswith(".required") else "=")]
    reply = subprocess.run([*argv, *args], input=input,
                           capture_output=True, env=environment)
    if reply.returncode:
        raise ValueError(reply.stderr.decode(errors="replace").strip() or "Git operation failed")
    return reply.stdout.decode()


def local_file(project, name, *, evidence=False):
    path = Path(name)
    if (not name or path.is_absolute() or ".." in path.parts or any(ord(c) < 32 for c in name)
            or any(p in {".git", ".venv", ".env", "auth.json", ".codex", ".ssh"} for p in path.parts)
            or (not evidence and ".rsi" in path.parts)
            or (project / path).resolve() != project / path):
        raise ValueError(f"Not a regular project source/evidence path: {name}")
    target = project / path
    if target.exists() and not target.is_file():
        raise ValueError(f"Select a file, not a directory: {name}")
    return target


def start_git(project):
    try:
        if Path(git(project, "rev-parse", "--show-toplevel").strip()).resolve() != project:
            raise ValueError("Select the repository/worktree root for coding delivery")
        dirty = set()
        for args in (("ls-files", "--modified", "--deleted", "-z"),
                     ("diff", "--cached", "--no-ext-diff", "--no-textconv", "--name-only", "-z"),
                     ("ls-files", "--others", "--exclude-standard", "-z")):
            dirty.update(p for p in git(project, *args).split("\0") if p)
        initial = {}
        for name in dirty:
            try:
                target = local_file(project, name)
                initial[name] = sha256(target) if target.is_file() else None
            except ValueError:
                continue  # excluded state/credentials are not source or delivery inputs
        return {"head": git(project, "rev-parse", "HEAD").strip(),
                "branch": git(project, "symbolic-ref", "--short", "HEAD").strip(),
                "git_dir": git(project, "rev-parse", "--absolute-git-dir").strip(),
                "common_dir": git(project, "rev-parse", "--path-format=absolute", "--git-common-dir").strip(),
                "dirty": sorted(dirty),
                "staged": git(project, "diff", "--cached", "--no-ext-diff", "--no-textconv", "--name-only", "-z").split("\0")[:-1],
                "external_git": (project / ".git").is_file(), "error": None,
                "initial_files": initial}
    except (OSError, ValueError) as exc:
        return {"error": str(exc), "dirty": [], "staged": []}


def same_repository(project, start):
    if (Path(git(project, "rev-parse", "--show-toplevel").strip()).resolve() != project
            or git(project, "rev-parse", "--absolute-git-dir").strip() != start["git_dir"]
            or git(project, "rev-parse", "--path-format=absolute", "--git-common-dir").strip() != start["common_dir"]):
        raise ValueError("Repository/Git metadata changed; do not redirect local host operations")


def host_commit(project, start, handoff):
    """Clean-at-start paths only; never run project commands or alter unrelated index entries."""
    paths = list(dict.fromkeys(handoff.paths))
    same_repository(project, start)
    overlap = set(paths) & set(start["dirty"])
    if overlap:
        raise ValueError("Initial mixed changes need an explicit hunk handoff: " + ", ".join(sorted(overlap)))
    if (git(project, "rev-parse", "HEAD").strip() != start["head"]
            or git(project, "symbolic-ref", "--short", "HEAD").strip() != start["branch"]):
        raise ValueError("Branch/HEAD changed during coding; host commit needs a fresh scoped handoff")
    if git(project, "diff", "--cached", "--no-ext-diff", "--no-textconv", "--name-only", "--", *paths).strip():
        raise ValueError("Selected paths now have staged changes; preserve the index and hand off")
    entries = []
    for name in paths:
        target = local_file(project, name)
        if target.exists():
            blob = git(project, "hash-object", "-w", "--no-filters", "--stdin", input=target.read_bytes()).strip()
            mode = "100755" if target.stat().st_mode & stat.S_IXUSR else "100644"
            entries.append(f"{mode} {blob}\t{name}\n")
        else:
            entries.append(f"0 {'0' * len(start['head'])}\t{name}\n")
    with tempfile.TemporaryDirectory(prefix="robot-rsi-git-") as directory:
        index = {"GIT_INDEX_FILE": str(Path(directory) / "index")}
        git(project, "read-tree", start["head"], env=index)
        git(project, "update-index", "--index-info", input="".join(entries).encode(), env=index)
        tree = git(project, "write-tree", env=index).strip()
        if tree == git(project, "rev-parse", start["head"] + "^{tree}").strip():
            raise ValueError("No selected source changes; no empty host commit")
        sha = git(project, "commit-tree", tree, "-p", start["head"],
                  input=(handoff.commit_message.strip() or handoff.summary).encode()).strip()
        git(project, "update-ref", "HEAD", sha, start["head"])
        # Only our initially clean paths change in the real index; other staging is untouched.
        git(project, "update-index", "--index-info", input="".join(entries).encode())
    return sha


def shell_command(value):
    words = shlex.split(value)
    return words[-1] if len(words) >= 3 and Path(words[0]).name in {"bash", "sh"} and words[1] in {"-lc", "-c"} else value


def unreported_source_changes(project, record, handoff):
    """Existing file events, plus Git's changed-path list when a handoff claims no development."""
    start = record["start"]
    names = set(record.get("file_changes", []))
    if (handoff.kind == "runtime" or not handoff.paths) and not start["error"]:
        same_repository(project, start)
        names.update(p for p in git(project, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", start["head"]).split("\0") if p)
        names.update(p for p in git(project, "ls-files", "--others", "--exclude-standard", "-z").split("\0") if p)
    excluded = {str(local_file(project, c.path).relative_to(project)) for c in handoff.unrelated_changes}
    sources = set()
    for name in names:
        try:
            path = Path(name)
            name = str(path.relative_to(project)) if path.is_absolute() else str(path)
            target = local_file(project, name)
        except ValueError:
            continue
        # A light code hint, not an ownership claim or a scan of generated results/state.
        if (target.suffix not in {".py", ".pyi", ".js", ".ts", ".jsx", ".tsx", ".c", ".cc", ".cpp", ".h", ".hpp", ".rs", ".go", ".java", ".sh", ".toml"}
                and target.name not in {"Makefile", "CMakeLists.txt", "package.json"}):
            continue
        if name in excluded or (handoff.kind != "runtime" and name in handoff.paths):
            continue
        initial = start.get("initial_files", {})
        if name in initial:
            changed = (sha256(target) if target.is_file() else None) != initial[name]
        elif not start["error"]:
            entry = git(project, "ls-tree", "--format=%(objectmode) %(objectname)", start["head"], "--", name).strip()
            if target.is_file():
                blob = git(project, "hash-object", "--no-filters", "--stdin", input=target.read_bytes()).strip()
                mode = "100755" if target.stat().st_mode & stat.S_IXUSR else "100644"
                changed = entry != mode + " " + blob
            else:
                changed = bool(entry)
        else:
            changed = True  # Actual file event, but no usable repository baseline.
        if changed:
            sources.add(name)
    return sorted(sources)


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


def inspect_handoff(project, record, handoff, *, no_commit=False):
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
    if (handoff.paths or handoff.commits) and not handoff.checkpoint.strip():
        missing.append("Provide a short recoverable checkpoint (goal, result, gaps/next step and sources).")
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


def client_arguments(arguments):
    parser = argparse.ArgumentParser(description="Existing Codex exec options inside the controlled coding entry")
    parser.add_argument("--model", "-m")
    parser.add_argument("--config", "-c", action="append", default=[])
    parser.add_argument("--sandbox", "-s")
    for flag in ("ignore-user-config", "ignore-rules", "dangerously-bypass-approvals-and-sandbox", "skip-git-repo-check", "json"):
        parser.add_argument("--" + flag, action="store_true")
    parser.add_argument("--output-last-message", "-o")
    parser.add_argument("prompt", nargs="?", default="-")
    args = parser.parse_args(arguments)
    options = []
    if args.model:
        options += ["--model", args.model]
    for setting in args.config + (["sandbox_mode=" + json.dumps(args.sandbox)] if args.sandbox else []):
        options += ["-c", setting]
    for flag in ("ignore-user-config", "ignore-rules", "dangerously-bypass-approvals-and-sandbox", "skip-git-repo-check"):
        if getattr(args, flag.replace("-", "_")):
            options.append("--" + flag)
    if args.output_last_message:
        options += ["--output-last-message", args.output_last_message]
    return options, sys.stdin.read() if args.prompt == "-" else args.prompt


def run_client(root, project, options, prompt, schema, record, *, reads=(), auth=None, network=False, session=None):
    arguments = (["resume"] if session else []) + options + ["--json", "--output-schema", str(schema)]
    if session:
        arguments.append(session)
    arguments.append("-")
    argv = command(root, project, "coding", arguments, reads=reads, auth=auth, network=network)
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    final, interrupted, terminal, usage = None, False, None, None
    try:
        process.stdin.write(prompt)
        process.stdin.close()
        for line in process.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            item = event.get("item", {})
            if item.get("type") == "reasoning":
                continue
            print(line, end="", flush=True)
            if event.get("type") in {"turn.completed", "turn.failed", "error"}:
                terminal, usage = event["type"], event.get("usage")
            if event.get("type") == "thread.started":
                identity = str(uuid.UUID(event["thread_id"]))
                if session and identity != session:
                    raise ValueError("Resume returned another session; do not continue automatically")
                record["session_id"] = identity
            if event.get("type") == "item.completed":
                if item.get("type") == "command_execution":
                    record["commands"].append({"id": item.get("id"), "command": item["command"], "exit_code": item.get("exit_code")})
                elif item.get("type") == "agent_message":
                    final = item.get("text")
                elif item.get("type") == "file_change":
                    record.setdefault("file_changes", []).extend(c["path"] for c in item.get("changes", []) if isinstance(c.get("path"), str))
        process.wait()
    except KeyboardInterrupt:
        interrupted = True
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    record["turns"].append({"session_id": record.get("session_id"), "resume": bool(session),
                             "exit_code": process.returncode, "interrupted": interrupted,
                             "terminal": terminal, "usage": usage})
    return final, interrupted, process.returncode or (0 if terminal == "turn.completed" else 1)


def run(root, project, arguments, *, reads=(), auth=None, network=False, resume=None, status=False, no_commit=False):
    command(root, project, "coding", [])  # validate/create only the existing writable host paths
    profile = selected_profile(project)
    if resume:
        record = prior_record(profile, str(uuid.UUID(resume)))

        if status or (record["status"] == "complete" and not arguments):
            print(json.dumps(record, ensure_ascii=False))
            return 0
        root = resume_host(root, project, record)
        record["candidate_retry"] = True  # Explicit resume can retry a failed/interrupted install once.
        if record["status"] == "complete":
            raise ValueError("Delivery is complete; start a new coding invocation for a new goal")
        options, continuation = client_arguments(arguments) if arguments else (record["client_options"], "Continue the saved goal and pending handoff.")
        if options != record["client_options"]:
            raise ValueError("Resume must preserve the original model and client permission options")
        prompt = record["goal"] + "\nContinue from this development handoff:\n" + json.dumps({k:record.get(k) for k in ("handoff", "pending", "validation_basis", "commands", "candidate_installs", "host")}) + "\n" + continuation
        no_commit = no_commit or record["no_commit"]
        if no_commit:
            prompt += "\nThe host explicitly requests no additional commits."
    else:
        options, prompt = client_arguments(arguments)
        record = {"id": "coding-delivery_" + uuid.uuid4().hex, "host":str(root), "origin_host":str(root), "project": str(project), "goal": prompt,
                  "session_id": None, "start": start_git(project), "client_options": options,
                  "commands": [], "turns": [], "no_commit": no_commit,
                  "base_identity": base_prompt()["identity"]}
    if status and not resume:
        raise ValueError("Select an explicit --resume-session for status")
    record.update(status="running", pending=[], blocked=False, no_commit=no_commit,
                  invocation_head=git(project, "rev-parse", "HEAD").strip() if resume and not record["start"]["error"] else record["start"].get("head"))
    save(profile, record)
    # Schema is transport material in the existing host-home, not a new memory owner.
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", dir=project / ".rsi/host-home", delete=False) as stream:
        schema_body = Handoff.model_json_schema()
        schema_body["required"] = list(schema_body["properties"])
        json.dump(schema_body, stream)
        schema = Path(stream.name)
    instructions = (guide("base") + "\n" +
        "Complete the user's goal. Return the output-schema handoff: only your source paths, actual local commits, existing validation commands/references, and a concise recoverable checkpoint. If actual local wheel installation is needed before completion, return candidate_install with project-relative wheels path/SHA256, reason and affected tasks (profile/task_id) already finished/archived or handed off. The parent uses the existing updater to preserve core/dependencies, then resumes this SAME session with the new fixed entry. This intermediate handoff need not claim final method readiness or commit unfinished work. No candidate means no install/extra turn. An installation failure returns here for local correction; only genuine credential/network/device or host-authorization conditions use blocked_reason to keep a precise pending handoff. Never replay old unknown/cancelled tasks; validate the new code in sourced new tasks. For a reusable-method request, include delivery with the original user goal, required whole-goal criteria, versioned task_run entry/input example and necessary remaining work; method_evidence references existing Registry registration and end-to-end plus different-input run reports. An ordinary fix does not need a new skill or delivery contract. Infer intent from the requested outcome, not a keyword. No fixed document change or repeated test is required. If worktree Git metadata is unavailable, leave commits empty and provide paths/commit_message: the local parent can commit initially clean paths after validation. Report mixed initial paths/blockers precisely. Respect explicit no-commit instructions via no_commit_reason; runtime work uses kind=runtime. Observed code edits cannot be omitted by an empty list/runtime label; explain outside-session changes as unrelated_changes with path/reason rather than claiming them.\n" +
        "Host start facts (data): " + json.dumps(record["start"]) + "\nHost explicit no-commit option: " + json.dumps(no_commit) + "\nUser goal:\n")
    try:
        repair_used = False
        while True:
            try:
                final, interrupted, code = run_client(root, project, options, prompt if record["session_id"] else instructions + prompt, schema, record,
                    reads=reads, auth=auth, network=network, session=record["session_id"])
            except (OSError, ValueError) as exc:
                record.update(status="pending", pending=["Client transport: " + str(exc)])
                save(profile, record)
                break
            missing, checks = [], []
            handoff = None
            if interrupted or code:
                missing = ["Interrupted; explicit resume required. Keep observed validation; inspect current source against the saved start, then finish the remaining handoff/commit." if interrupted else f"Client exit/turn failure ({code}); resolve authentication/tool/process failure before explicit resume."]
            else:
                try:
                    handoff = Handoff.model_validate_json(final or "")
                    if handoff.blocked_reason:
                        record["blocked"] = True
                        missing = [handoff.blocked_reason]
                        checks = validation_basis(project, record, handoff)
                    elif handoff.candidate_install:
                        include_checked_scope(record, handoff)
                        observed = unreported_source_changes(project, record, handoff)
                        checks = validation_basis(project, record, handoff)
                        if observed or not handoff.checkpoint.strip() or (handoff.paths and not checks):
                            raise ValueError("Candidate needs scoped source paths, existing validation and a recoverable checkpoint")
                        for name in handoff.paths:
                            local_file(project, name)
                        if handoff.delivery:
                            record["delivery"] = preserve(record.get("delivery"), handoff.delivery, record["goal"])
                        record.update(handoff=handoff.model_dump(), validation_basis=checks,
                                      status="installing")
                        save(profile, record)
                        previous_attempts = len(record.get("candidate_installs", []))
                        try:
                            root, receipt = install_candidate(root, project, record, handoff.candidate_install, local_file, allow_network=network, persist=lambda value: save(profile, value))
                        except CandidateNoProgress as exc:
                            record["blocked"] = True
                            raise ValueError(str(exc)) from exc
                        except Exception as exc:
                            if len(record.get("candidate_installs", [])) > previous_attempts:
                                record.update(status="pending", pending=["Candidate install: " + str(exc)])
                                save(profile, record)
                                prompt = ("Installation did not complete; the original host/core/base and task outcomes remain. "
                                          "Continue as this same Agent: correct local code/dependency metadata or select another compatible artifact, "
                                          "then request a changed candidate. Do not repeat this unchanged artifact or upgrade the fixed base. "
                                          "Only genuine credential/network/device/host-authorization conditions use blocked_reason with a precise gap. "
                                          "Host installation result: " + json.dumps(record["candidate_installs"][-1]))
                                continue
                            raise ValueError("Candidate install: " + str(exc)) from exc
                        print(json.dumps({"type": "coding.install", "record": receipt}, ensure_ascii=False), flush=True)
                        record.update(status="running", pending=[])
                        save(profile, record)
                        prompt = ("Continue the same original goal in this session using the installed candidate. "
                                  "This is installation continuation, not another review. Keep valid checks and old task outcomes; "
                                  "use new sourced task IDs for new code. Complete actual invocation/reuse and the final handoff. "
                                  "Omit candidate_install unless another actual changed artifact is necessary. Host facts: "
                                  + json.dumps(receipt))
                        continue
                    else:
                        missing, checks = inspect_handoff(project, record, handoff, no_commit=no_commit)
                except (ValidationError, OSError, ValueError) as exc:
                    missing = ["Handoff: " + str(exc)]
            record.update(handoff=handoff.model_dump() if handoff else record.get("handoff"),
                          validation_basis=checks or record.get("validation_basis", []),
                          pending=missing, status="interrupted" if interrupted else "pending" if missing else "complete")
            save(profile, record)
            if not missing or interrupted or code or record.get("blocked") or not record["session_id"] or repair_used:
                break
            repair_used = True
            prompt = ("Complete only these missing handoff items in this same session; preserve valid checks and user changes. Do not repeat completed work:\n" + "\n".join(missing)
                      + "\nLocal host handoff facts: " + json.dumps(record["handoff"]))
        print(json.dumps({"type":"coding.delivery", "record":record}, ensure_ascii=False), flush=True)
        return 130 if record["status"] == "interrupted" else 0 if record["status"] == "complete" else 2
    finally:
        schema.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--read", type=Path, action="append", default=[])
    parser.add_argument("--auth", type=Path)
    parser.add_argument("--network", action="store_true")
    parser.add_argument("--resume-session")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--no-commit", action="store_true")
    args, extra = parser.parse_known_args()
    if extra[:1] == ["--"]:
        extra = extra[1:]
    def interrupt(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupt)
    try:
        return run(args.host.resolve(), args.project.resolve(), extra, reads=args.read, auth=args.auth,
                   network=args.network, resume=args.resume_session, status=args.status, no_commit=args.no_commit)
    except (HarnessError, OSError, ValueError) as exc:
        print(json.dumps({"ok":False,"error":{"code":"coding_delivery_failed","message":str(exc)}}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
