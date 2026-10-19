import asyncio
import json
import os
from pathlib import Path

import pytest
from mcp import ClientSession

from robot_rsi.agent_host import AgentHost, connect_mcp, control_call
from robot_rsi.contracts import AgentConfig, AgentRole, HarnessError
from robot_rsi.runtime import Runtime
from robot_rsi.storage import Store


async def call(runtime, name, **args):
    result = await runtime.invoke(name, args)
    assert result["ok"], result
    return result["result"]


async def begin(runtime):
    return await call(runtime, "task_begin", task_id="session-task", goal="抓取红色积木放到蓝盒")


async def finish_goal(runtime, operation_id, session_id=None):
    record = runtime.task_memory._load("session-task")
    response = await runtime.invoke(
        "task_finish",
        {
            "task_id": "session-task",
            "task_epoch": record["execution"]["epoch"],
            "outcome": "succeeded",
            "summary": "整个目标已完成，抓取与放置由操作证据验证",
            "operation_ids": [operation_id],
        },
        agent_session_id=session_id,
    )
    assert response["ok"], response


class FakeClient:
    def __init__(self, action):
        self.action = action
        self.turns = []

    def binding(self, role, config):
        return {
            "client": "fixture",
            "role": role,
            "model": config.model or "fixture-model",
            "reasoning_effort": None,
        }

    async def run(self, turn, emit):
        self.turns.append({k: turn[k] for k in ("id", "task_id", "task_epoch", "binding")})
        emit({"type": "thread.started", "thread_id": "native-" + turn["id"]})
        result = await self.action(turn)
        emit({"type": "item.completed", "item": {"type": "reasoning", "text": "not retained"}})
        emit(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 10, "output_tokens": 2, "cached_input_tokens": 4},
            }
        )
        return result or {"completed": True, "returncode": 0}


def capture(runtime, name, **details):
    destination = os.environ.get("B04_EVIDENCE_ROOT")
    if not destination:
        return
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    result = {
        "source": "deterministic_protocol_fixture",
        "details": details,
        "sessions": runtime.store.all("agent_session"),
        "requests": runtime.store.all("assistance_request"),
        "checkpoints": runtime.store.all("task_checkpoint"),
        "operations": runtime.store.all("operation"),
        "commands": runtime.robot.commands,
        "events": [
            dict(r)
            for r in runtime.store.db.execute(
                "SELECT kind,body FROM events WHERE kind LIKE 'agent.%'"
            )
        ],
    }
    (root / (name + ".json")).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


async def test_one_owner_http_client_completion_and_usage(profile):
    async with Runtime(profile) as runtime:
        await begin(runtime)

        async def action(turn):
            with pytest.raises(HarnessError) as busy:
                Store(profile)
            assert busy.value.code == "scope_busy"
            async with connect_mcp(turn["url"], turn["token"]) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    reply = await session.call_tool(
                        "task_run",
                        {"arguments": {"request_id": "normal", "task_epoch": turn["task_epoch"]}},
                    )
                    assert not reply.isError, reply
                    op = reply.structuredContent["result"]
                    done = await session.call_tool(
                        "operation_get", {"arguments": {"operation_id": op["id"], "wait_s": 2}}
                    )
                    assert (
                        not done.isError
                        and done.structuredContent["result"]["status"] == "succeeded"
                    )
                    epoch = runtime.task_memory._load("session-task")["execution"]["epoch"]
                    finished = await session.call_tool(
                        "task_finish",
                        {
                            "arguments": {
                                "task_id": "session-task",
                                "task_epoch": epoch,
                                "outcome": "succeeded",
                                "summary": "已完成整个抓放目标，有放置验证和 HOLD 证据",
                                "operation_ids": [op["id"]],
                            }
                        },
                    )
                    assert not finished.isError, finished
                    archived = await session.call_tool(
                        "task_archive", {"arguments": {"task_id": "session-task"}}
                    )
                    assert not archived.isError

        client = FakeClient(action)
        async with AgentHost(runtime).serve() as host:
            result = await host.run("session-task", client=client)
        assert result["status"] == "complete" and len(client.turns) == 1
        saved = runtime.task_memory._load("session-task")
        assert saved["archived"] and saved["execution"]["model_usage"]["total_tokens"] == 12
        sessions = runtime.store.all("agent_session")
        assert sessions[0]["native_session_id"].startswith("native-session_")
        assert sessions[0]["metrics"]["tool_ms"] > 0
        assert not any(
            "not retained" in row[0] for row in runtime.store.db.execute("SELECT body FROM events")
        )
        assert not (runtime.store.root / "agent-control.json").exists()
        capture(runtime, "one-owner", turns=client.turns, result=result)


