import asyncio

import pytest

from robot_rsi.contracts import TaskLimits
from robot_rsi.runtime import Runtime


async def call(runtime, name, **arguments):
    response = await runtime.invoke(name, arguments)
    assert response["ok"], response
    return response["result"]


async def begin(runtime, task_id="loop", **overrides):
    return await call(
        runtime, "task_begin", task_id=task_id, goal="将红色积木放到蓝色盒子中", **overrides
    )


async def run(runtime, task_id="loop", epoch=0, request_id="first"):
    accepted = await call(
        runtime, "task_run", task_id=task_id, task_epoch=epoch, request_id=request_id
    )
    return await call(runtime, "operation_get", operation_id=accepted["id"], wait_s=2)


async def test_managed_failure_recovery_uses_original_goal_fresh_state_and_budget(profile):
    profile.mock_grasp_failures = 2
    async with Runtime(profile) as runtime:
        await begin(runtime)
        first = await run(runtime)
        assert first["status"] == "needs_assistance"
        context = await call(runtime, "task_context", task_id="loop", selected_skill="pick_place")
        assert context["allowed_recovery"] == [
            {"action": "retry_pick_place", "operation_id": first["id"], "task_epoch": 1}
        ]
        before = context["checkpoint"]["execution"]["tool_calls"]
        retry = {"task_id": "loop", "request_id": "recovery", **context["allowed_recovery"][0]}
        accepted = await call(runtime, "task_recover", **retry)
        done = await call(runtime, "operation_get", operation_id=accepted["id"], wait_s=2)
        assert done["status"] == "succeeded" and done["result"]["verified"]
        assert done["recovery_of"] == first["id"] and done["assistance"] == "external_agent"
        assert done["args"]["parameters"] == first["args"]["parameters"]
        assert runtime.store.get("operation", first["id"])["status"] == "needs_assistance"
        commands = list(runtime.robot.commands)
        duplicate = await call(runtime, "task_recover", **retry)
        assert duplicate["id"] == done["id"] and runtime.robot.commands == commands
        record = await call(runtime, "task_checkpoint_get", task_id="loop")
        assert record["execution"]["tool_calls"] > before
        assert record["execution"]["recoveries"] == 1
        assert len(record["operation_ids"]) == 2
        assert record["last_confirmed"].endswith("succeeded / verified")
        await call(runtime, "task_archive", task_id="loop")


async def test_checkpoint_cannot_reset_usage_or_remove_managed_operations(profile, b01_evidence):
    async with Runtime(profile) as runtime:
        initial = await begin(runtime)
        await run(runtime)
        record = await call(runtime, "task_checkpoint_get", task_id="loop")
        fields = {
            key: record[key]
            for key in (
                "task_id",
                "goal",
                "constraints",
                "skill_versions",
                "last_confirmed",
                "operation_ids",
                "unknowns",
                "remaining_budget",
                "evidence_refs",
            )
        }
        fields.update(
            operation_ids=[],
            remaining_budget={"tool_calls": 9999, "time_s": 9999, "model_tokens": 9999},
        )
        saved = await call(runtime, "task_checkpoint_save", **fields)
        assert saved["operation_ids"] == record["operation_ids"]
        assert saved["execution"]["tool_calls"] == record["execution"]["tool_calls"] + 1
        assert saved["remaining_budget"]["tool_calls"] < profile.task_limits.tool_calls
        assert saved["execution"]["deadline_at"] == initial["execution"]["deadline_at"]
        used = saved["execution"]["tool_calls"]
        deadline = saved["execution"]["deadline_at"]
    async with Runtime(profile) as runtime:
        restored = await call(runtime, "task_checkpoint_get", task_id="loop")
        assert restored["execution"]["tool_calls"] == used + 1
        assert restored["execution"]["deadline_at"] == deadline
        assert restored["operations"][0]["status"] == "succeeded"
        assert not runtime.robot.commands
        await begin(runtime, task_id="other")
        other = await call(runtime, "task_context", task_id="other")
        assert other["checkpoint"]["operation_ids"] == []
        b01_evidence(runtime, prior_used_calls=used, restored=restored, other=other)


