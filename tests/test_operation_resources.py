"""B03-R1: selected resources govern device admission and operation cleanup."""

import asyncio
import json
import os
from pathlib import Path

import pytest

from robot_rsi.capabilities import SkillBindings
from robot_rsi.runtime import Runtime
from robot_rsi.skill_management import create

CODE = """import asyncio
from pydantic import Field
from robot_rsi.contracts import Contract

class Parameters(Contract):
    value: int
    delay_s: float = Field(default=0, ge=0, le=1)
    fail: bool = False

async def run(context, args):
    context.stage("calculate", None)
    await asyncio.sleep(args.delay_s)
    if args.fail:
        raise RuntimeError("fixture interruption")
    return {"value": args.value + 1}
"""


def register(runtime, tmp_path, *, name="pure_calculation", requires=None, code=CODE):
    path = tmp_path / name
    create(path, name, "1.0.0", "code")
    manifest = path / "skill.json"
    data = json.loads(manifest.read_text())
    data.update(resources=[], requires=requires or {}, description="计算夹具", keywords=["计算"])
    manifest.write_text(json.dumps(data))
    (path / "skill.py").write_text(code)
    runtime.registry.register(path)
    runtime.registry.enable(name, "1.0.0")


async def call(runtime, tool, **arguments):
    response = await runtime.invoke(tool, arguments)
    assert response["ok"], response
    return response["result"]


def capture(runtime, name, **details):
    destination = os.environ.get("B03_R1_EVIDENCE_ROOT")
    if not destination:
        return
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    body = {
        "qualification": "deterministic_mock_fixture",
        "details": details,
        "operations": runtime.store.all("operation"),
        "checkpoints": runtime.store.all("task_checkpoint"),
        "observation_count": len(runtime.store.all("observation")),
        "device_epoch": runtime.robot.epoch,
        "robot_commands": runtime.robot.commands,
        "execution_blocked": runtime.operations.blocked,
        "pins": [dict(row) for row in runtime.registry.db.execute("SELECT * FROM pins")],
    }
    (root / (name + ".json")).write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n")


@pytest.mark.parametrize("no_scene", [False, True])
async def test_pure_code_works_without_motion_or_implicit_device_use(
    profile, tmp_path, monkeypatch, no_scene
):
    factory = (lambda *_: SkillBindings(scene=None)) if no_scene else None
    async with Runtime(profile, bindings_factory=factory) as runtime:
        monkeypatch.setattr(runtime.robot, "supports_motion", False)
        counts = {"observe": 0, "hold": 0}

        async def forbidden_observe(*_):
            counts["observe"] += 1
            raise AssertionError("implicit observe")

        async def forbidden_hold():
            counts["hold"] += 1
            raise AssertionError("implicit hold")

        monkeypatch.setattr(runtime.robot, "read_state", forbidden_observe)
        monkeypatch.setattr(runtime.robot, "hold", forbidden_hold)
        register(runtime, tmp_path)
        tools = {t["name"]: t for t in runtime.tools()["tools"]}
        assert tools["task_run"]["available"] and not tools["skill_pick_place"]["available"]
        await call(runtime, "task_begin", task_id="calculation", goal="计算")
        context = await call(
            runtime, "task_context", task_id="calculation", selected_skill="pure_calculation"
        )
        assert context["selected_skill"]["available"]
        assert [c["name"] for c in context["skill_candidates"]] == ["pure_calculation"]
        descriptors = {s["name"]: s for s in runtime.skill_descriptors()}
        assert (
            descriptors["pure_calculation"]["available"]
            and not descriptors["pick_place"]["available"]
        )
        prepared = await call(runtime, "task_run", request_id="workflow", skill="workspace_context")
        assert prepared["prepared"] and prepared["completed"] is False
        args = dict(
            request_id="calculate",
            skill="pure_calculation",
            parameters={"value": 3},
            task_id="calculation",
            task_epoch=0,
        )
        op = await call(runtime, "task_run", **args)
        done = await runtime.operations.get(op["id"], 2)
        assert done["status"] == "succeeded" and done["result"] == {"value": 4}
        assert done["resources"] == [] and done["hold"] is None
        assert (await call(runtime, "task_run", **args))["id"] == op["id"]
        assert counts == {"observe": 0, "hold": 0} and runtime.robot.epoch == 0
        assert runtime.store.all("observation") == []
        assert (
            runtime.registry.db.execute(
                "SELECT count(*) FROM pins WHERE task LIKE 'operation:%'"
            ).fetchone()[0]
            == 0
        )
        capture(
            runtime,
            f"pure-{no_scene}",
            counts=counts,
            tools=tools,
            context=context,
            prepared=prepared,
        )


@pytest.mark.parametrize("delay", [0, 0.01])
async def test_nonmotion_cancel_before_or_after_start_does_not_hold(profile, tmp_path, delay):
    async with Runtime(profile) as runtime:
        register(runtime, tmp_path)
        op = await call(
            runtime,
            "task_run",
            request_id="cancel",
            skill="pure_calculation",
            parameters={"value": 3, "delay_s": 1},
        )
        if delay:
            await asyncio.sleep(delay)
        cancelled = await call(runtime, "operation_cancel", operation_id=op["id"])
        assert cancelled["status"] == "cancelled" and cancelled["hold"] is None
        assert runtime.robot.epoch == 0 and not runtime.operations.blocked
        assert not runtime.store.all("observation") and not runtime.robot.commands
        capture(runtime, f"cancel-{delay}")


