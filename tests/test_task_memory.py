import pytest

from robot_rsi.runtime import Runtime


@pytest.fixture
def checkpoint():
    return {
        "task_id": "task-a",
        "goal": "将物体放到指定区域",
        "constraints": ["保留右侧区域"],
        "skill_versions": {"pick_place": "0.2.0"},
        "last_confirmed": "已选定抓放技能，尚未执行",
        "operation_ids": [],
        "unknowns": [],
        "remaining_budget": {"tool_calls": 12, "time_s": 60, "model_tokens": None},
        "evidence_refs": [],
    }


async def call(runtime, name, **arguments):
    response = await runtime.invoke(name, arguments)
    assert response["ok"], response
    return response["result"]


async def test_checkpoint_save_restart_finish_and_archive(profile, checkpoint):
    async with Runtime(profile) as runtime:
        saved = await call(runtime, "task_checkpoint_save", **checkpoint)
        assert saved["revision"] == 1 and saved["changed"]
        assert saved["capabilities"] == runtime.bindings.catalog()
        assert not runtime.robot.commands
        repeated = await call(runtime, "task_checkpoint_save", **checkpoint)
        assert not repeated["changed"] and repeated["revision"] == 1
        operation = await call(runtime, "task_run", request_id="checkpoint-pick")
        done = await call(runtime, "operation_get", operation_id=operation["id"], wait_s=2)
        assert done["status"] == "succeeded"
        lesson = await call(
            runtime, "memory_remember", claim="模拟抓放证据", operation_id=done["id"]
        )
        checkpoint.update(
            last_confirmed="已验证放置",
            operation_ids=[done["id"]],
            remaining_budget={"tool_calls": 8, "time_s": 40, "model_tokens": None},
            evidence_refs=[
                {"kind": "observation", "id": done["result"]["observation_id"]},
                {"kind": "memory", "id": lesson["id"]},
            ],
        )
        saved = await call(runtime, "task_checkpoint_save", **checkpoint)
        assert saved["revision"] == 2
        assert not runtime.store.all("workspace_note")
        assert len(runtime.store.all("task_checkpoint")) == 1

    async with Runtime(profile) as runtime:
        restored = await call(runtime, "task_checkpoint_get", task_id="task-a")
        for key, value in checkpoint.items():
            assert restored[key] == value
        assert restored["capabilities"] == saved["capabilities"]
        assert restored["operations"][0]["status"] == "succeeded"
        assert restored["operations"][0]["verified"] is True
        assert restored["unresolved_operation_ids"] == []
        archive = await call(runtime, "task_archive", task_id="task-a")
        again = await call(runtime, "task_archive", task_id="task-a")
        assert archive == again
        missing = await runtime.invoke("task_checkpoint_get", {"task_id": "task-a"})
        assert missing["error"]["code"] == "not_found"
        reference = archive["reference"]
        archived = await call(runtime, reference["tool"], **reference["arguments"])
        assert archived["archived"] and archived["goal"] == checkpoint["goal"]
        rejected = await runtime.invoke("task_checkpoint_save", checkpoint)
        assert rejected["error"]["code"] == "task_archived"
        assert runtime.robot.commands == []

    async with Runtime(profile) as runtime:
        archived = await call(runtime, "task_checkpoint_get", task_id="task-a", include_archived=True)
        assert archived["archived"] and archived["revision"] == 3
        assert runtime.robot.commands == []


@pytest.mark.parametrize("status", ["running", "unknown", "needs_assistance"])
async def test_unresolved_operation_truth_survives_restart_and_summary_updates(
    profile, checkpoint, status
):
    async with Runtime(profile) as runtime:
        # Persist an interrupted/uncertain operation without starting a coroutine or sending motion.
        runtime.store.put(
            "operation",
            "uncertain-op",
            {
                "id": "uncertain-op",
                "status": status,
                "stage": "verify_placement",
                "tool": "task_run",
                "skill_name": "pick_place",
                "skill_version": "0.2.0",
                "qualification": "synthetic",
                "error": {"code": "verification_unknown"} if status == "needs_assistance" else None,
            },
        )
        checkpoint.update(operation_ids=["uncertain-op"], unknowns=["放置结果未确认"])
        saved = await call(runtime, "task_checkpoint_save", **checkpoint)
        assert saved["operations"][0]["status"] == status
        assert saved["unresolved_operation_ids"] == ["uncertain-op"]

    async with Runtime(profile) as runtime:
        restored = await call(runtime, "task_checkpoint_get", task_id="task-a")
        expected = "unknown" if status == "running" else status
        assert restored["operations"][0]["status"] == expected
        assert restored["unknowns"] == ["放置结果未确认"]
        assert restored["remaining_budget"] == checkpoint["remaining_budget"]
        # A caller's optimistic summary cannot erase the unknown operation or alter its result.
        checkpoint.update(operation_ids=[], unknowns=[], last_confirmed="模型摘要声称完成")
        updated = await call(runtime, "task_checkpoint_save", **checkpoint)
        assert updated["unresolved_operation_ids"] == ["uncertain-op"]
        assert updated["operations"][0]["status"] == expected
        assert updated["operations"][0]["verified"] is None
        assert runtime.store.get("operation", "uncertain-op")["status"] == expected
        await call(runtime, "task_archive", task_id="task-a")
        archived = await call(runtime, "task_checkpoint_get", task_id="task-a", include_archived=True)
        assert archived["unresolved_operation_ids"] == ["uncertain-op"]
        assert runtime.robot.commands == []


