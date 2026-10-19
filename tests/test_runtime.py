import asyncio
import time

import pytest

from robot_rsi.contracts import HarnessError
from robot_rsi.runtime import Runtime
from robot_rsi.storage import Store


async def start(runtime, **overrides):
    observation = await runtime.observe("top")
    args = {"request_id": "pick-1", "observation_id": observation["id"], **overrides}
    result = await runtime.invoke("skill_pick_place", args)
    assert result["ok"], result
    return result["result"], args


async def test_pick_place_verifies_and_deduplicates_after_expiry(profile):
    async with Runtime(profile) as runtime:
        operation, args = await start(runtime)
        done = await runtime.operations.get(operation["id"], 2)
        assert done["status"] == "succeeded"
        assert done["result"]["verified"] and done["hold"]["confirmed"]
        assert done["result"]["verification_method"] == "mock_scene_state"
        assert runtime.robot.holding["left"] is None
        assert runtime.robot.objects["red_block"]["position_m"] == [0.45, 0.18, 0.025]
        commands = len(runtime.robot.commands)
        obs = runtime.store.get("observation", args["observation_id"])
        obs["captured_at"] -= 99
        runtime.store.put("observation", obs["id"], obs)
        duplicate = await runtime.invoke("skill_pick_place", args)
        assert duplicate["result"]["id"] == operation["id"]
        assert len(runtime.robot.commands) == commands
        conflict = await runtime.invoke("skill_pick_place", {**args, "arm": "right"})
        assert conflict["error"]["code"] == "request_conflict"


@pytest.mark.parametrize(
    "failures,expected,attempts", [(1, "succeeded", 2), (2, "needs_assistance", 2)]
)
async def test_retry_budget_and_assistance(profile, failures, expected, attempts):
    profile.mock_grasp_failures = failures
    async with Runtime(profile) as runtime:
        operation, _ = await start(runtime)
        done = await runtime.operations.get(operation["id"], 2)
        assert done["status"] == expected
        assert done["hold"]["confirmed"]
        assert sum(c.get("opening") == 0 for c in runtime.robot.commands) == attempts
        if expected == "needs_assistance":
            assert done["suggested_assistance"] == "luna_class_agent"
            assert done["assistance"] == "none"  # no model was actually invoked


@pytest.mark.parametrize("delay", [0, 0.015])
async def test_cancel_stops_future_commands(profile, delay):
    profile.mock_step_s = 0.01
    async with Runtime(profile) as runtime:
        operation, _ = await start(runtime)
        if delay:
            await asyncio.sleep(delay)
        cancelled = await runtime.operations.cancel(operation["id"])
        assert cancelled["status"] == "cancelled"
        assert cancelled["hold"]["confirmed"]
        count = len(runtime.robot.commands)
        await asyncio.sleep(0.04)
        assert len(runtime.robot.commands) == count


async def test_timeout_busy_and_stale_observation(profile):
    profile.mock_step_s = 0.05
    async with Runtime(profile) as runtime:
        operation, args = await start(runtime, timeout_s=0.02)
        busy = await runtime.invoke("skill_pick_place", {**args, "request_id": "second"})
        assert busy["error"]["code"] == "resource_busy"
        done = await runtime.operations.get(operation["id"], 1)
        assert done["status"] == "timed_out" and done["hold"]["confirmed"]
        stale = await runtime.invoke("skill_pick_place", {**args, "request_id": "third"})
        assert stale["error"]["code"] == "stale_observation"
        fresh = await runtime.observe("top")
        fresh["captured_at"] = time.time() - 99
        runtime.store.put("observation", fresh["id"], fresh)
        stale = await runtime.invoke(
            "skill_pick_place", {**args, "request_id": "fourth", "observation_id": fresh["id"]}
        )
        assert stale["error"]["code"] == "stale_observation"


async def test_restart_never_replays_and_block_persists(profile):
    store = Store(profile)
    store.put("operation", "interrupted", {"id": "interrupted", "status": "running"})
    store.close()
    async with Runtime(profile) as runtime:
        assert runtime.operations.blocked
        assert runtime.store.get("operation", "interrupted")["status"] == "unknown"
        assert not runtime.robot.commands
    async with Runtime(profile) as runtime:
        assert runtime.operations.blocked
        await runtime.operations.hold()
        assert not runtime.operations.blocked
    async with Runtime(profile) as runtime:
        assert not runtime.operations.blocked
        assert not runtime.robot.commands


async def test_reboot_rejects_previous_process_observation(profile):
    async with Runtime(profile) as runtime:
        old = await runtime.observe("top")
    async with Runtime(profile) as runtime:
        result = await runtime.invoke(
            "skill_pick_place", {"request_id": "after-restart", "observation_id": old["id"]}
        )
        assert result["error"]["code"] == "stale_observation"


async def test_invalid_contracts_and_workspace(profile):
    async with Runtime(profile) as runtime:
        result = await runtime.invoke("memory_recall", {"role": "development"})
        assert result["error"]["code"] == "invalid_arguments"
        obs = await runtime.observe("top")
        args = {"request_id": "outside", "observation_id": obs["id"], "position_m": [999, 0, 0.2]}
        accepted = await runtime.invoke("robot_move", args)
        done = await runtime.operations.get(accepted["result"]["id"], 1)
        assert done["error"]["code"] == "workspace_limit"
        assert not runtime.robot.commands
        result = await runtime.invoke("robot_move", {**args, "position_m": [float("nan"), 0, 0]})
        assert result["error"]["code"] == "invalid_arguments"


async def test_exclusive_runtime_scope(profile):
    async with Runtime(profile):
        with pytest.raises(HarnessError, match="Another runtime"):
            Runtime(profile)


async def test_task_goal_needs_no_agent_supplied_observation(profile):
    async with Runtime(profile) as runtime:
        station = await runtime.invoke("station_describe")
        assert station["result"]["supported_tasks"] == ["pick_place"]
        args = {
            "request_id": "high-level-task",
            "skill": "pick_place",
            "parameters": {"object_id": "red_block", "target_region": "blue_box"},
        }
        accepted = await runtime.invoke("task_run", args)
        assert accepted["ok"], accepted
        done = await runtime.operations.get(accepted["result"]["id"], 2)
        assert done["status"] == "succeeded"
        count = len(runtime.store.all("observation"))
        duplicate = await runtime.invoke("task_run", args)
        assert duplicate["result"]["id"] == done["id"]
        assert len(runtime.store.all("observation")) == count