async def test_fast_recovery_teacher_gap_operator_unknown_routing(profile):
    profile.mock_grasp_failures = 2
    profile.agent = AgentConfig(
        fast=AgentRole(model="fast-fixture"), teacher=AgentRole(model="teacher-fixture")
    )
    async with Runtime(profile) as runtime:
        await begin(runtime)

        async def action(turn):
            arguments = {
                "task_id": "session-task",
                "task_epoch": turn["task_epoch"],
                "request_id": turn["id"],
            }
            request = runtime.agent_state.request("session-task")
            if request["allowed_recovery"]:
                response = await runtime.invoke(
                    "task_recover",
                    {**arguments, **request["allowed_recovery"][0]},
                    agent_session_id=turn["id"],
                )
            else:
                response = await runtime.invoke("task_run", arguments, agent_session_id=turn["id"])
            assert response["ok"], response
            done = await runtime.operations.get(response["result"]["id"], 2)
            if done["status"] == "succeeded":
                await finish_goal(runtime, done["id"], turn["id"])

        client = FakeClient(action)
        async with AgentHost(runtime).serve() as host:
            result = await host.run("session-task", client=client)
        assert result["status"] == "complete"
        assert [t["binding"]["role"] for t in client.turns] == ["fast", "fast"]
        assert all(t["binding"]["model"] == "fast-fixture" for t in client.turns)
        assert runtime.task_memory._load("session-task")["execution"]["recoveries"] == 1
        capture(runtime, "fast-recovery", turns=client.turns)
    other = profile.model_copy(update={"namespace": "gap"})
    async with Runtime(other) as runtime:
        await call(runtime, "task_begin", task_id="session-task", goal="焊接未知材料")

        async def no_progress(turn):
            pass

        client = FakeClient(no_progress)
        async with AgentHost(runtime).serve() as host:
            result = await host.run("session-task", client=client)
        assert result["status"] == "handoff" and result["request"]["role"] == "operator"
        assert [t["binding"]["role"] for t in client.turns] == ["teacher"]
        assert not runtime.robot.commands and not runtime.store.all("operation")
        capture(runtime, "teacher-gap", turns=client.turns, result=result)


async def test_disconnect_reconnect_preserves_budget_versions_no_replay(profile):
    async with Runtime(profile) as runtime:
        await begin(runtime)

        async def disconnect(turn):
            await call(runtime, "task_context", task_id="session-task", selected_skill="pick_place")
            raise ConnectionError("fixture disconnect")

        client = FakeClient(disconnect)
        async with AgentHost(runtime).serve() as host:
            result = await host.run("session-task", client=client)
        assert result["status"] == "disconnected"
        prior = runtime.task_memory._load("session-task")
        capture(runtime, "disconnect", result=result)
    async with Runtime(profile) as runtime:
        restored = runtime.task_memory._load("session-task")
        assert (
            restored["execution"] == prior["execution"]
            and restored["skill_versions"] == prior["skill_versions"]
        )

        async def resume(turn):
            response = await runtime.invoke(
                "task_run",
                {
                    "task_id": "session-task",
                    "task_epoch": turn["task_epoch"],
                    "request_id": "resume",
                },
                agent_session_id=turn["id"],
            )
            assert response["ok"], response
            await runtime.operations.get(response["result"]["id"], 2)
            await finish_goal(runtime, response["result"]["id"], turn["id"])

        client = FakeClient(resume)
        async with AgentHost(runtime).serve() as host:
            result = await host.run("session-task", client=client)
        assert result["status"] == "complete"
        saved = runtime.task_memory._load("session-task")
        assert saved["execution"]["deadline_at"] == prior["execution"]["deadline_at"]
        assert (
            saved["skill_versions"] == prior["skill_versions"]
            and len(runtime.store.all("operation")) == 1
        )
        assert len(runtime.store.all("agent_session")) == 2
        capture(runtime, "reconnect", prior=prior, turns=client.turns)


