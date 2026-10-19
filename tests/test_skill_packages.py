import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from robot_rsi.contracts import HarnessError, SkillQuery
from robot_rsi.registry import Registry
from robot_rsi.runtime import TOOLS, Runtime
from robot_rsi.skill_management import create


async def call(runtime, tool, **arguments):
    result = await runtime.invoke(tool, arguments)
    assert result["ok"], result
    return result["result"]


def candidate(tmp_path, name="extension", version="1.0.0", kind="code"):
    path = tmp_path / (name + "-" + version)
    create(path, name, version, kind)
    return path


def capture(runtime, name, **details):
    destination = os.environ.get("B03_EVIDENCE_ROOT")
    if not destination:
        return
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    result = {
        "qualification": "mock_fixture_only",
        "details": details,
        "operations": runtime.store.all("operation"),
        "checkpoints": runtime.store.all("task_checkpoint"),
        "index": [
            dict(row)
            for row in runtime.registry.db.execute(
                "SELECT name,version,digest,directory,deleted FROM skills"
            )
        ],
        "bindings": [dict(row) for row in runtime.registry.db.execute("SELECT * FROM bindings")],
        "pins": [dict(row) for row in runtime.registry.db.execute("SELECT * FROM pins")],
        "changes": [dict(row) for row in runtime.registry.db.execute("SELECT * FROM changes")],
        "counters": runtime.registry.counters,
    }
    (root / (name + ".json")).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


async def test_create_register_discover_code_workflow_without_core_edits(profile, tmp_path):
    core = [Path("src/robot_rsi") / name for name in ("runtime.py", "catalog.py", "mcp_server.py")]
    hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in core]
    tools = set(TOOLS)
    code = candidate(tmp_path)
    workflow = candidate(tmp_path, "workflow", kind="agent_workflow")
    async with Runtime(profile) as runtime:
        for path, name in ((code, "extension"), (workflow, "workflow")):
            registered = runtime.registry.register(path)
            runtime.registry.enable(name, "1.0.0")
            assert not runtime.registry.register(path)["changed"]
            assert registered["changed"]
        cards = await call(runtime, "skills_list", query="演示")
        assert {c["name"] for c in cards["skills"]} == {"extension", "workflow"}
        detail = await call(runtime, "skill_describe", name="extension")
        assert detail["input_schema"]["properties"]["message"]["type"] == "string"
        bad = await runtime.invoke(
            "task_run", dict(request_id="bad", skill="extension", parameters={"delay_s": -1})
        )
        assert bad["error"]["code"] == "invalid_arguments"
        prepared = await call(runtime, "task_run", request_id="prepare", skill="workflow")
        assert prepared["prepared"] and prepared["completed"] is False
        assert runtime.store.all("operation") == [] and not runtime.robot.commands
        op = await call(
            runtime,
            "task_run",
            request_id="run",
            skill="extension",
            parameters={"message": "hello"},
        )
        result = await runtime.operations.get(op["id"], 2)
        assert result["status"] == "succeeded" and result["result"]["message"] == "hello"
        assert result["skill_version"] == "1.0.0"
        assert set(TOOLS) == tools and hashes == [
            hashlib.sha256(p.read_bytes()).hexdigest() for p in core
        ]
        capture(runtime, "creation", cards=cards, detail=detail, bad=bad, prepared=prepared)


async def test_lifecycle_boundary_disable_delete_history_restart(profile, tmp_path):
    v1 = candidate(tmp_path)
    v2 = candidate(tmp_path, version="2.0.0")
    async with Runtime(profile) as runtime:
        runtime.registry.register(v1)
        runtime.registry.enable("extension", "1.0.0")
        await call(runtime, "task_begin", task_id="versioned", goal="演示")
        op = await call(
            runtime,
            "task_run",
            task_id="versioned",
            task_epoch=0,
            request_id="v1",
            skill="extension",
            parameters={"delay_s": 0.1},
        )
        manager = Registry(profile)
        try:
            manager.register(v2)
            with pytest.raises(HarnessError, match="pinned tasks") as blocked:
                manager.enable("extension", "2.0.0")
            assert blocked.value.code == "task_boundary_required"
            manager.disable("extension")
            assert (await call(runtime, "skills_list", query="演示"))["skills"] == []
            denied = await runtime.invoke(
                "task_run", dict(request_id="disabled", skill="extension")
            )
            assert denied["error"]["code"] == "unknown_skill"
            pinned = runtime.registry.resolve("extension", "versioned")
            assert pinned["version"] == "1.0.0"
            with pytest.raises(HarnessError) as referenced:
                manager.delete("extension", "1.0.0")
            assert referenced.value.code == "skill_in_use"
            done = await runtime.operations.get(op["id"], 2)
            assert done["status"] == "succeeded" and done["skill_version"] == "1.0.0"
            await call(runtime, "task_archive", task_id="versioned")
            manager.enable("extension", "2.0.0")
            with pytest.raises(HarnessError) as bound:
                manager.delete("extension", "2.0.0")
            assert bound.value.code == "skill_in_use"
            removed = manager.delete("extension", "1.0.0")
            assert removed["history_retained"]
            assert manager.describe("extension", "1.0.0", history=True)["input_schema"]
            assert not Path(manager._row("extension", "1.0.0", history=True)["directory"]).exists()
            # Candidate corruption cannot replace the active version or index.
            manifest = v2 / "skill.json"
            data = json.loads(manifest.read_text())
            data["entry"] = "skill:missing"
            manifest.write_text(json.dumps(data))
            with pytest.raises(HarnessError):
                manager.register(v2)
            assert runtime.registry.resolve("extension")["version"] == "2.0.0"
            capture(
                runtime,
                "lifecycle",
                blocked=blocked.value.payload(),
                denied=denied,
                removed=removed,
            )
        finally:
            manager.close()
    async with Runtime(profile) as runtime:
        old = await call(runtime, "task_checkpoint_get", task_id="versioned", include_archived=True)
        assert old["skill_versions"]["extension"] == "1.0.0"
        op = await call(runtime, "task_run", request_id="v2", skill="extension")
        done = await runtime.operations.get(op["id"], 2)
        assert done["skill_version"] == "2.0.0" and done["status"] == "succeeded"
        capture(runtime, "restart", historical=old)