async def test_checkpoint_reads_live_operation_state_without_loading_other_tasks(profile, checkpoint):
    async with Runtime(profile) as runtime:
        runtime.store.put("operation", "pending", {"id": "pending", "status": "running"})
        await call(runtime, "task_checkpoint_save", **{**checkpoint, "operation_ids": ["pending"]})
        other = {**checkpoint, "task_id": "task-b", "goal": "另一任务"}
        await call(runtime, "task_checkpoint_save", **other)
        task_b = await call(runtime, "task_checkpoint_get", task_id="task-b")
        assert task_b["goal"] == "另一任务" and task_b["operations"] == []
        assert task_b["unresolved_operation_ids"] == []
        runtime.store.put("operation", "pending", {"id": "pending", "status": "succeeded"})
        task_a = await call(runtime, "task_checkpoint_get", task_id="task-a")
        assert task_a["operations"][0]["status"] == "succeeded"
        assert task_a["unresolved_operation_ids"] == []
        brief = await call(runtime, "workspace_read")
        assert "task-a" not in str(brief) and "task-b" not in str(brief)
        assert (await call(runtime, "memory_recall"))["records"] == []
        assert runtime.robot.commands == []


@pytest.mark.parametrize(
    "scope_change", [{"namespace": "other"}, {"station": "other"}, {"role": "evaluation"}]
)
async def test_checkpoint_and_evidence_cannot_cross_scope(profile, checkpoint, scope_change):
    async with Runtime(profile) as runtime:
        await call(runtime, "task_checkpoint_save", **checkpoint)
        artifact = runtime.store.artifact(b"task-a evidence", "text/plain")
        runtime.store.put("operation", "private-op", {"id": "private-op", "status": "unknown"})
    async with Runtime(profile.model_copy(update=scope_change)) as runtime:
        for name, args in (
            ("task_checkpoint_get", {"task_id": "task-a"}),
            ("task_archive", {"task_id": "task-a"}),
            ("task_checkpoint_save", {**checkpoint, "operation_ids": ["private-op"]}),
            (
                "task_checkpoint_save",
                {**checkpoint, "evidence_refs": [{"kind": "artifact", "id": artifact}]},
            ),
        ):
            response = await runtime.invoke(name, args)
            assert response["error"]["code"] == "not_found"
        assert runtime.store.all("task_checkpoint") == []
        fresh = await call(runtime, "task_checkpoint_save", **{**checkpoint, "goal": "本 scope 任务"})
        assert fresh["goal"] == "本 scope 任务" and fresh["operations"] == []


async def test_checkpoint_pins_versions_and_binding_and_rejects_false_evidence(profile, checkpoint):
    async with Runtime(profile) as runtime:
        saved = await call(runtime, "task_checkpoint_save", **checkpoint)
        wrong_version = {**checkpoint, "skill_versions": {"pick_place": "0.1.0"}}
        rejected = await runtime.invoke("task_checkpoint_save", wrong_version)
        assert rejected["error"]["code"] == "skill_version_conflict"
        runtime.store.put(
            "operation",
            "old-op",
            {
                "id": "old-op",
                "status": "unknown",
                "skill_name": "pick_place",
                "skill_version": "0.1.0",
            },
        )
        rejected = await runtime.invoke(
            "task_checkpoint_save", {**checkpoint, "operation_ids": ["old-op"]}
        )
        assert rejected["error"]["code"] == "skill_version_conflict"
        # A new process/catalog may change; checkpoint metadata still identifies the original task.
        runtime.task_memory.skill_versions = {"pick_place": "0.3.0"}
        runtime.task_memory.capabilities = [{"provider_id": "replacement"}]
        updated = await call(
            runtime, "task_checkpoint_save", **{**checkpoint, "skill_versions": {}, "last_confirmed": "等待"}
        )
        assert updated["skill_versions"] == saved["skill_versions"]
        assert updated["capabilities"] == saved["capabilities"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"task_id": None},
        {"namespace": "other"},
        {"operations": [{"id": "fake", "status": "succeeded"}]},
        {"remaining_budget": {"tool_calls": -1, "time_s": 0, "model_tokens": None}},
        {"remaining_budget": {}},
        {"goal": "x" * 501},
    ],
)
async def test_checkpoint_validates_required_context_and_host_scope(profile, checkpoint, overrides):
    async with Runtime(profile) as runtime:
        result = await runtime.invoke("task_checkpoint_save", {**checkpoint, **overrides})
        assert result["error"]["code"] == "invalid_arguments"
        assert not runtime.store.all("task_checkpoint")
        result = await runtime.invoke("task_checkpoint_get")
        assert result["error"]["code"] == "invalid_arguments"
