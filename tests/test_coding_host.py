import uuid

import pytest

from robot_rsi import coding_host as host


def repository(path):
    path.mkdir()
    host.git(path, "init", "-b", "codex/fixture")
    host.git(path, "config", "user.name", "Offline fixture")
    host.git(path, "config", "user.email", "fixture@example.invalid")
    for name in ("mean.py", "staged.txt", "unstaged.txt"):
        (path / name).write_text("original\n")
    host.git(path, "add", "--", "mean.py", "staged.txt", "unstaged.txt")
    host.git(path, "commit", "-m", "baseline")
    return path


def handoff(**changes):
    return host.Handoff.model_validate({"kind":"development", "summary":"Fix fractional mean", "paths":["mean.py"],
        "validation":[{"command":"python check.py", "evidence":None, "result":"passed", "detail":"Fractional case passed"}],
        "checkpoint":"Goal fixed; fractional check passed. No hardware claims. Next: no pending source work; see mean.py.",
        "commits":[], "commit_message":"Fix fractional mean", "no_commit_reason":None, **changes})


def record(project):
    return {"start":host.start_git(project), "commands":[{"id":"check", "command":"/bin/bash -lc 'python check.py'", "exit_code":0}]}


def test_local_host_commit_preserves_unrelated_staging_and_never_runs_filters_or_hooks(tmp_path):
    owner = repository(tmp_path / "owner")
    work = tmp_path / "worktree"
    host.git(owner, "worktree", "add", "-b", "codex/work", str(work))
    (work / "staged.txt").write_text("user staging\n")
    host.git(work, "add", "--", "staged.txt")
    (work / "unstaged.txt").write_text("user working copy\n")
    # Host add/diff/commit would run these project commands if the exit seam used them.
    marker = tmp_path / "must-not-run"
    (work / ".gitattributes").write_text("mean.py filter=unsafe\n")
    host.git(owner, "config", "filter.unsafe.clean", f"touch {marker}")
    hook = owner / ".git/hooks/pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    before_index = host.git(work, "ls-files", "--stage", "--", "staged.txt", "unstaged.txt")
    rec = record(work)
    (work / "mean.py").write_text("fixed\n")
    result = handoff()
    missing, checks = host.inspect_handoff(work, rec, result)
    assert missing == [] and checks[0]["basis"] == "host_observed_command"
    assert len(result.commits) == 1
    assert host.git(work, "show", "--format=", "--name-only", result.commits[0]).split() == ["mean.py"]
    assert host.git(work, "ls-files", "--stage", "--", "staged.txt", "unstaged.txt") == before_index
    assert (work / "unstaged.txt").read_text() == "user working copy\n"
    assert not marker.exists()


def test_initial_mixed_path_is_left_pending_without_whole_file_commit(tmp_path):
    owner = repository(tmp_path / "owner")
    work = tmp_path / "worktree"
    host.git(owner, "worktree", "add", "-b", "codex/work", str(work))
    (work / "mean.py").write_text("user line\noriginal\n")
    rec = record(work)
    (work / "mean.py").write_text("user line\nfixed\n")
    missing, _ = host.inspect_handoff(work, rec, handoff())
    assert any("mixed changes" in item for item in missing)
    assert rec["blocked"]
    assert host.git(work, "rev-parse", "HEAD").strip() == rec["start"]["head"]
    assert (work / "mean.py").read_text().startswith("user line")


def test_missing_local_host_identity_stays_pending_without_changing_head_or_index(tmp_path):
    owner = repository(tmp_path / "owner")
    work = tmp_path / "worktree"
    host.git(owner, "worktree", "add", "-b", "codex/work", str(work))
    host.git(owner, "config", "user.name", "")
    host.git(owner, "config", "user.email", "")
    rec = record(work)
    index = host.git(work, "ls-files", "--stage")
    (work / "mean.py").write_text("fixed\n")
    missing, _ = host.inspect_handoff(work, rec, handoff())
    assert missing and rec["blocked"]
    assert host.git(work, "rev-parse", "HEAD").strip() == rec["start"]["head"]
    assert host.git(work, "ls-files", "--stage") == index


