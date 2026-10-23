import json
import os
from pathlib import Path

import pytest

from robot_rsi.agent_host import AgentHost
from robot_rsi.contracts import AgentConfig
from robot_rsi.runtime import Runtime
from robot_rsi.skill_management import create


async def call(runtime, name, **arguments):
    response = await runtime.invoke(name, arguments)
    assert response["ok"], response
    return response["result"]


async def begin(runtime, task_id="whole-goal"):
    return await call(
        runtime, "task_begin", task_id=task_id, goal="先计算目标，再抓取红色积木并放进蓝盒"
    )


async def conclude(runtime, op_ids, *, outcome="succeeded", summary="整个当前目标已完成"):
    record = runtime.task_memory._load("whole-goal")
    return await runtime.invoke(
        "task_finish",
        {
            "task_id": "whole-goal",
            "task_epoch": record["execution"]["epoch"],
            "outcome": outcome,
            "summary": summary,
            "operation_ids": op_ids,
        },
    )


class NoModel:
    def binding(self, *_):
        raise AssertionError("No extra model decision is needed")


async def run_host(runtime):
    async with AgentHost(runtime).serve() as host:
        return await host.run("whole-goal", client=NoModel())


def capture(runtime, name, **details):
    destination = os.environ.get("B04_R1_EVIDENCE_ROOT")
    if not destination:
        return
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    data = {
        "qualification": "deterministic_mock_fixture",
        "details": details,
        "checkpoints": runtime.store.all("task_checkpoint"),
        "operations": runtime.store.all("operation"),
        "sessions": runtime.store.all("agent_session"),
        "robot_commands": runtime.robot.commands,
    }
    (root / (name + ".json")).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


async def test_successful_preparation_stays_pending_and_resume_only_runs_next_step(
    profile, tmp_path
):
    profile.agent = AgentConfig(fast=None)
    path = tmp_path / "prepare"
    create(path, "prepare_target", "1.0.0", "code")
    manifest = path / "skill.json"
    data = json.loads(manifest.read_text())
    data.update(resources=[], requires={})
    manifest.write_text(json.dumps(data))
    (path / "skill.py").write_text("""from robot_rsi.contracts import Empty as Parameters
async def run(context, args):
    return {"prepared": True, "placement_verified": False, "region": "blue_box"}
""")
    args = {
        "task_id": "whole-goal",
        "task_epoch": 0,
        "request_id": "prepare",
        "skill": "prepare_target",
    }
    async with Runtime(profile) as runtime:
        runtime.registry.register(path)
        runtime.registry.enable("prepare_target", "1.0.0")
        await begin(runtime)
        accepted = await call(runtime, "task_run", **args)
        prepared = await runtime.operations.get(accepted["id"], 2)
        assert prepared["status"] == "succeeded"
        # A selected-source checkpoint explicitly records the unfinished physical goal.
        from robot_rsi.contracts import TaskCheckpointSave

        previous = runtime.task_memory._load("whole-goal")
        saved = {key: previous[key] for key in TaskCheckpointSave.model_fields}
        saved["last_confirmed"] = "仅完成目标准备，尚未执行抓取或放置。"
        runtime.task_memory.save(TaskCheckpointSave.model_validate(saved))
        result = await run_host(runtime)
        assert result["status"] == "handoff" and result["request"]["reason"] == "task_unfinished"
        assert not runtime.task_memory._load("whole-goal")["archived"]
        assert not runtime.robot.commands and not runtime.store.all("agent_session")
        rejected = await conclude(runtime, [prepared["id"]])
        assert rejected["error"]["code"] == "inapplicable_evidence"
        prior = runtime.task_memory._load("whole-goal")
        capture(runtime, "preparation-pending", result=result, rejected=rejected)
    async with Runtime(profile) as runtime:
        restored = runtime.task_memory._load("whole-goal")
        assert restored["execution"]["deadline_at"] == prior["execution"]["deadline_at"]
        assert restored["skill_versions"] == prior["skill_versions"]
        assert restored["last_confirmed"] == "仅完成目标准备，尚未执行抓取或放置。"
        assert not runtime.robot.commands
        assert (await call(runtime, "task_run", **args))["id"] == prepared["id"]
        repeat = await runtime.invoke(
            "task_run", {**args, "request_id": "unmarked-repeat", "task_epoch": 1}
        )
        assert repeat["error"]["code"] == "task_recovery_required"
        placed = await call(
            runtime,
            "task_run",
            task_id="whole-goal",
            task_epoch=1,
            request_id="place",
            skill="pick_place",
            after_operation_id=prepared["id"],
        )
        done = await runtime.operations.get(placed["id"], 2)
        assert done["status"] == "succeeded" and done["result"]["verified"]
        finished = await conclude(runtime, [prepared["id"], done["id"]])
        assert finished["ok"], finished
        assert finished["result"]["task_outcome"]["outcome"] == "succeeded"
        assert not finished["result"]["archived"]
        commands = list(runtime.robot.commands)
        host = await run_host(runtime)
        assert (
            host["status"] == "complete" and not runtime.task_memory._load("whole-goal")["archived"]
        )
        assert runtime.robot.commands == commands and not runtime.store.all("agent_session")
        await call(runtime, "task_archive", task_id="whole-goal")
        assert (await run_host(runtime))["status"] == "complete"
        capture(runtime, "preparation-continuation", prior=prior, result=host, repeat=repeat)


