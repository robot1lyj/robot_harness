"""C11 targeted whole-goal, independent pixel result and method handoff checks."""
import asyncio
import copy
import json

import pytest

from robot_rsi.contracts import Profile, TaskCheckpointSave, TaskCheckpointGet
from robot_rsi.delivery import check_method
from robot_rsi.runtime import Runtime

pytest.importorskip("inspection_foundation")
pytest.importorskip("multipoint_inspection")


def parameters(xs=(0.3, 0.7), **extra):
    return {"points": [{"id": f"p{i}", "joints": {"carriage_x": x, "carriage_y": 0.1, "turntable": 0}} for i, x in enumerate(xs)],
            "resource_id": "bench", "arm": "bridge_3", "model_ref": {"id": "fixture_model", "version": "v1"}, **extra}


def contract(goal="巡检全部输入点并确认正常", args=None, kind="one_off"):
    args = args or parameters()
    return {"original_goal": goal, "kind": kind, "criteria": [
        {"id": "report", "description": "Visit all input points and report image observations", "required": True},
        {"id": "normal", "description": "Every point is normal by image evidence", "required": args.get("mode", "confirm_normal") == "confirm_normal"}],
        "entry": {"skill": "multipoint_inspection", "version": "0.1.1", "parameters": args}, "remaining": []}


def configured(profile, **options):
    return profile.model_copy(update={"robot": None, "host": Profile(host={"adapter": {"factory": "inspection_foundation", "options": options}, "model_ref": {"id": "fixture_model", "version": "v1"}}).host})


async def call(runtime, tool, **args):
    response = await runtime.invoke(tool, args)
    assert response["ok"], response
    return response["result"]


async def prepare(runtime, task="inspection", args=None, declaration=True):
    runtime.registry.discover()
    runtime.registry.enable("multipoint_inspection", "0.1.1")
    await call(runtime, "resource_connect", resource_id="bench")
    goal = "巡检全部输入点并生成报告" if (args or {}).get("mode") == "report" else "巡检全部输入点并确认正常"
    kwargs = {"delivery": contract(goal=goal, args=args)} if declaration else {}
    return await call(runtime, "task_begin", task_id=task, goal=goal, **kwargs)


async def run(runtime, task="inspection", args=None):
    accepted = await call(runtime, "task_run", task_id=task, task_epoch=0, request_id=task + "-run", skill="multipoint_inspection", parameters=args or parameters())
    return await runtime.operations.get(accepted["id"], 5)


async def finish(runtime, op, **extra):
    record = runtime.task_memory._load(op["task_id"])
    return await runtime.invoke("task_finish", {"task_id": op["task_id"], "task_epoch": record["execution"]["epoch"], "outcome": "succeeded", "summary": "Entire goal evidenced", "operation_ids": [op["id"]], **extra})


@pytest.mark.parametrize("options,expected,samples,can_finish", [
    ({}, "passed", 2, True),
    ({"transient_dark_frames": 1}, "passed", 4, True),
    ({"scenes": {"0.7": "anomaly"}}, "failed", 2, False),
    ({"scenes": {"0.7": "unreadable"}}, "unknown", 3, False)])
async def test_commands_and_observations_are_separate(profile, options, expected, samples, can_finish):
    async with Runtime(configured(profile, **options)) as runtime:
        await prepare(runtime)
        op = await run(runtime)
        assert op["status"] == "succeeded" and op["hold"]["confirmed"]
        assert len(runtime.robot.commands) == 2
        assert op["result"]["moves"] == 2 and op["result"]["samples"] == samples
        report = json.loads(runtime.store.read_artifact(op["result"]["report_id"]))
        assert report["normality"] == expected
        assert all(p["motion"]["feedback"]["status"] == "completed" for p in report["points"])
        assert not runtime.task_memory.get(TaskCheckpointGet(task_id="inspection"))["unresolved_operation_ids"]
        result = await finish(runtime, op)
        assert result["ok"] == can_finish, result
        if not can_finish:
            assert result["error"]["code"] == "goal_not_satisfied"
            forged = copy.deepcopy(op["result"]["criterion_results"])
            forged[1].update(status="passed", remaining=[])
            assert (await finish(runtime, op, criterion_results=forged))["error"]["code"] == "criterion_conflict"


async def test_report_goal_can_succeed_with_anomaly(profile):
    args = parameters(mode="report")
    async with Runtime(configured(profile, scenes={"0.7": "anomaly"})) as runtime:
        await prepare(runtime, args=args)
        op = await run(runtime, args=args)
        assert op["result"]["criterion_results"][1]["status"] == "failed"
        assert (await finish(runtime, op))["ok"]