def test_client_cannot_redirect_parent_commit_to_another_git_directory(tmp_path):
    owner = repository(tmp_path / "owner")
    work = tmp_path / "worktree"
    host.git(owner, "worktree", "add", "-b", "codex/work", str(work))
    rec = record(work)
    # Even an equivalent copied Git directory must not become the selected host destination.
    import shutil
    other = tmp_path / "other-git"
    shutil.copytree(owner / ".git", other)
    (work / ".git").write_text("gitdir: " + str(other / "worktrees/worktree") + "\n")
    (work / "mean.py").write_text("fixed\n")
    missing, _ = host.inspect_handoff(work, rec, handoff())
    assert rec["blocked"] and any("metadata changed" in x for x in missing)
    assert host.git(owner, "rev-parse", "HEAD").strip() == rec["start"]["head"]


def test_starting_head_and_commit_with_wrong_paths_are_not_delivery(tmp_path):
    project = repository(tmp_path / "repo")
    rec = record(project)
    assert any("Starting HEAD" in x for x in host.inspect_handoff(project, rec, handoff(commits=[rec["start"]["head"]]))[0])
    (project / "other.py").write_text("unrelated commit\n")
    host.git(project, "add", "--", "other.py")
    host.git(project, "commit", "-m", "other change")
    missing, _ = host.inspect_handoff(project, rec, handoff(commits=[host.git(project, "rev-parse", "HEAD").strip()]))
    assert any("Commit paths differ" in x for x in missing)


@pytest.mark.parametrize("kind,paths,no_commit", [("runtime",[],False),("development",[],False),("development",["mean.py"],True)])
def test_runtime_no_change_and_explicit_no_commit_do_not_create_commits(tmp_path, kind, paths, no_commit):
    project = repository(tmp_path / "repo")
    rec = record(project)
    if paths:
        (project / "mean.py").write_text("fixed\n")
    missing, _ = host.inspect_handoff(project, rec, handoff(kind=kind, paths=paths, checkpoint="" if not paths else "Scoped continuation"), no_commit=no_commit)
    assert not missing
    assert host.git(project, "rev-parse", "HEAD").strip() == rec["start"]["head"]


@pytest.mark.parametrize("name", ["../escape", ".git/config", ".rsi/state.db", ":(glob)*", "x\ny"])
def test_host_commit_requires_literal_regular_source_files(tmp_path, name):
    if name == ":(glob)*":
        # A literal filename cannot expand into arbitrary repository pathspecs.
        assert host.local_file(tmp_path, name) == tmp_path / name
    else:
        with pytest.raises(ValueError):
            host.local_file(tmp_path, name)


def test_existing_reference_and_not_applicable_checks_are_qualified_without_running_them(tmp_path):
    project = repository(tmp_path / "repo")
    rec = record(project)
    (project / ".rsi").mkdir()
    (project / ".rsi/previous-check.txt").write_text("Prior targeted result\n")
    proof = [{"command":"not observed", "evidence":".rsi/previous-check.txt", "result":"passed", "detail":"previous evidence"}]
    missing, checks = host.inspect_handoff(project, rec, handoff(validation=proof), no_commit=True)
    assert not missing and checks[0]["basis"] == "agent_report_with_readable_reference"


@pytest.mark.parametrize("kind", ["development", "runtime"])
def test_r1_empty_or_runtime_handoff_cannot_hide_changed_source(tmp_path, kind):
    project = repository(tmp_path / "repo")
    rec = record(project)
    (project / "mean.py").write_text("fixed\n")
    missing, _ = host.inspect_handoff(project, rec, handoff(kind=kind, paths=[], commits=[], validation=[], checkpoint=""))
    assert missing and "mean.py" in missing[0]
    assert not rec["blocked"]  # The same session can correct its handoff; no automatic ownership.
    assert host.git(project, "rev-parse", "HEAD").strip() == rec["start"]["head"]


def test_r1_source_event_outside_claim_needs_scope_but_parallel_explanation_can_close(tmp_path):
    project = repository(tmp_path / "repo")
    rec = record(project)
    (project / "parallel.py").write_text("another actor's source\n")
    rec["file_changes"] = [str(project / "parallel.py")]
    report = handoff(paths=[], commits=[], validation=[], checkpoint="")
    assert host.inspect_handoff(project, rec, report)[0]
    report.unrelated_changes = [host.UnrelatedChange(path="parallel.py", reason="Another local actor owns this change; this session only inspected it.")]
    assert host.inspect_handoff(project, rec, report)[0] == []
    assert host.git(project, "rev-parse", "HEAD").strip() == rec["start"]["head"]