async def test_budget_enforced_on_invalid_calls_and_deadline_survives_restart(
    profile, b01_evidence
):
    profile.active_task_id = "loop"
    profile.task_limits = TaskLimits(tool_calls=2)
    async with Runtime(profile) as runtime:
        await begin(runtime)
        invalid = await runtime.invoke("task_run", {"request_id": "bad", "skill": "weld", "task_epoch": 0})
        assert invalid["error"]["code"] == "unknown_skill"
        rejected = await runtime.invoke("task_run", {"request_id": "over", "task_epoch": 0})
        assert rejected["error"]["code"] == "task_budget_exhausted"
        record = await call(runtime, "task_checkpoint_get", task_id="loop")
        assert record["execution"]["tool_calls"] == 4
        assert not runtime.robot.commands
        b01_evidence(runtime, invalid=invalid, rejected=rejected)
        record = runtime.task_memory._load("loop")
        record["execution"]["deadline_at"] = 0
        runtime.task_memory._write(record)
    async with Runtime(profile) as runtime:
        rejected = await runtime.invoke("task_run", {"request_id": "late", "task_epoch": 0})
        assert rejected["error"]["code"] == "task_budget_exhausted"
        assert not runtime.robot.commands


async def test_task_deadline_bounds_running_operation(profile):
    profile.mock_step_s = 0.02
    profile.task_limits = TaskLimits(duration_s=0.06)
    async with Runtime(profile) as runtime:
        await begin(runtime)
        done = await run(runtime)
        assert done["status"] == "timed_out" and done["hold"]["confirmed"]


async def test_cancelled_task_rejects_late_recovery_and_plain_retry(profile, b01_evidence):
    profile.mock_grasp_failures = 2
    async with Runtime(profile) as runtime:
        await begin(runtime)
        failed = await run(runtime)
        context = await call(runtime, "task_context", task_id="loop")
        option = context["allowed_recovery"][0]
        stale = await runtime.invoke(
            "task_recover", {"task_id": "loop", "request_id": "stale", **option, "task_epoch": 0}
        )
        assert stale["error"]["code"] == "stale_task_epoch"
        plain = await runtime.invoke(
            "task_run", {"task_id": "loop", "task_epoch": 1, "request_id": "bypass"}
        )
        assert plain["error"]["code"] == "task_recovery_required"
        await call(runtime, "task_cancel", task_id="loop")
        commands = list(runtime.robot.commands)
        late = await runtime.invoke(
            "task_recover", {"task_id": "loop", "request_id": "late", **option}
        )
        assert late["error"]["code"] == "task_closed"
        assert runtime.robot.commands == commands
        assert runtime.store.get("operation", failed["id"])["status"] == "needs_assistance"
        b01_evidence(runtime, late=late, stale=stale, plain=plain)
    async with Runtime(profile) as runtime:
        late = await runtime.invoke(
            "task_recover", {"task_id": "loop", "request_id": "late-after-restart", **option}
        )
        assert late["error"]["code"] == "task_closed" and not runtime.robot.commands


async def test_cancel_while_waiting_for_observation_cannot_submit(profile):
    async with Runtime(profile) as runtime:
        await begin(runtime)
        entered, resume = asyncio.Event(), asyncio.Event()
        observe = runtime.bindings.scene.observe

        async def delayed_observe():
            entered.set()
            await resume.wait()
            return await observe()

        runtime.bindings.scene.observe = delayed_observe
        pending = asyncio.create_task(
            runtime.invoke(
                "task_run", {"task_id": "loop", "task_epoch": 0, "request_id": "late-observation"}
            )
        )
        await entered.wait()
        await call(runtime, "task_cancel", task_id="loop")
        resume.set()
        result = await pending
        assert result["error"]["code"] == "task_closed"
        assert runtime.store.all("operation") == [] and not runtime.robot.commands


