import json

import httpx
import pytest

from robot_rsi.catalog import SKILLS
from robot_rsi.contracts import WorkspaceConfig
from robot_rsi.runtime import Runtime


async def call(runtime, tool_name, **arguments):
    response = await runtime.invoke(tool_name, arguments)
    assert response["ok"], response
    return response["result"]


async def test_same_foundation_skill_reads_two_workspaces_without_device_access(profile):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(503)

    for station, title in (("bench-a", "桌面整理"), ("bench-b", "装配工作台")):
        current = profile.model_copy(deep=True)
        current.station = station
        current.robot = "yam_legacy"
        current.workspace = WorkspaceConfig(title=title, constraints=["保留操作台右侧区域"])
        async with Runtime(current, robot_transport=httpx.MockTransport(handler)) as runtime:
            brief = await call(runtime, "workspace_read")
            assert brief["workspace"]["id"] == station
            assert brief["workspace"]["title"] == title
            assert brief["workspace"]["constraints"] == ["保留操作台右侧区域"]
            assert brief["workspace"]["source"] == "host_profile"
            assert not runtime.store.all("observation")
            cards = await call(runtime, "skills_list", query="工作区资料")
            assert [s["name"] for s in cards["skills"]] == ["workspace_context"]
            skill = await call(runtime, "skill_describe", name="workspace_context")
            assert skill["kind"] == "agent_workflow" and skill["available"]
            assert skill["entrypoint"]["tool"] == "workspace_read"
            # The brief contains semantic roles, not connection addresses or deployment paths.
            public = json.dumps(brief)
            assert current.yam_url not in public and current.state_root not in public
    assert requests == []


async def test_notes_update_reuse_keys_persist_and_archive(profile):
    note = {
        "key": "table-layout",
        "title": "托盘区域说明",
        "text": "绿色托盘用于已分拣物体。",
        "source": "用户提供的操作台说明",
    }
    async with Runtime(profile) as runtime:
        first = await call(runtime, "workspace_note", **note)
        assert first["changed"] and first["revision"] == 1
        again = await call(runtime, "workspace_note", **note)
        assert not again["changed"] and again["revision"] == 1
        updated = await call(
            runtime, "workspace_note", **{**note, "text": "蓝色托盘用于已分拣物体。"}
        )
        assert updated["revision"] == 2
        assert len(runtime.store.all("workspace_note")) == 1
        assert runtime.profile.workspace == WorkspaceConfig()
        assert runtime.robot.commands == []

    async with Runtime(profile) as runtime:
        cards = await call(runtime, "workspace_read", query="托盘")
        assert cards["total"] == 1
        assert "text" not in cards["notes"][0]
        record = await call(runtime, "workspace_read", key="table-layout")
        assert record["text"].startswith("蓝色") and record["revision"] == 2
        await call(runtime, "workspace_note", key="table-layout", archive=True)
        assert (await call(runtime, "workspace_read", query=""))["notes"] == []
        missing = await runtime.invoke("workspace_read", {"key": "table-layout"})
        assert missing["error"]["code"] == "not_found"


async def test_notes_are_scoped_and_do_not_enter_runtime_lessons(profile):
    async with Runtime(profile) as runtime:
        await call(
            runtime,
            "workspace_note",
            key="fixture",
            title="资料",
            text="工作区 A 的任务资料",
            source="用户说明",
        )
        assert (await call(runtime, "memory_recall"))["records"] == []
    other = profile.model_copy(update={"namespace": "other-task-environment"})
    async with Runtime(other) as runtime:
        assert (await call(runtime, "workspace_read", query=""))["total"] == 0
        inaccessible = await runtime.invoke("workspace_read", {"key": "fixture"})
        assert inaccessible["error"]["code"] == "not_found"


async def test_large_note_library_returns_bounded_cards_and_specific_detail(profile):
    async with Runtime(profile) as runtime:
        for index in range(25):
            await call(
                runtime,
                "workspace_note",
                key=f"note-{index}",
                title=f"摆放说明 {index}",
                text="相关资料 " * 200,
                source="测试夹具",
            )
        brief = await call(runtime, "workspace_read")
        assert brief["notes"]["count"] == 25
        assert "相关资料" not in json.dumps(brief, ensure_ascii=False)
        first = await call(runtime, "workspace_read", query="摆放", limit=2)
        second = await call(
            runtime, "workspace_read", query="摆放", limit=2, offset=first["next_offset"]
        )
        assert first["total"] == 25 and len(first["notes"]) == 2
        assert {n["key"] for n in first["notes"]}.isdisjoint(n["key"] for n in second["notes"])
        assert all("text" not in note for note in first["notes"])
        detail = await call(runtime, "workspace_read", key=first["notes"][0]["key"])
        assert detail["text"].startswith("相关资料")


async def test_skill_discovery_selects_then_runs_code_skill(profile, monkeypatch):
    # Extra directory entries exercise retrieval only, not implemented robot skills.
    fixtures = [
        {**SKILLS[0], "name": f"fixture_{i}", "description": "无关目录夹具", "keywords": []}
        for i in range(60)
    ]
    async with Runtime(profile) as runtime:
        runtime.registry.seed_fixtures(fixtures)
        all_cards = await call(runtime, "skills_list", limit=3)
        assert all_cards["total"] == len(SKILLS) + len(fixtures) and len(all_cards["skills"]) == 3
        cards = await call(runtime, "skills_list", query="抓取放置")
        assert [s["name"] for s in cards["skills"]] == ["pick_place"]
        assert "instructions" not in cards["skills"][0]
        detail = await call(runtime, "skill_describe", name=cards["skills"][0]["name"])
        assert detail["instructions"] and "instructions_path" not in detail
        assert (await call(runtime, "skills_list", query="welding"))["skills"] == []
        operation = await call(
            runtime,
            detail["entrypoint"]["tool"],
            request_id="selected-skill",
            **detail["entrypoint"]["arguments"],
        )
        done = await call(runtime, "operation_get", operation_id=operation["id"], wait_s=2)
        assert done["status"] == "succeeded"


@pytest.mark.parametrize("arguments", [{"key": "k", "query": "q"}, {"namespace": "other"}])
async def test_workspace_lookup_keeps_single_host_scope(profile, arguments):
    async with Runtime(profile) as runtime:
        result = await runtime.invoke("workspace_read", arguments)
        assert result["error"]["code"] == "invalid_arguments"