def test_r1_runtime_result_and_unchanged_source_do_not_require_git_delivery(tmp_path):
    project = repository(tmp_path / "repo")
    (project / "result.json").write_text('{}\n')
    host.git(project, "add", "--", "result.json")
    host.git(project, "commit", "-m", "tracked runtime result fixture")
    rec = record(project)
    (project / "result.json").write_text('{"sample": 1}\n')
    (project / ".rsi").mkdir()
    (project / ".rsi/run.log").write_text("runtime evidence\n")
    rec["file_changes"] = ["mean.py", ".rsi/run.log"]
    assert host.inspect_handoff(project, rec, handoff(kind="runtime", paths=[], commits=[], validation=[], checkpoint=""))[0] == []


@pytest.mark.parametrize("worktree", [False, True])
def test_r1_unrelated_parallel_content_and_index_can_advance_during_own_delivery(tmp_path, worktree):
    owner = repository(tmp_path / "owner")
    project = tmp_path / "worktree" if worktree else owner
    if worktree:
        host.git(owner, "worktree", "add", "-b", "codex/work", str(project))
    (project / "unstaged.txt").write_text("user work at start\n")
    rec = record(project)
    (project / "mean.py").write_text("fixed\n")
    report = handoff()
    if not worktree:
        host.git(project, "add", "--", "mean.py")
        host.git(project, "commit", "-m", "own source repair")
        report.commits = [host.git(project, "rev-parse", "HEAD").strip()]
    (project / "unstaged.txt").write_text("parallel staged progress\n")
    host.git(project, "add", "--", "unstaged.txt")
    (project / "unstaged.txt").write_text("parallel newest working progress\n")
    newest_index = host.git(project, "ls-files", "--stage", "--", "unstaged.txt")
    assert host.inspect_handoff(project, rec, report)[0] == []
    assert not rec["blocked"] and report.commits
    assert host.git(project, "ls-files", "--stage", "--", "unstaged.txt") == newest_index
    assert (project / "unstaged.txt").read_text() == "parallel newest working progress\n"
    assert host.git(project, "show", "--format=", "--name-only", report.commits[0]).split() == ["mean.py"]


@pytest.mark.parametrize("scenario,expected_calls,expected_status", [("normal",1,"complete"),("missing",2,"complete"),("stuck",2,"pending"),("interrupt",1,"interrupted"),("missing_interrupt",2,"interrupted")])
def test_one_session_feedback_is_bounded_and_status_read_does_not_run_model(tmp_path, monkeypatch, scenario, expected_calls, expected_status):
    project = repository(tmp_path / "repo")
    calls = []
    session = str(uuid.uuid4())
    def boundary(*_args, **_kwargs):
        (project / ".rsi/host-home").mkdir(parents=True, exist_ok=True)
        return []
    monkeypatch.setattr(host, "command", boundary)
    def client(_root, _project, _options, prompt, _schema, rec, **kwargs):
        calls.append({"session":kwargs["session"],"prompt":prompt})
        rec["session_id"] = session
        rec["turns"].append({"session_id":session})
        if scenario == "interrupt" or (scenario == "missing_interrupt" and len(calls) == 2):
            return None, True, -2
        first = len(calls) == 1
        if first:
            (project / "mean.py").write_text("fixed\n")
            rec["commands"].append({"id":"check","command":"python check.py","exit_code":0})
        if scenario == "stuck" or (scenario in {"missing","missing_interrupt"} and first):
            return handoff(checkpoint="").model_dump_json(), False, 0
        host.git(project, "add", "--", "mean.py")
        host.git(project, "commit", "-m", "fix")
        sha = host.git(project, "rev-parse", "HEAD").strip()
        return handoff(commits=[sha]).model_dump_json(), False, 0
    monkeypatch.setattr(host, "run_client", client)
    code = host.run(tmp_path, project, ["--model","same-model","fix"])
    assert code == (0 if scenario in {"normal","missing"} else 130 if "interrupt" in scenario else 2)
    assert len(calls) == expected_calls and calls[0]["session"] is None
    if expected_calls == 2:
        assert calls[1]["session"] == session and "missing handoff" in calls[1]["prompt"]
    profile = host.selected_profile(project)
    saved = host.prior_record(profile, session)
    assert saved["status"] == expected_status and len(saved["commands"]) <= 1
    if scenario == "missing_interrupt":
        assert saved["handoff"]["paths"] == ["mean.py"]
        assert saved["validation_basis"][0]["basis"] == "host_observed_command"
    assert host.run(tmp_path, project, [], resume=session, status=True) == 0
    assert len(calls) == expected_calls
    other = repository(tmp_path / "other")
    with pytest.raises(ValueError, match="selected development scope"):
        host.prior_record(host.selected_profile(other), session)