async def test_operator_claim_return_and_old_grant_cannot_learn_new_epoch(profile):
    async with Runtime(profile) as runtime:
        await begin(runtime)
        session = runtime.agent_state.start(
            "session-task", "fast", {"client": "fixture", "model": "old"}
        )
        request = runtime.agent_state.request("session-task")
        kwargs = {
            "task_id": "session-task",
            "request_id": request["id"],
            "task_epoch": 0,
            "operator": "human",
        }
        forbidden = await runtime.invoke("assistance_claim", kwargs, agent_session_id=session["id"])
        assert forbidden["error"]["code"] == "operator_required"
        claimed = await runtime.invoke("assistance_claim", kwargs, actor="operator")
        assert claimed["ok"] and claimed["result"]["claim_epoch"] == 1
        returned = await runtime.invoke(
            "assistance_return",
            {**kwargs, "task_epoch": 1, "disposition": "resume", "note": "fixture checked"},
            actor="operator",
        )
        assert returned["ok"] and returned["result"]["task_epoch"] == 2
        late = await runtime.invoke(
            "task_run",
            {"task_id": "session-task", "task_epoch": 2, "request_id": "late"},
            agent_session_id=session["id"],
        )
        assert late["error"]["code"] == "stale_agent_session" and not runtime.robot.commands
        assert runtime.task_memory._load("session-task")["execution"]["deadline_at"]
        capture(runtime, "operator-return", claimed=claimed, returned=returned, late=late)


async def test_unknown_to_operator_cancel_late_and_handoff(profile):
    async with Runtime(profile) as runtime:
        await begin(runtime)
        op = await call(
            runtime, "task_run", task_id="session-task", task_epoch=0, request_id="unknown"
        )
        done = await runtime.operations.get(op["id"], 2)
        done.update(status="unknown", hold=None, error={"code": "verification_unknown"})
        runtime.store.put("operation", done["id"], done)
        request = runtime.agent_state.request("session-task")
        assert request["role"] == "operator"
        client = FakeClient(lambda _: None)
        async with AgentHost(runtime).serve() as host:
            result = await host.run("session-task", client=client)
        assert result["status"] == "handoff" and client.turns == []
        args = {
            "task_id": "session-task",
            "request_id": request["id"],
            "task_epoch": request["task_epoch"],
            "operator": "human",
        }
        assert (await runtime.invoke("assistance_claim", args, actor="operator"))["ok"]
        rejected = await runtime.invoke(
            "assistance_return",
            {
                **args,
                "task_epoch": request["task_epoch"] + 1,
                "disposition": "resume",
                "note": "HOLD alone is not effect verification",
            },
            actor="operator",
        )
        assert rejected["error"]["code"] == "unresolved_effects"
        returned = await runtime.invoke(
            "assistance_return",
            {
                **args,
                "task_epoch": request["task_epoch"] + 1,
                "disposition": "handoff",
                "note": "unknown retained",
            },
            actor="operator",
        )
        assert returned["ok"] and returned["result"]["unknowns_retained"]
        assert runtime.store.get("operation", done["id"])["status"] == "unknown"
        capture(runtime, "unknown-operator", request=request, rejected=rejected, returned=returned)