async def test_goal_save_context_delta_detail_restart_and_archive(profile):
    profile = configured(profile)
    async with Runtime(profile) as runtime:
        await prepare(runtime, declaration=False)
        record = runtime.task_memory._load("inspection")
        original = {key: record.get(key) for key in TaskCheckpointSave.model_fields}
        changed = {**original, "goal": "只生成报告"}
        with pytest.raises(Exception, match="original goal"):
            runtime.task_memory.save(TaskCheckpointSave.model_validate(changed))
        runtime.task_memory.save(TaskCheckpointSave.model_validate({**original, "delivery": contract()}))
        full = await call(runtime, "task_context", task_id="inspection", blocks=True)
        assert full["blocks"]["task"]["delivery"]["original_goal"] == record["goal"]
        previous = runtime.task_memory._load("inspection")
        saved = {key: previous.get(key) for key in TaskCheckpointSave.model_fields}
        saved["delivery"]["remaining"] = ["Need end-to-end run"]
        runtime.task_memory.save(TaskCheckpointSave.model_validate(saved))
        delta = await call(runtime, "task_context", task_id="inspection", blocks=True, delivered=full["delivered"])
        assert delta["blocks"]["task"]["delivery"]["remaining"] == ["Need end-to-end run"]
        bad = copy.deepcopy(saved)
        bad["delivery"]["criteria"] = bad["delivery"]["criteria"][:1]
        with pytest.raises(Exception, match="criteria are fixed"):
            runtime.task_memory.save(TaskCheckpointSave.model_validate(bad))
    async with Runtime(profile) as runtime:
        assert not runtime.robot.commands
        detail = await call(runtime, "task_checkpoint_get", task_id="inspection")
        assert detail["delivery"]["remaining"] == ["Need end-to-end run"]
        saved["delivery"]["remaining"] = []
        runtime.task_memory.save(TaskCheckpointSave.model_validate(saved))
        await call(runtime, "resource_connect", resource_id="bench")
        op = await run(runtime)
        assert (await finish(runtime, op))["ok"]
        await call(runtime, "task_archive", task_id="inspection")
        assert (await call(runtime, "task_checkpoint_get", task_id="inspection", include_archived=True))["task_result"]["delivery"]["criteria"] == contract()["criteria"]


async def test_cancel_and_unknown_do_not_replay(profile):
    async with Runtime(configured(profile, delay_s=0.08)) as runtime:
        await prepare(runtime)
        accepted = await call(runtime, "task_run", task_id="inspection", task_epoch=0, request_id="cancel", skill="multipoint_inspection", parameters=parameters((0.3, 0.7, 0.8)))
        for _ in range(100):
            if len(runtime.robot.commands) == 2:
                break
            await asyncio.sleep(0.005)
        cancelled = await call(runtime, "task_cancel", task_id="inspection")
        op = await runtime.operations.get(accepted["id"])
        assert op["status"] == "cancelled" and op["hold"]["confirmed"]
        events = [json.loads(row[0]) for row in runtime.store.db.execute("SELECT body FROM events WHERE kind='operation.stage'")]
        last = [e["data"] for e in events if "confirmed" in e["data"]][-1]
        assert last["confirmed"] == ["p0"] and last["remaining"] == ["p1", "p2"]
        assert op["stage_data"] == last
        count = len(runtime.robot.commands)
        rejected = await runtime.invoke("task_run", {"task_id": "inspection", "task_epoch": cancelled["execution"]["epoch"], "request_id": "no-replay", "skill": "multipoint_inspection", "parameters": parameters()})
        assert not rejected["ok"] and len(runtime.robot.commands) == count
    async with Runtime(configured(profile, disconnect_after_submit=True).model_copy(update={"namespace": "unknown"})) as runtime:
        await prepare(runtime)
        op = await run(runtime)
        assert op["status"] == "unknown" and op["hold"]["confirmed"]
        assert len(runtime.robot.commands) == 1
        assert not (await finish(runtime, op))["ok"]
        assert runtime.task_memory.recovery_options(runtime.task_memory._load("inspection")) == []