@pytest.mark.parametrize(
    "outcome", ["cancelled", "failed", "unknown", "incomplete", "legacy_success"]
)
async def test_archived_outcome_is_not_changed_to_success(profile, outcome):
    async with Runtime(profile) as runtime:
        await begin(runtime)
        if outcome == "legacy_success":
            op = await call(
                runtime, "task_run", task_id="whole-goal", task_epoch=0, request_id="old-success"
            )
            assert (await runtime.operations.get(op["id"], 2))["result"]["verified"]
        elif outcome == "cancelled":
            await call(runtime, "task_cancel", task_id="whole-goal")
        elif outcome in ("failed", "unknown"):
            assert (await conclude(runtime, [], outcome=outcome, summary="目标未完成，保留此结论"))[
                "ok"
            ]
        await call(runtime, "task_archive", task_id="whole-goal")
        expected = "incomplete" if outcome == "legacy_success" else outcome
        result = await run_host(runtime)
        status = await call(runtime, "agent_session_get", task_id="whole-goal")
        assert result["status"] == expected
        assert status["checkpoint"]["task_outcome"]["outcome"] == expected
        assert not runtime.store.all("agent_session")
        capture(runtime, "archived-" + outcome, result=result, status=status)
    async with Runtime(profile) as runtime:
        assert (await run_host(runtime))["status"] == expected
        assert not runtime.robot.commands


async def test_explicit_success_is_final_and_resume_has_no_model_or_action(profile):
    async with Runtime(profile) as runtime:
        await begin(runtime)
        op = await call(
            runtime, "task_run", task_id="whole-goal", task_epoch=0, request_id="verified"
        )
        done = await runtime.operations.get(op["id"], 2)
        response = await conclude(runtime, [done["id"]], summary="全部目标步骤已由关联操作证明完成")
        assert response["ok"]
        original = response["result"]["task_result"]
        assert (await conclude(runtime, [done["id"]], summary="全部目标步骤已由关联操作证明完成"))[
            "result"
        ]["task_result"] == original
        changed = await conclude(runtime, [], outcome="failed")
        assert changed["error"]["code"] == "task_result_conflict"
        cancelled = await call(runtime, "task_cancel", task_id="whole-goal")
        assert cancelled["task_outcome"]["outcome"] == "succeeded"
        await call(runtime, "task_archive", task_id="whole-goal")
        before = runtime.task_memory._load("whole-goal")
        capture(runtime, "explicit-success", original=original)
    async with Runtime(profile) as runtime:
        result = await run_host(runtime)
        assert result["status"] == "complete" and not runtime.robot.commands
        after = runtime.task_memory._load("whole-goal")
        assert after["task_result"] == original
        assert after["execution"] == before["execution"]
        assert not runtime.store.all("agent_session") and len(runtime.store.all("operation")) == 1
        capture(runtime, "completed-resume", result=result)