async def test_control_endpoint_cancel_revokes_running_client(profile):
    profile.active_task_id = "session-task"
    async with Runtime(profile) as runtime:
        await begin(runtime)
        ready = asyncio.Event()

        async def waits(turn):
            ready.set()
            await asyncio.sleep(10)

        client = FakeClient(waits)
        async with AgentHost(runtime).serve() as host:
            pending = asyncio.create_task(host.run("session-task", client=client))
            await ready.wait()
            result = await control_call(profile, "task_cancel", {"task_id": "session-task"})
            assert result["ok"]
            stopped = await asyncio.wait_for(pending, 2)
        assert stopped["status"] == "superseded"
        assert (
            not runtime.robot.commands
            and runtime.task_memory._load("session-task")["execution"]["epoch"] == 1
        )
        capture(runtime, "cancel-inflight", result=stopped)


async def test_missing_role_returns_material_without_model_call(profile):
    async with Runtime(profile) as runtime:
        await call(runtime, "task_begin", task_id="session-task", goal="焊接零件")
        client = FakeClient(lambda _: None)
        async with AgentHost(runtime).serve() as host:
            result = await host.run("session-task", client=client)
        assert result["request"]["role"] == "teacher" and result["request"]["configured"] is False
        assert client.turns == []
        capture(runtime, "missing-role", result=result)


async def test_fast_to_teacher_handoff_keeps_owner_and_usage(profile):
    profile.agent = AgentConfig(
        fast=AgentRole(model="fast-model"), teacher=AgentRole(model="teacher-model")
    )
    async with Runtime(profile) as runtime:
        await begin(runtime)
        original = runtime.task_memory._load("session-task")

        async def action(turn):
            await call(runtime, "task_context", task_id="session-task", selected_skill="pick_place")
            if turn["binding"]["role"] == "teacher":
                response = await runtime.invoke(
                    "task_run",
                    {
                        "task_id": "session-task",
                        "task_epoch": turn["task_epoch"],
                        "request_id": "teacher-action",
                    },
                    agent_session_id=turn["id"],
                )
                assert response["ok"], response
                await runtime.operations.get(response["result"]["id"], 2)
                await finish_goal(runtime, response["result"]["id"], turn["id"])

        client = FakeClient(action)
        async with AgentHost(runtime).serve() as host:
            result = await host.run("session-task", client=client)
        assert result["status"] == "complete"
        assert [t["binding"]["role"] for t in client.turns] == ["fast", "teacher"]
        saved = runtime.task_memory._load("session-task")
        assert saved["execution"]["deadline_at"] == original["execution"]["deadline_at"]
        assert saved["skill_versions"] == {"pick_place": "0.2.0"}
        assert saved["execution"]["model_usage"]["total_tokens"] == 24
        capture(runtime, "role-handoff", turns=client.turns, original=original)


async def test_interrupted_session_and_operation_restore_unknown_without_replay(profile):
    async with Runtime(profile) as runtime:
        await begin(runtime)
        session = runtime.agent_state.start(
            "session-task", "fast", {"client": "fixture", "model": "fast"}
        )
        response = await runtime.invoke(
            "task_run",
            {"task_id": "session-task", "task_epoch": 0, "request_id": "interrupted"},
            agent_session_id=session["id"],
        )
        done = await runtime.operations.get(response["result"]["id"], 2)
        done.update(status="running", result=None, hold=None)
        runtime.store.put("operation", done["id"], done)
        prior = runtime.task_memory._load("session-task")
    async with Runtime(profile) as runtime:
        client = FakeClient(lambda _: None)
        async with AgentHost(runtime).serve() as host:
            result = await host.run("session-task", client=client)
        assert (
            result["request"]["role"] == "operator"
            and not client.turns
            and not runtime.robot.commands
        )
        restored = runtime.task_memory._load("session-task")
        assert (
            restored["execution"] == prior["execution"]
            and restored["skill_versions"] == prior["skill_versions"]
        )
        assert runtime.store.get("operation", done["id"])["status"] == "unknown"
        assert runtime.store.get("agent_session", session["id"])["status"] == "disconnected"
        capture(runtime, "interrupted-owner", prior=prior, result=result)