async def test_two_scopes_share_single_artifact_and_isolate_task_memory(profile, tmp_path):
    path = candidate(tmp_path)
    async with Runtime(profile) as one:
        one.registry.register(path)
        one.registry.enable("extension", "1.0.0")
        other = profile.model_copy(update={"namespace": "second"})
        async with Runtime(other) as two:
            two.registry.enable("extension", "1.0.0")
            a = one.registry._row("extension", "1.0.0")
            b = two.registry._row("extension", "1.0.0")
            assert a["directory"] == b["directory"] and a["digest"] == b["digest"]
            assert not list(one.store.root.rglob("SKILL.md")) and not list(
                two.store.root.rglob("SKILL.md")
            )
            await call(one, "task_begin", task_id="same", goal="one")
            await call(two, "task_begin", task_id="same", goal="two")
            assert (await call(one, "task_checkpoint_get", task_id="same"))["goal"] == "one"
            assert (await call(two, "task_checkpoint_get", task_id="same"))["goal"] == "two"
            saved = two.bindings
            from dataclasses import replace

            two.bindings = replace(two.bindings, scene=None)
            two.registry.bindings = two.bindings
            two.registry._queries.clear()
            cards = await call(two, "skills_list", query="演示", available_only=False)
            assert cards["skills"][0]["available"] is False
            denied = await two.invoke("task_run", dict(request_id="missing", skill="extension"))
            assert denied["error"]["code"] == "unsupported"
            two.bindings = saved
            capture(
                one, "shared", other_scope=two.store.scope, artifact=a["directory"], denied=denied
            )


async def test_slow_entry_control_mutex_dedup_timeout_and_unknown_restart(profile, tmp_path):
    path = candidate(tmp_path)
    async with Runtime(profile) as runtime:
        runtime.registry.register(path)
        runtime.registry.enable("extension", "1.0.0")
        args = dict(request_id="slow", skill="extension", parameters={"delay_s": 1})
        op = await call(runtime, "task_run", **args)
        start = time.perf_counter()
        status = await call(runtime, "operation_get", operation_id=op["id"])
        assert time.perf_counter() - start < 0.5 and status["status"] in ("accepted", "running")
        assert (await call(runtime, "task_run", **args))["id"] == op["id"]
        busy = await runtime.invoke("task_run", {**args, "request_id": "busy"})
        assert busy["error"]["code"] == "resource_busy"
        cancelled = await call(runtime, "operation_cancel", operation_id=op["id"])
        assert cancelled["status"] == "cancelled" and cancelled["hold"]["confirmed"]
        timeout = await call(
            runtime,
            "task_run",
            request_id="timeout",
            skill="extension",
            parameters={"delay_s": 1},
            timeout_s=0.01,
        )
        done = await runtime.operations.get(timeout["id"], 2)
        assert done["status"] == "timed_out"
        interrupted = {**done, "id": "interrupted", "status": "running", "hold": None}
        runtime.store.put("operation", "interrupted", interrupted)
        capture(runtime, "control", responsive_ms=(time.perf_counter() - start) * 1000, busy=busy)
    async with Runtime(profile) as runtime:
        assert runtime.store.get("operation", "interrupted")["status"] == "unknown"
        assert runtime.operations.blocked and not runtime.robot.commands
        capture(runtime, "unknown_restart")