async def test_nonmotion_completion_and_cancel_do_not_stop_unrelated_motion(profile, tmp_path):
    profile.mock_step_s = 0.03
    async with Runtime(profile) as runtime:
        register(runtime, tmp_path)
        math = await call(
            runtime,
            "task_run",
            request_id="parallel-math",
            skill="pure_calculation",
            parameters={"value": 10, "delay_s": 0.02},
        )
        motion = await call(runtime, "task_run", request_id="motion", skill="pick_place")
        other = await call(
            runtime,
            "task_run",
            request_id="other-math",
            skill="pure_calculation",
            parameters={"value": 1, "delay_s": 1},
        )
        assert (await runtime.operations.get(math["id"], 1))["status"] == "succeeded"
        await call(runtime, "operation_cancel", operation_id=other["id"])
        assert runtime.robot.epoch == 0
        assert (await runtime.operations.get(motion["id"]))["status"] == "running"
        busy = await runtime.invoke("task_run", {"request_id": "conflict", "skill": "pick_place"})
        assert busy["error"]["code"] == "resource_busy"
        done = await runtime.operations.get(motion["id"], 2)
        assert (
            done["status"] == "succeeded"
            and done["result"]["verified"]
            and done["hold"]["confirmed"]
        )
        assert done["resources"] == ["manipulator"] and runtime.robot.epoch == 1
        capture(runtime, "resource-exclusion", busy=busy)


async def test_explicit_robot_hold_preserves_unrelated_nonmotion_operation(profile, tmp_path):
    profile.mock_step_s = 0.03
    async with Runtime(profile) as runtime:
        register(runtime, tmp_path)
        math = await call(
            runtime,
            "task_run",
            request_id="math",
            skill="pure_calculation",
            parameters={"value": 0, "delay_s": 1},
        )
        motion = await call(runtime, "task_run", request_id="motion", skill="pick_place")
        stopped = await call(runtime, "robot_hold")
        assert stopped["confirmed"]
        assert (await runtime.operations.get(motion["id"]))["status"] == "cancelled"
        assert (await runtime.operations.get(math["id"]))["status"] in ("accepted", "running")
        epoch = runtime.robot.epoch
        await call(runtime, "operation_cancel", operation_id=math["id"])
        assert runtime.robot.epoch == epoch
        capture(runtime, "explicit-hold", epoch_after_explicit_hold=epoch)


async def test_readonly_code_observes_only_when_entry_requests_it(profile, tmp_path, monkeypatch):
    code = CODE.replace(
        '    return {"value": args.value + 1}',
        '    observation = await context.bindings.scene.observe()\n    return {"observation_id": observation.id}',
    )
    async with Runtime(profile) as runtime:
        monkeypatch.setattr(runtime.robot, "supports_motion", False)
        register(runtime, tmp_path, name="read_scene", requires={"scene.observe": 1}, code=code)
        op = await call(
            runtime, "task_run", request_id="read", skill="read_scene", parameters={"value": 0}
        )
        done = await runtime.operations.get(op["id"], 2)
        assert done["status"] == "succeeded" and done["result"]["observation_id"]
        assert len(runtime.store.all("observation")) == 1
        assert runtime.robot.epoch == 0 and done["hold"] is None
        capture(runtime, "readonly-explicit-observation")


async def test_nonmotion_unknown_survives_restart_without_replay_or_robot_block(profile, tmp_path):
    async with Runtime(profile) as runtime:
        register(runtime, tmp_path)
        args = dict(request_id="interrupted", skill="pure_calculation", parameters={"value": 0})
        op = await call(runtime, "task_run", **args)
        done = await runtime.operations.get(op["id"], 2)
        # Deterministic crash fixture: the persisted operation never recorded its terminal response.
        done.update(status="running", result=None)
        runtime.store.put("operation", done["id"], done)
    async with Runtime(profile) as runtime:
        assert not runtime.operations.blocked and runtime.robot.epoch == 0
        restored = await call(runtime, "task_run", **args)
        assert (
            restored["status"] == "unknown" and restored["error"]["code"] == "runtime_interrupted"
        )
        assert not runtime.store.all("observation")
        failure = await call(
            runtime,
            "task_run",
            request_id="failed",
            skill="pure_calculation",
            parameters={"value": 0, "fail": True},
        )
        failed = await runtime.operations.get(failure["id"], 2)
        assert failed["status"] == "unknown" and failed["hold"] is None
        assert not runtime.operations.blocked and runtime.robot.epoch == 0
        motion = await call(runtime, "task_run", request_id="motion", skill="pick_place")
        assert (await runtime.operations.get(motion["id"], 2))["status"] == "succeeded"
        await call(runtime, "robot_hold")
        assert not runtime.store.get("operation", op["id"]).get("hold_reconciled")
        capture(runtime, "unknown-restart", restored=restored)


async def test_cli_waits_for_nonmotion_result_without_device_cleanup(profile, tmp_path):
    import subprocess
    import sys

    async with Runtime(profile) as runtime:
        register(runtime, tmp_path)
    path = tmp_path / "profile.json"
    path.write_text(profile.model_dump_json())
    command = [
        sys.executable,
        "-m",
        "robot_rsi.cli",
        "--profile",
        str(path),
        "call",
        "task_run",
        json.dumps(
            {"request_id": "cli-math", "skill": "pure_calculation", "parameters": {"value": 9}}
        ),
    ]
    completed = subprocess.run(command, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)["result"]
    assert result["status"] == "succeeded" and result["result"] == {"value": 10}
    assert result["resources"] == [] and result["hold"] is None
    async with Runtime(profile) as runtime:
        assert runtime.store.all("observation") == [] and not runtime.robot.commands
        capture(runtime, "cli-result", command=command, result=result)
