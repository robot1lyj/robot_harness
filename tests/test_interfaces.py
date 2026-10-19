import io
import json
import os
import sys

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image

from robot_rsi.robots import LegacyYam
from robot_rsi.runtime import Runtime
from robot_rsi.server import create_app
from robot_rsi.storage import Store


async def test_legacy_owner_reads_only_and_no_stereo_fabricated(profile):
    requests = []
    image = io.BytesIO()
    Image.new("RGB", (32, 16)).save(image, format="JPEG")

    def handler(request):
        requests.append((request.method, request.url.path))
        if request.url.path == "/status":
            return httpx.Response(200, json={"connection": "connected", "epoch": 4, "tick": 5})
        return httpx.Response(200, content=image.getvalue())

    profile.robot = "yam_legacy"
    async with Runtime(profile, robot_transport=httpx.MockTransport(handler)) as runtime:
        assert not requests  # discovery and construction do not connect hardware
        assert not next(t for t in runtime.tools()["tools"] if t["name"] == "robot_move")[
            "available"
        ]
        obs = await runtime.observe("left")
        assert obs["stereo"] is None and obs["captured_at"] is None
        assert obs["state"]["tick"] == 5
        motion = await runtime.invoke(
            "skill_pick_place", {"request_id": "no-physical-motion", "observation_id": obs["id"]}
        )
        assert motion["error"]["code"] == "unsupported"
        hold = await runtime.invoke("robot_hold")
        assert hold["error"]["code"] == "not_enabled"
    assert requests == [("GET", "/status"), ("GET", "/camera/left.jpg")]


async def test_legacy_hold_ack_is_never_stop_confirmation(profile):
    profile.robot, profile.yam_allow_hold = "yam_legacy", True

    def handler(request):
        assert request.method == "POST" and request.url.path == "/event/hold"
        assert request.headers["X-YAM-Control"] == "1"
        return httpx.Response(200, json={"queued": "hold"})

    store = Store(profile)
    robot = LegacyYam(profile, store, transport=httpx.MockTransport(handler))
    try:
        assert await robot.hold() == {
            "confirmed": False,
            "queued": True,
            "note": "Legacy acknowledgement does not prove physical stop",
        }
    finally:
        await robot.close()
        store.close()


async def test_http_auth_validation_and_artifact_fetch(profile):
    token = "test-only-token-with-at-least-24-characters"
    app = create_app(profile, token)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/tools")).status_code == 401
        assert (await client.post("/tools/robot_hold", json={"arguments": {}})).status_code == 401
        headers = {"Authorization": "Bearer " + token}
        assert (await client.get("/tools", headers=headers)).status_code == 200
        bad = await client.post(
            "/tools/memory_recall",
            headers=headers,
            json={"arguments": {"namespace": "development"}},
        )
        assert bad.status_code == 422
        response = await client.post(
            "/tools/robot_observe", headers=headers, json={"arguments": {}}
        )
        obs = response.json()["result"]
        url = "/artifacts/" + obs["image_artifact"]
        assert (await client.get(url)).status_code == 401
        artifact = await client.get(url, headers=headers)
        assert artifact.headers["content-type"] == "image/png"
        assert Image.open(io.BytesIO(artifact.content)).size == (640, 480)
        assert (await client.get("/artifacts/nonexistent", headers=headers)).status_code == 404
    with pytest.raises(ValueError, match="TOKEN"):
        create_app(profile, "")


async def test_real_mcp_stdio_discovery_and_skill_execution(profile, tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(profile.model_dump_json())
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "robot_rsi.cli", "--profile", str(path), "mcp"],
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}
        assert {
            "skill_pick_place",
            "depth_estimate",
            "memory_recall",
            "artifact_image",
            "task_checkpoint_save",
            "task_checkpoint_get",
            "task_archive",
        } <= names
        assert "memory_review" not in names

        async def call(name, arguments):
            result = await session.call_tool(name, {"arguments": arguments})
            assert not result.isError, result
            return json.loads(result.content[0].text)["result"]

        brief = await call("workspace_read", {})
        assert brief["workspace"]["id"] == profile.station
        cards = await call("skills_list", {"query": "工作区"})
        assert cards["skills"][0]["name"] == "workspace_context"
        foundation = await call("skill_describe", {"name": "workspace_context"})
        assert foundation["kind"] == "agent_workflow" and foundation["instructions"]
        await call(
            "workspace_note",
            {"key": "tray", "title": "托盘用途", "text": "托盘用于分拣。", "source": "测试输入"},
        )
        note = await call("workspace_read", {"key": "tray"})
        assert note["text"] == "托盘用于分拣。"

        observation = await call("robot_observe", {})
        operation = await call("task_run", {"request_id": "mcp-pick"})
        done = await call("operation_get", {"operation_id": operation["id"], "wait_s": 5})
        assert done["status"] == "succeeded"
        cards = await call("skills_list", {"query": "任务检查点"})
        assert cards["skills"][0]["name"] == "task_memory"
        task_memory = await call("skill_describe", {"name": "task_memory"})
        assert task_memory["kind"] == "agent_workflow" and task_memory["instructions"]
        saved = await call(
            "task_checkpoint_save",
            {
                "task_id": "mcp-task",
                "goal": "模拟抓放",
                "constraints": [],
                "skill_versions": {"pick_place": "0.2.0"},
                "last_confirmed": "已验证放置",
                "operation_ids": [done["id"]],
                "unknowns": [],
                "remaining_budget": {"tool_calls": 3, "time_s": None, "model_tokens": None},
                "evidence_refs": [{"kind": "observation", "id": done["result"]["observation_id"]}],
            },
        )
        restored = await call("task_checkpoint_get", {"task_id": "mcp-task"})
        assert (
            restored["goal"] == saved["goal"] and restored["operations"][0]["status"] == "succeeded"
        )
        archived = await call("task_archive", {"task_id": "mcp-task"})
        assert archived["archived"]
        reference = archived["reference"]
        assert (await call(reference["tool"], reference["arguments"]))["archived"]
        image = await session.call_tool(
            "artifact_image", {"arguments": {"artifact_id": observation["image_artifact"]}}
        )
        assert image.content[0].type == "image"
        invalid = await session.call_tool("memory_recall", {"arguments": {"role": "development"}})
        assert invalid.isError


async def test_mcp_task_budget_includes_schema_rejections_and_host_binding(profile, tmp_path):
    profile.active_task_id = "mcp-budget"
    path = tmp_path / "managed-profile.json"
    path.write_text(profile.model_dump_json())
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "robot_rsi.cli", "--profile", str(path), "mcp"],
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        started = await session.call_tool(
            "task_begin", {"arguments": {"task_id": "mcp-budget", "goal": "抓取"}}
        )
        assert not started.isError
        invalid = await session.call_tool(
            "task_run", {"arguments": {"request_id": "unsupported", "skill": "weld"}}
        )
        assert invalid.isError
        context = await session.call_tool("task_context", {"arguments": {"task_id": "mcp-budget"}})
        body = json.loads(context.content[0].text)["result"]
        assert body["checkpoint"]["execution"]["tool_calls"] == 3
        assert body["checkpoint"]["operation_ids"] == []
        wrong_task = await session.call_tool(
            "task_run",
            {"task_id": "other", "arguments": {"request_id": "cross-task", "task_epoch": 0}},
        )
        assert wrong_task.isError
