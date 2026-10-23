"""Controlled coding orchestration: client, scoped delivery and candidate install."""
from __future__ import annotations
import argparse
import json
import signal
import tempfile
import uuid
from pathlib import Path
from pydantic import ValidationError
from .coding_install import CandidateInstall, CandidateNoProgress, install as install_candidate, resume_host
from .coding_git import git, local_file, start_git, same_repository, host_commit, unreported_source_changes
from .coding_delivery import (Check, UnrelatedChange, Handoff, shell_command, validation_basis,
    checked_delivery_scope, remember_delivery_scope, include_checked_scope, inspect_handoff,
    selected_profile, save, prior_record, complete_description, read_handoff)
from .coding_client import client_arguments, run_client
from .delivery import preserve
from .host_boundary import command, sha256
from .project_setup import guide
from .prompts import base_prompt

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
        "Complete the user's goal. Return the output-schema handoff: only your selected source paths and relevant validation commands/references. Previously checked scope/validation and actual matching Git commits are reused by the host; a missing narrative checkpoint is assembled from observed facts without another model turn. If actual local wheel installation is needed before completion, return candidate_install with project-relative wheels path/SHA256, reason and affected tasks (profile/task_id) already finished/archived or handed off. The parent uses the existing updater to preserve core/dependencies, then resumes this SAME session with the new fixed entry. This intermediate handoff need not claim final method readiness or commit unfinished work. No candidate means no install/extra turn. An installation failure returns here for local correction; only genuine credential/network/device or host-authorization conditions use blocked_reason to keep a precise pending handoff. Never replay old unknown/cancelled tasks; validate the new code in sourced new tasks. For a reusable-method request, include delivery with the original user goal, required whole-goal criteria, versioned task_run entry/input example and necessary remaining work; method_evidence references existing Registry registration and end-to-end plus different-input run reports. An ordinary fix does not need a new skill or delivery contract. Infer intent from the requested outcome, not a keyword. No fixed document change or repeated test is required. If worktree Git metadata is unavailable, leave commits empty and provide paths/commit_message: the local parent can commit initially clean paths after validation. Report mixed initial paths/blockers precisely. Respect explicit no-commit instructions via no_commit_reason; runtime work uses kind=runtime. Observed code edits cannot be omitted by an empty list/runtime label; explain outside-session changes as unrelated_changes with path/reason rather than claiming them.\n" +
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
                    handoff = read_handoff(final or "", record)
                    if handoff.blocked_reason:
                        record["blocked"] = True
                        missing = [handoff.blocked_reason]
                        checks = validation_basis(project, record, handoff)
                    elif handoff.candidate_install:
                        include_checked_scope(record, handoff)
                        complete_description(record, handoff)
                        observed = unreported_source_changes(project, record, handoff)
                        checks = validation_basis(project, record, handoff)
                        if observed or (handoff.paths and not checks):
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
    main()