async def test_method_report_structural_checks_and_runtime_finish(profile, tmp_path, monkeypatch):
    async with Runtime(configured(profile)) as runtime:
        await prepare(runtime, task="first")
        one = await run(runtime, task="first")
        await prepare(runtime, task="reuse", args=parameters((0.4, 0.9)))
        two = await run(runtime, task="reuse", args=parameters((0.4, 0.9)))
        registration = await call(runtime, "skill_describe", name="multipoint_inspection")
        report = {"registration": registration, "runs": [{"source": "Runtime task_run synthetic", "operation": op} for op in (one, two)], "remaining": []}
        spec = contract(kind="reusable_method")
        assert check_method(spec, report) == []
        assert check_method(spec, {"registration": None, "runs": []})
        assert check_method(spec, {"registration": registration, "runs": [None]})
        from robot_rsi.coding_host import Handoff, inspect_handoff, start_git, git
        project = tmp_path / "coding-project"
        project.mkdir()
        git(project, "init", "-b", "codex/method")
        git(project, "config", "user.name", "Offline fixture")
        git(project, "config", "user.email", "fixture@example.invalid")
        (project / "method-report.json").write_text(json.dumps(report))
        git(project, "add", "--", "method-report.json")
        git(project, "commit", "-m", "existing method reports")
        record = {"start": start_git(project), "commands": [], "goal": spec["original_goal"]}
        def forbidden(*_args, **_kwargs):
            raise AssertionError("Parent must not load plugins or rerun actions")
        monkeypatch.setattr("robot_rsi.registry.symbol", forbidden)
        handoff = Handoff(kind="development", summary="Method delivered", paths=[], validation=[],
            checkpoint="existing runs", commits=[], commit_message="", no_commit_reason=None,
            delivery=spec, method_evidence="method-report.json")
        missing, checked = inspect_handoff(project, record, handoff)
        assert not missing and checked[0]["basis"] == "existing_method_report_structure_only"
        # Restore loading before exercising the normal Runtime method once more.
        monkeypatch.undo()

        assert check_method(spec, {**report, "runs": report["runs"][:1]})
        previous = runtime.task_memory._load("first")
        # Existing declaration cannot change kind; create a method-development task.
        await call(runtime, "task_begin", task_id="method", goal=spec["original_goal"], delivery=spec)
        op = await run(runtime, task="method")
        identity = runtime.store.artifact(json.dumps(report).encode(), "application/json")
        assert (await finish(runtime, op, method_evidence={"kind": "artifact", "id": identity}))["ok"]
        assert previous["delivery"]["kind"] == "one_off"


def test_coding_parent_reads_report_without_import_or_action(tmp_path):
    from robot_rsi.coding_host import Handoff, inspect_handoff, start_git, git
    project = tmp_path
    git(project, "init", "-b", "codex/fixture")
    git(project, "config", "user.name", "Offline fixture")
    git(project, "config", "user.email", "fixture@example.invalid")
    (project / "baseline.txt").write_text("fixture")
    git(project, "add", "--", "baseline.txt")
    git(project, "commit", "-m", "baseline")
    record = {"start": start_git(project), "commands": [], "goal": "巡检全部输入点并确认正常"}
    handoff = Handoff(kind="development", summary="method", paths=[], validation=[], checkpoint="", commits=[], commit_message="", no_commit_reason=None, delivery=contract(kind="reusable_method"))
    missing, _ = inspect_handoff(project, record, handoff)
    assert any("Reusable method requires" in m for m in missing)
    one_off = handoff.model_copy(update={"delivery": __import__('robot_rsi.contracts', fromlist=['GoalDelivery']).GoalDelivery.model_validate(contract())})
    missing, _ = inspect_handoff(project, {"start": start_git(project), "commands": [], "goal": record["goal"]}, one_off)
    assert any("Required criterion" in m for m in missing)

    # Ordinary code-free delivery remains valid without a method contract.
    assert inspect_handoff(project, {"start": start_git(project), "commands": []}, handoff.model_copy(update={"delivery": None}))[0] == []


async def test_selected_sources_cannot_hide_an_earlier_negative(profile):
    from robot_rsi.delivery import operation_results, check_results
    async with Runtime(profile) as runtime:
        ref = runtime.store.artifact(b"existing domain evidence", "text/plain")
        for identity, status in [("earlier", "failed"), ("later", "passed")]:
            runtime.store.put("operation", identity, {"result": {"criterion_results": [{
                "criterion_id": "report", "status": status, "source": "domain observation",
                "evidence_refs": [{"kind": "artifact", "id": ref}], "remaining": []}]}})
        results = operation_results(runtime.store, ["earlier", "later"])
        assert results[0]["status"] == "failed"
        assert check_results({**contract(), "criteria": contract()["criteria"][:1]}, results)