async def test_one_inflight_and_completion_report_is_idempotent(profile):
    async with Runtime(profile) as runtime:
        await begin(runtime)
        session = runtime.agent_state.start(
            "session-task", "fast", {"client": "fixture", "model": "fast"}
        )
        with pytest.raises(HarnessError) as busy:
            runtime.agent_state.start(
                "session-task", "teacher", {"client": "fixture", "model": "teacher"}
            )
        assert busy.value.code == "agent_busy"
        runtime.agent_state.client_event(
            session, {"type": "turn.completed", "usage": {"input_tokens": 9, "output_tokens": 1}}
        )
        first = runtime.agent_state.finish(
            session, {"completed": True, "returncode": 0}, elapsed_s=0.01
        )
        second = runtime.agent_state.finish(
            session, {"completed": True, "returncode": 0}, elapsed_s=0.01
        )
        assert (
            first == second
            and runtime.task_memory._load("session-task")["execution"]["model_usage"][
                "total_tokens"
            ]
            == 10
        )
        capture(runtime, "one-inflight", first=first)


async def test_http_model_grant_cannot_take_operator_role_or_reuse_new_epoch(profile):
    profile.active_task_id = "session-task"
    async with Runtime(profile) as runtime:
        await begin(runtime)
        model = runtime.agent_state.start(
            "session-task", "fast", {"client": "fixture", "model": "fast"}
        )
        request = runtime.agent_state.request("session-task")
        async with AgentHost(runtime).serve() as host:
            token = "fixture-model-token"
            host.grants[token] = {"agent_session_id": model["id"]}
            assert (runtime.store.root / "agent-control.json").stat().st_mode & 0o777 == 0o600
            args = {
                "task_id": "session-task",
                "request_id": request["id"],
                "task_epoch": 0,
                "operator": "human",
            }
            async with connect_mcp(host.url, token) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    forbidden = await session.call_tool("assistance_claim", {"arguments": args})
                    assert forbidden.isError and "operator_required" in forbidden.content[0].text
                    assert (await control_call(profile, "assistance_claim", args))["ok"]
                    returned = await control_call(
                        profile,
                        "assistance_return",
                        {
                            **args,
                            "task_epoch": 1,
                            "disposition": "resume",
                            "note": "explicit operator return",
                        },
                    )
                    assert returned["ok"]
                    late = await session.call_tool(
                        "task_run", {"arguments": {"request_id": "late-http", "task_epoch": 2}}
                    )
                    assert late.isError and "stale_agent_session" in late.content[0].text
            with pytest.raises(HarnessError) as rejected:
                await control_call(
                    profile,
                    "assistance_return",
                    {
                        **args,
                        "task_epoch": 1,
                        "disposition": "resume",
                        "note": "duplicate stale return",
                    },
                )
            assert rejected.value.code == "control_rejected"
        assert not runtime.robot.commands
        capture(runtime, "http-control-fence", returned=returned)


async def test_legacy_checkpoint_resume_adopts_remaining_allowance_without_reset(profile):
    from robot_rsi.agent_host import run_agent

    profile.agent = AgentConfig(fast=None)
    async with Runtime(profile) as runtime:
        await call(
            runtime,
            "task_checkpoint_save",
            task_id="legacy",
            goal="抓取物体",
            constraints=["原约束"],
            skill_versions={"pick_place": "0.2.0"},
            last_confirmed="尚未执行",
            operation_ids=[],
            unknowns=[],
            remaining_budget={"tool_calls": 3, "time_s": 20, "model_tokens": None},
            evidence_refs=[],
        )
        request = await call(runtime, "assistance_get", task_id="legacy")
        assert request[
            "reason"
        ] == "task_not_started" and "execution" not in runtime.task_memory._load("legacy")
    result = await run_agent(profile, "legacy")
    assert result["status"] == "handoff" and result["request"]["configured"] is False
    async with Runtime(profile) as runtime:
        restored = runtime.task_memory._load("legacy")
        assert restored["execution"]["tool_call_limit"] == 3
        assert restored["execution"]["deadline_at"] - restored["execution"]["started_at"] <= 20.01
        assert restored["skill_versions"] == {"pick_place": "0.2.0"} and restored[
            "constraints"
        ] == ["原约束"]
        assert not runtime.robot.commands
        capture(runtime, "legacy-adoption", result=result)