async def test_completion_requires_applicable_nonactive_known_evidence(profile):
    profile.mock_step_s = 0.03
    async with Runtime(profile) as runtime:
        await begin(runtime)
        missing = await conclude(runtime, [])
        assert missing["error"]["code"] == "completion_evidence_required"
        op = await call(
            runtime, "task_run", task_id="whole-goal", task_epoch=0, request_id="active"
        )
        active = await conclude(runtime, [op["id"]])
        assert active["error"]["code"] == "operation_active"
        done = await runtime.operations.get(op["id"], 2)
        other = await runtime.invoke(
            "task_finish",
            {
                "task_id": "whole-goal",
                "task_epoch": 0,
                "outcome": "succeeded",
                "summary": "late",
                "operation_ids": [op["id"]],
            },
        )
        assert other["error"]["code"] == "stale_task_epoch"
        actual = dict(done)
        actual.update(status="unknown", error={"code": "verification_unknown"})
        runtime.store.put("operation", op["id"], actual)
        unknown = await conclude(runtime, [op["id"]])
        assert unknown["error"]["code"] == "unresolved_effects"
        await call(runtime, "task_archive", task_id="whole-goal")
        assert (await run_host(runtime))["status"] == "unknown"
        capture(runtime, "reject-active-unknown", missing=missing, active=active, unknown=unknown)


async def test_evidence_must_match_task_version_binding_and_latest_step(profile):
    async with Runtime(profile) as runtime:
        await begin(runtime)
        op = await call(
            runtime, "task_run", task_id="whole-goal", task_epoch=0, request_id="source"
        )
        done = await runtime.operations.get(op["id"], 2)
        wrong = await conclude(runtime, ["not-linked"])
        assert wrong["error"]["code"] == "task_mismatch"
        for field, value in [
            ("task_id", "another-task"),
            ("skill_version", "obsolete"),
            ("capabilities", []),
        ]:
            runtime.store.put("operation", done["id"], {**done, field: value})
            response = await conclude(runtime, [done["id"]])
            assert response["error"]["code"] == "inapplicable_evidence"
        runtime.store.put("operation", done["id"], done)
        assert (await conclude(runtime, [done["id"]]))["ok"]
        capture(runtime, "evidence-applicability")


async def test_workflow_can_conclude_with_sourced_material_without_fake_operation(profile):
    async with Runtime(profile) as runtime:
        await call(runtime, "task_begin", task_id="whole-goal", goal="记录托盘说明")
        await call(
            runtime,
            "workspace_note",
            key="tray",
            title="托盘",
            text="蓝盒存放红色积木",
            source="用户提供",
        )
        result = await call(
            runtime,
            "task_finish",
            task_id="whole-goal",
            task_epoch=0,
            outcome="succeeded",
            summary="已按用户要求记录托盘说明",
            evidence_refs=[{"kind": "workspace_note", "id": "tray"}],
        )
        assert result["task_outcome"]["outcome"] == "succeeded"
        assert not runtime.store.all("operation") and not runtime.robot.commands
        assert (await run_host(runtime))["status"] == "complete"
        snapshot = result["task_result"]["evidence_snapshots"][0]
        await call(
            runtime,
            "workspace_note",
            key="tray",
            title="托盘",
            text="后续任务改变的资料",
            source="后续用户更新",
        )
        frozen = json.loads(runtime.store.read_artifact(snapshot["artifact_id"]))
        assert frozen["text"] == "蓝盒存放红色积木" and frozen["revision"] == snapshot["revision"]
        capture(runtime, "workflow-ending", snapshot=snapshot)


async def test_new_workspace_keeps_registered_foundation_version_until_host_discovery(profile):
    async with Runtime(profile) as runtime:
        # Recreate a pool from before task_memory 0.4.0, retaining its published snapshot.
        with runtime.registry.db:
            row = runtime.registry.db.execute(
                "SELECT * FROM skills WHERE name='task_memory'"
            ).fetchone()
            body = json.loads(row["body"])
            body["version"] = "0.3.0"
            card = json.loads(row["card"])
            card["version"] = "0.3.0"
            runtime.registry.db.execute(
                "UPDATE skills SET version='0.3.0',body=?,card=? WHERE name='task_memory'",
                (json.dumps(body), json.dumps(card)),
            )
            runtime.registry.db.execute(
                "UPDATE bindings SET version='0.3.0' WHERE name='task_memory'"
            )
    other = profile.model_copy(update={"namespace": "new-workspace"})
    async with Runtime(other) as runtime:
        assert runtime.registry.resolve("task_memory")["version"] == "0.3.0"
        runtime.registry.discover()
        runtime.registry.enable("task_memory", "0.6.0")
        assert runtime.registry.resolve("task_memory")["version"] == "0.6.0"
        assert "task_finish" in runtime.registry.resolve("task_memory")["instructions"]