@pytest.mark.parametrize("size", [10, 100, 1000])
async def test_retrieval_cache_incremental_and_chinese_aliases(profile, tmp_path, size):
    async with Runtime(profile) as runtime:
        from robot_rsi.catalog import SKILLS

        fixtures = [
            {**SKILLS[1], "name": f"fixture_{i}", "description": "无关夹具", "keywords": []}
            for i in range(size - 3)
        ]
        runtime.registry.seed_fixtures(fixtures)
        for query in ("抓取", "把红色物体抓取并放到蓝色区域", "pick place"):
            assert [
                c["name"] for c in runtime.registry.cards(SkillQuery(query=query))["skills"]
            ] == ["pick_place"]
        runtime.registry.describe("pick_place")
        before = dict(runtime.registry.counters)
        for _ in range(10):
            runtime.registry.cards(SkillQuery(query="抓取"))
            runtime.registry.describe("pick_place")
        after = dict(runtime.registry.counters)
        for key in (
            "instruction_reads",
            "schema_exports",
            "entry_loads",
            "directory_scans",
            "detail_loads",
            "query_builds",
        ):
            assert after[key] == before[key]
        changed = candidate(tmp_path)
        runtime.registry.register(changed)
        runtime.registry.enable("extension", "1.0.0")
        runtime.registry.cards(SkillQuery(query="抓取"))
        assert runtime.registry.counters["query_builds"] == before["query_builds"]
        assert len(runtime.registry.cards(SkillQuery(query="演示"))["skills"]) == 1
        runtime.registry.describe("pick_place")
        assert runtime.registry.counters["detail_loads"] == before["detail_loads"]
        capture(runtime, f"cache-{size}", before=before, after=after, scale=size)


async def test_cli_and_http_use_own_schema_and_no_new_tools(profile, tmp_path):
    path = tmp_path / "cli-skill"
    profile_file = tmp_path / "profile.json"
    profile_file.write_text(profile.model_dump_json())
    prefix = [sys.executable, "-m", "robot_rsi.cli", "--profile", str(profile_file), "skill"]
    for command in (
        ["create", str(path), "cli_fixture", "--kind", "code"],
        ["register", str(path)],
        ["enable", "cli_fixture", "1.0.0"],
        ["inspect", "cli_fixture"],
    ):
        result = subprocess.run(prefix + command, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
    from robot_rsi.server import create_app

    app = create_app(profile, "test-token-for-b03-fixture-123")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://local",
            headers={"Authorization": "Bearer test-token-for-b03-fixture-123"},
        ) as client:
            bad = await client.post(
                "/tools/task_run",
                json={
                    "arguments": {
                        "request_id": "http-bad",
                        "skill": "cli_fixture",
                        "parameters": {"unknown": 1},
                    }
                },
            )
            assert bad.json()["error"]["code"] == "invalid_arguments"
            response = await client.post(
                "/tools/task_run",
                json={
                    "arguments": {
                        "request_id": "http",
                        "skill": "cli_fixture",
                        "parameters": {"message": "http"},
                    }
                },
            )
            assert response.json()["ok"]
            op = response.json()["result"]
            done = await client.post(
                "/tools/operation_get", json={"arguments": {"operation_id": op["id"], "wait_s": 2}}
            )
            assert done.json()["result"]["status"] == "succeeded"


async def test_disabled_pinned_version_restores_and_finishes_before_activation(profile, tmp_path):
    one = candidate(tmp_path)
    two = candidate(tmp_path, version="2.0.0")
    async with Runtime(profile) as runtime:
        runtime.registry.register(one)
        runtime.registry.enable("extension", "1.0.0")
        await call(runtime, "task_begin", task_id="pinned", goal="演示")
        await call(runtime, "task_context", task_id="pinned", selected_skill="extension")
        runtime.registry.disable("extension")
    async with Runtime(profile) as runtime:
        restored = await call(runtime, "task_context", task_id="pinned", selected_skill="extension")
        assert restored["selected_skill"]["version"] == "1.0.0"
        assert not runtime.robot.commands
        runtime.registry.register(two)
        with pytest.raises(HarnessError) as blocked:
            runtime.registry.enable("extension", "2.0.0")
        assert blocked.value.code == "task_boundary_required"
        op = await call(
            runtime,
            "task_run",
            task_id="pinned",
            task_epoch=0,
            request_id="restored",
            skill="extension",
        )
        done = await runtime.operations.get(op["id"], 2)
        assert done["skill_version"] == "1.0.0" and done["status"] == "succeeded"
        await call(runtime, "task_archive", task_id="pinned")
        runtime.registry.enable("extension", "2.0.0")
        capture(runtime, "pinned_restart", restored=restored)
        closed = await runtime.invoke("skill_describe", {"name": "extension"}, task_id="pinned")
        assert closed["error"]["code"] == "task_closed"
        assert (
            runtime.registry.db.execute(
                "SELECT count(*) FROM pins WHERE task=?", ("pinned",)
            ).fetchone()[0]
            == 0
        )


async def test_acceptance_transition_rolls_back_together(profile, monkeypatch):
    async with Runtime(profile) as runtime:
        original = runtime.store.event

        def fail(kind, body):
            if kind == "operation.accepted":
                raise HarnessError("fixture_storage_failure", "fixture")
            return original(kind, body)

        monkeypatch.setattr(runtime.store, "event", fail)
        response = await runtime.invoke("task_run", {"request_id": "atomic"})
        assert response["error"]["code"] == "fixture_storage_failure"
        assert runtime.store.all("operation") == []
        assert runtime.store.db.execute("SELECT count(*) FROM requests").fetchone()[0] == 0
        assert not runtime.robot.commands
        capture(runtime, "atomic_durability", response=response)