async def test_unknown_failure_is_handoff_only_and_other_task_evidence_rejected(
    profile, b01_evidence
):
    async with Runtime(profile) as runtime:
        await begin(runtime)
        verify = runtime.bindings.verification.verify

        async def unknown(*args):
            result = await verify(*args)
            return result.model_copy(update={"outcome": "unknown", "retryable": False})

        runtime.bindings.verification.verify = unknown
        failed = await run(runtime)
        assert failed["error"]["code"] == "verification_unknown"
        context = await call(runtime, "task_context", task_id="loop")
        assert not context["allowed_recovery"]
        assert context["checkpoint"]["unresolved_operation_ids"] == [failed["id"]]
        commands = list(runtime.robot.commands)
        rejected = await runtime.invoke(
            "task_recover",
            {
                "task_id": "loop",
                "operation_id": failed["id"],
                "action": "retry_pick_place",
                "task_epoch": 1,
                "request_id": "unsafe",
            },
        )
        assert rejected["error"]["code"] == "recovery_not_allowed"
        await begin(runtime, task_id="other")
        rejected = await runtime.invoke(
            "task_recover",
            {
                "task_id": "other",
                "operation_id": failed["id"],
                "action": "retry_pick_place",
                "task_epoch": 0,
                "request_id": "cross-task",
            },
        )
        assert rejected["error"]["code"] == "task_mismatch"
        assert runtime.robot.commands == commands
        b01_evidence(
            runtime, context=context, rejected=rejected, command_count_before_recovery=len(commands)
        )


@pytest.mark.parametrize("recovery_limit", [0, 1])
async def test_recovery_allowance_is_enforced(profile, recovery_limit):
    profile.mock_grasp_failures = 10
    profile.task_limits = TaskLimits(recoveries=recovery_limit)
    async with Runtime(profile) as runtime:
        await begin(runtime)
        failed = await run(runtime)
        if recovery_limit:
            accepted = await call(
                runtime,
                "task_recover",
                task_id="loop",
                operation_id=failed["id"],
                action="retry_pick_place",
                task_epoch=1,
                request_id="one-recovery",
            )
            failed = await call(runtime, "operation_get", operation_id=accepted["id"], wait_s=2)
        context = await call(runtime, "task_context", task_id="loop")
        assert not context["allowed_recovery"]
        before = len(runtime.robot.commands)
        denied = await runtime.invoke(
            "task_recover",
            {
                "task_id": "loop",
                "operation_id": failed["id"],
                "action": "retry_pick_place",
                "task_epoch": context["checkpoint"]["execution"]["epoch"],
                "request_id": "over-recovery",
            },
        )
        assert denied["error"]["code"] == "recovery_not_allowed"
        assert len(runtime.robot.commands) == before


async def test_actual_client_usage_is_observation_only(profile):
    profile.task_limits = TaskLimits(model_tokens=10)
    async with Runtime(profile) as runtime:
        await begin(runtime)
        runtime.task_memory.record_usage(
            "loop",
            {"input_tokens": 9, "output_tokens": 3},
            client="fixture",
            model="reported-model",
        )
        accepted = await runtime.invoke(
            "task_run", {"task_id": "loop", "task_epoch": 0, "request_id": "over-tokens"}
        )
        assert accepted["ok"], accepted
        done = await call(runtime, "operation_get", operation_id=accepted["result"]["id"], wait_s=2)
        assert done["status"] == "succeeded"
        record = await call(runtime, "task_checkpoint_get", task_id="loop")
        assert record["execution"]["model_usage"]["total_tokens"] == 12
        assert (
            record["execution"]["model_usage"]["measurement"] == "client_report; observation_only"
        )
        assert record["remaining_budget"]["model_tokens"] is None
        assert "model_token_limit" not in record["execution"]


async def test_required_context_overflow_requests_separate_reads(profile):
    profile.task_limits = TaskLimits(context_bytes=1000)
    async with Runtime(profile) as runtime:
        await begin(runtime, constraints=["必要约束" * 30])
        result = await runtime.invoke(
            "task_context", {"task_id": "loop", "selected_skill": "pick_place"}
        )
        assert result["ok"] and result["result"]["details_required"]
        references = result["result"]["read_first"]
        assert [ref["tool"] for ref in references] == ["workspace_read", "task_checkpoint_get", "skill_describe"]
        record = await call(runtime, "task_checkpoint_get", task_id="loop")
        assert record["constraints"] == ["必要约束" * 30]
