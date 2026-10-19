import json
import time

from robot_rsi.contracts import MemoryMaintenance, Profile, TaskLimits
from robot_rsi.runtime import Runtime


async def call(runtime, tool, **arguments):
    response = await runtime.invoke(tool, arguments)
    assert response["ok"], response
    return response["result"]


async def begin(runtime, task_id="adaptive"):
    return await call(
        runtime, "task_begin", task_id=task_id, goal="抓取并放置", constraints=["保留右侧区域"]
    )


def snapshot_input(runtime, task_id="adaptive"):
    record = runtime.task_memory._load(task_id)
    return {
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


def current(runtime, task_id="adaptive"):
    state = runtime.task_memory._load(task_id)["memory_state"]
    path = runtime.store.root / state["file_ref"]
    return state, path


async def test_legacy_token_quota_is_ignored_across_restart(profile, b02_evidence):
    legacy_profile = profile.model_dump()
    legacy_profile["task_limits"]["model_tokens"] = 1
    profile = Profile.model_validate(legacy_profile)
    assert "model_tokens" not in profile.task_limits.model_dump()
    assert "model_tokens" not in TaskLimits.model_json_schema()["properties"]
    async with Runtime(profile) as runtime:
        await begin(runtime)
        record = runtime.task_memory._load("adaptive")
        record["execution"]["model_token_limit"] = 1
        runtime.store.put("task_checkpoint", "task-checkpoint_adaptive", record)
        runtime.task_memory.record_usage(
            "adaptive",
            {
                "input_tokens": 100,
                "output_tokens": 20,
                "cached_input_tokens": 80,
                "cache_write_input_tokens": 2,
                "reasoning_output_tokens": 10,
            },
            client="reported-fixture",
            model="reported-model",
        )
    async with Runtime(profile) as runtime:
        restored = await call(runtime, "task_checkpoint_get", task_id="adaptive")
        assert "model_token_limit" not in restored["execution"]
        assert restored["remaining_budget"]["model_tokens"] is None
        accepted = await call(
            runtime, "task_run", task_id="adaptive", task_epoch=0, request_id="after-old-quota"
        )
        done = await call(runtime, "operation_get", operation_id=accepted["id"], wait_s=2)
        assert done["status"] == "succeeded"
        usage = runtime.task_memory._load("adaptive")["execution"]["model_usage"]
        assert usage["total_tokens"] == 120 and usage["cached_input_tokens"] == 80
        assert usage["cache_write_input_tokens"] == 2 and usage["reasoning_output_tokens"] == 10
        b02_evidence(runtime, restored=restored, reported_usage=usage)


async def test_no_change_keeps_file_revision_mtime_and_model_calls(profile, b02_evidence):
    async with Runtime(profile) as runtime:
        await begin(runtime)
        state, path = current(runtime)
        baseline = {
            "revision": state["revision"],
            "mtime_ns": path.stat().st_mtime_ns,
            "writes": state["writes"],
        }
        checkpoint_revision = runtime.task_memory._load("adaptive")["revision"]
        for _ in range(8):
            await call(runtime, "task_context", task_id="adaptive")
        fields = snapshot_input(runtime)
        fields["constraints"] *= 3
        saved = await call(runtime, "task_checkpoint_save", **fields)
        state, path = current(runtime)
        assert state["revision"] == baseline["revision"]
        assert path.stat().st_mtime_ns == baseline["mtime_ns"]
        assert state["writes"] == baseline["writes"] and state["model_calls"] == 0
        assert not saved["changed"] and saved["revision"] == checkpoint_revision
        assert not runtime.robot.commands
        b02_evidence(
            runtime,
            baseline=baseline,
            after={
                "revision": state["revision"],
                "mtime_ns": path.stat().st_mtime_ns,
                "writes": state["writes"],
            },
        )


async def test_selected_materials_merge_without_losing_sources_and_refresh_on_change(
    profile, b02_evidence
):
    async with Runtime(profile) as runtime:
        await begin(runtime)
        for key in ("tray-a", "tray-b"):
            await call(
                runtime,
                "workspace_note",
                key=key,
                title="托盘用途",
                text="左侧托盘用于分拣",
                source=key,
            )
        fields = snapshot_input(runtime)
        fields["evidence_refs"] = [
            {"kind": "workspace_note", "id": key} for key in ("tray-a", "tray-b", "tray-a")
        ]
        await call(runtime, "task_checkpoint_save", **fields)
        state, path = current(runtime)
        assert len(state["view"]["materials"]) == 1
        assert len(state["view"]["materials"][0]["sources"]) == 2
        assert path.read_text().count("左侧托盘用于分拣") == 1
        revision = state["revision"]
        await call(
            runtime,
            "workspace_note",
            key="tray-a",
            title="托盘用途",
            text="左侧托盘用于成品",
            source="用户更新",
        )
        context = await call(runtime, "task_context", task_id="adaptive")
        state, path = current(runtime)
        assert state["revision"] > revision and "左侧托盘用于成品" in path.read_text()
        assert len(context["checkpoint"]["materials"]) == 2  # Different facts are not merged.
        await call(runtime, "workspace_note", key="tray-b", archive=True)
        await call(runtime, "task_checkpoint_get", task_id="adaptive")
        state, path = current(runtime)
        assert "左侧托盘用于分拣" not in path.read_text()
        archived = await call(
            runtime, "task_checkpoint_get", task_id="adaptive", archive_id=state["archive_head"]
        )
        assert "左侧托盘用于分拣" in json.dumps(archived, ensure_ascii=False)
        b02_evidence(runtime, context=context)


async def test_small_changes_batch_and_stability_extends_interval(
    profile, monkeypatch, b02_evidence
):
    profile.memory_maintenance = MemoryMaintenance(
        interval_s=10, max_interval_s=80, batch_changes=4, duplicate_checks=100
    )
    clock = [time.time()]
    monkeypatch.setattr("robot_rsi.task_snapshot.time.time", lambda: clock[0])
    async with Runtime(profile) as runtime:
        await begin(runtime)
        state, path = current(runtime)
        baseline = path.stat().st_mtime_ns
        clock[0] += 11
        await call(runtime, "task_checkpoint_get", task_id="adaptive")
        state, path = current(runtime)
        assert state["interval_s"] == 20 and path.stat().st_mtime_ns == baseline
        fields = snapshot_input(runtime)
        first = runtime.store.artifact(b"small evidence", "text/plain")
        fields["evidence_refs"] = [{"kind": "artifact", "id": first}]
        await call(runtime, "task_checkpoint_save", **fields)
        state, path = current(runtime)
        assert state["decision"] == "deferred" and path.stat().st_mtime_ns == baseline
        for index in range(3):
            fields["evidence_refs"].append(
                {
                    "kind": "artifact",
                    "id": runtime.store.artifact(str(index).encode(), "text/plain"),
                }
            )
            await call(runtime, "task_checkpoint_save", **fields)
        state, path = current(runtime)
        assert state["decision"] == "accumulated" and len(state["view"]["evidence_refs"]) == 4
        assert state["interval_s"] == 10
        fields["evidence_refs"].append(
            {"kind": "artifact", "id": runtime.store.artifact(b"later", "text/plain")}
        )
        await call(runtime, "task_checkpoint_save", **fields)
        clock[0] += 11
        await call(runtime, "task_checkpoint_get", task_id="adaptive")
        state, path = current(runtime)
        assert len(state["view"]["evidence_refs"]) == 5
        reasons = [
            json.loads(row[0])["reason"]
            for row in runtime.store.db.execute(
                "SELECT body FROM events WHERE kind='task_memory.updated'"
            )
        ]
        assert "interval" in reasons and "accumulated" in reasons
        assert state["model_calls"] == 0
        b02_evidence(runtime, update_reasons=reasons)


async def test_pending_duplicate_reads_merge_once_then_stop_writing(profile):
    profile.memory_maintenance = MemoryMaintenance(duplicate_checks=2)
    async with Runtime(profile) as runtime:
        await begin(runtime)
        fields = snapshot_input(runtime)
        fields["evidence_refs"] = [
            {"kind": "artifact", "id": runtime.store.artifact(b"one new reference", "text/plain")}
        ]
        await call(runtime, "task_checkpoint_save", **fields)
        await call(runtime, "task_checkpoint_get", task_id="adaptive")
        state, path = current(runtime)
        assert state["view"]["evidence_refs"] == fields["evidence_refs"]
        previous = (state["revision"], path.stat().st_mtime_ns)
        await call(runtime, "task_checkpoint_get", task_id="adaptive")
        state, path = current(runtime)
        assert (state["revision"], path.stat().st_mtime_ns) == previous


async def test_36_progress_events_archive_without_growing_active_history_and_restore(
    profile, b02_evidence
):
    profile.task_limits = TaskLimits(tool_calls=200)
    async with Runtime(profile) as runtime:
        await begin(runtime)
        runtime.store.put(
            "operation",
            "unknown-op",
            {
                "id": "unknown-op",
                "task_id": "adaptive",
                "status": "unknown",
                "stage": "verify_placement",
                "error": {"code": "verification_unknown"},
                "skill_name": "pick_place",
                "skill_version": "0.2.0",
            },
        )
        fields = snapshot_input(runtime)
        fields.update(operation_ids=["unknown-op"], unknowns=["放置效果未知", "放置效果未知"])
        trend = []
        for index in range(36):
            fields["last_confirmed"] = f"阶段 {index // 3:02} 已记录，放置仍未确认"
            await call(runtime, "task_checkpoint_save", **fields)
            state, path = current(runtime)
            trend.append(
                {
                    "event": index + 1,
                    "revision": state["revision"],
                    "bytes": path.stat().st_size,
                    "writes": state["writes"],
                }
            )
        assert len(runtime.store.all("task_memory_archive")) >= 12
        assert max(row["bytes"] for row in trend) < min(row["bytes"] for row in trend) * 1.15
        assert len({row["revision"] for row in trend}) == 12
        assert state["view"]["unknowns"] == ["放置效果未知"]
        assert state["view"]["unresolved_operation_ids"] == ["unknown-op"]
        assert state["model_calls"] == 0 and not runtime.robot.commands
        archive_head = state["archive_head"]
        archive = await call(
            runtime, "task_checkpoint_get", task_id="adaptive", archive_id=archive_head
        )
        assert archive["view"]["last_confirmed"] != fields["last_confirmed"]
        before = runtime.task_memory._load("adaptive")
        used = before["execution"]["tool_calls"]
        epoch = before["execution"]["epoch"]
        deadline = before["execution"]["deadline_at"]
    async with Runtime(profile) as runtime:
        restored = await call(runtime, "task_checkpoint_get", task_id="adaptive")
        assert restored["goal"] == "抓取并放置" and restored["constraints"] == ["保留右侧区域"]
        assert restored["execution"]["tool_calls"] == used + 1
        assert (
            restored["execution"]["epoch"] == epoch
            and restored["execution"]["deadline_at"] == deadline
        )
        view = restored["current_memory"]["view"]
        assert view["unresolved_operation_ids"] == ["unknown-op"] and "未知" in view["next_step"]
        assert restored["operations"][0]["status"] == "unknown" and runtime.operations.blocked
        assert not runtime.robot.commands
        await begin(runtime, task_id="task-b")
        other = await call(runtime, "task_context", task_id="task-b")
        assert other["checkpoint"]["operation_ids"] == []
        wrong = await runtime.invoke(
            "task_checkpoint_get", {"task_id": "task-b", "archive_id": archive_head}
        )
        assert wrong["error"]["code"] == "not_found"
        b02_evidence(runtime, trend=trend, restored=restored, wrong_task_archive=wrong)
    other_profile = profile.model_copy(update={"namespace": "separate"})
    async with Runtime(other_profile) as runtime:
        result = await runtime.invoke("task_checkpoint_get", {"task_id": "adaptive"})
        assert result["error"]["code"] == "not_found"
        assert runtime.store.all("task_memory_archive") == []


async def test_phase_changes_update_memory_and_missing_file_rebuilds_without_replay(
    profile, b02_evidence
):
    async with Runtime(profile) as runtime:
        await begin(runtime)
        accepted = await call(
            runtime, "task_run", task_id="adaptive", task_epoch=0, request_id="phases"
        )
        done = await call(runtime, "operation_get", operation_id=accepted["id"], wait_s=2)
        assert done["status"] == "succeeded"
        state, path = current(runtime)
        assert state["view"]["last_confirmed"].endswith("succeeded / verified")
        events = [
            json.loads(row[0])
            for row in runtime.store.db.execute(
                "SELECT body FROM events WHERE kind='task_memory.updated'"
            )
        ]
        assert len(events) > 5 and all(row["model_calls"] == 0 for row in events)
        revision, writes, body = state["revision"], state["writes"], path.read_text()
        path.unlink()
        commands = list(runtime.robot.commands)
        restored = await call(runtime, "task_checkpoint_get", task_id="adaptive")
        state, path = current(runtime)
        assert state["revision"] == revision and state["writes"] == writes + 1
        assert path.read_text() == body and runtime.robot.commands == commands
        assert restored["current_memory"]["view"]["operations"][0]["status"] == "succeeded"
        b02_evidence(runtime, maintenance_events=events, repaired=restored)


async def test_restart_reconciles_derived_file_from_owner_without_replaying(profile):
    async with Runtime(profile) as runtime:
        await begin(runtime)
        state, path = current(runtime)
        revision = state["revision"]
        expected = path.read_text()
    path.write_text("interrupted derived view with a conflicting goal")
    async with Runtime(profile) as runtime:
        restored = await call(runtime, "task_checkpoint_get", task_id="adaptive")
        assert restored["goal"] == "抓取并放置"
        assert restored["current_memory"]["revision"] == revision
        assert path.read_text() == expected and runtime.robot.commands == []


async def test_effective_unknowns_may_grow_past_a_single_read_budget(profile):
    profile.task_limits = TaskLimits(context_bytes=1000)
    async with Runtime(profile) as runtime:
        await begin(runtime)
        fields = snapshot_input(runtime)
        fields["unknowns"] = [
            f"历史反馈项 {index} 待补充来源。" + "需保留的条件。" * 20 for index in range(12)
        ]
        await call(runtime, "task_checkpoint_save", **fields)
        state, path = current(runtime)
        assert path.stat().st_size > profile.task_limits.context_bytes
        assert state["view"]["unknowns"] == fields["unknowns"]
        response = await runtime.invoke("task_context", {"task_id": "adaptive"})
        assert response["ok"] and response["result"]["details_required"]
        references = response["result"]["read_first"]
        checkpoint = next(ref for ref in references if ref["tool"] == "task_checkpoint_get")
        detail = await runtime.invoke(checkpoint["tool"], checkpoint["arguments"])
        assert detail["ok"] and detail["result"]["unknowns"] == fields["unknowns"]
        # A paged-read requirement never becomes a replacement execution quota.
        accepted = await call(
            runtime, "task_run", task_id="adaptive", task_epoch=0, request_id="no-character-quota"
        )
        done = await call(runtime, "operation_get", operation_id=accepted["id"], wait_s=2)
        assert done["status"] == "succeeded"


async def test_automatic_operation_evidence_preserves_all_selected_materials(profile):
    async with Runtime(profile) as runtime:
        await begin(runtime)
        fields = snapshot_input(runtime)
        fields["evidence_refs"] = []
        for index in range(16):
            key = f"selected-{index}"
            await call(
                runtime,
                "workspace_note",
                key=key,
                title=f"资料 {index}",
                text=f"需保留的工件说明 {index}",
                source="用户资料",
            )
            fields["evidence_refs"].append({"kind": "workspace_note", "id": key})
        await call(runtime, "task_checkpoint_save", **fields)
        accepted = await call(
            runtime, "task_run", task_id="adaptive", task_epoch=0, request_id="preserve-materials"
        )
        done = await call(runtime, "operation_get", operation_id=accepted["id"], wait_s=2)
        assert done["status"] == "succeeded"
        restored = await call(runtime, "task_checkpoint_get", task_id="adaptive")
        assert restored["evidence_refs"] == fields["evidence_refs"]
        view = restored["current_memory"]["view"]
        assert len(view["materials"]) == 16
        assert {"kind": "observation", "id": done["result"]["observation_id"]} in view[
            "evidence_refs"
        ]
