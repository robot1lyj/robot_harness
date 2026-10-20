import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from robot_rsi import cli, guides
from robot_rsi.agent_client import AGENT_TOOLS
from robot_rsi.agent_host import AgentHost
from robot_rsi.contracts import ProjectConfig, TaskBegin
from robot_rsi.guides import HarnessRead
from robot_rsi.project import source_profile
from robot_rsi.resources import FKRequest
from robot_rsi.runtime import Runtime


@pytest.fixture
def installed_guides(tmp_path, monkeypatch):
    site = tmp_path / "site"
    for name in ("integration", "interfaces"):
        file = site / f"robot_rsi/resources/guides/{name}.md"
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(f"# {name}\n" + "bounded 文本\n" * 20)
    monkeypatch.setattr(guides, "distribution", lambda name: SimpleNamespace(
        version="test-installed", locate_file=lambda resource: site / resource,
        read_text=lambda name: '{"url":"file:///fixture/core.whl"}'))
    return site


def test_usage_target_is_optional_metadata_not_creation_or_scope(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "new" / "use"
    old = source_profile(source, state_root=str(tmp_path / "state"))
    new = source_profile(source, state_root=str(tmp_path / "state"), usage_root=target)
    assert old.project.usage_root is None and new.project.usage_root == str(target)
    assert old.namespace == new.namespace and not target.exists()
    file = tmp_path / "not_a_directory"
    file.write_text("leave unchanged")
    with pytest.raises(ValidationError):
        ProjectConfig(root=str(source), usage_root=str(file))
    with pytest.raises(ValidationError):
        ProjectConfig(root=str(source), usage_root="")


async def test_fixed_guides_pagination_identity_and_no_checkout_fallback(tmp_path, installed_guides):
    root = tmp_path / "source"
    root.mkdir()
    profile = source_profile(root, state_root=str(tmp_path / "state"))
    async with Runtime(profile) as runtime:
        first = await runtime.invoke("harness_read", {"guide": "interfaces", "limit": 7})
        assert first["ok"]
        one = first["result"]
        two = (await runtime.invoke("harness_read", {"guide": "interfaces", "offset": one["next_offset"], "limit": 7}))["result"]
        original = (installed_guides / "robot_rsi/resources/guides/interfaces.md").read_text()
        assert one["text"] + two["text"] == original[:14]
        assert one["sha256"] == two["sha256"] and one["canonical_source"] == "docs/interfaces.md"
        assert one["installation_digest"] == runtime.task_memory.installation_digest
        assert one["distribution_version"] == "test-installed"
        assert one["distribution_origin"]["url"] == "file:///fixture/core.whl"
        for args in ({"guide": "../AGENTS"}, {"guide": str(root)}, {"offset": -1}, {"limit": 16001}):
            assert (await runtime.invoke("harness_read", args))["error"]["code"] == "invalid_arguments"
        (installed_guides / "robot_rsi/resources/guides/integration.md").unlink()
        assert (await runtime.invoke("harness_read", {}))["error"]["code"] == "guide_not_installed"
        assert runtime.robot is None and not runtime.store.all("operation")
    assert HarnessRead().guide == "integration"


async def test_host_plan_goal_target_and_read_only_contract_entries_reach_client(tmp_path, installed_guides):
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("Ordinary source description")
    target = tmp_path / "usage_target"
    profile = source_profile(source, state_root=str(tmp_path / "state"), usage_root=target)
    goal = "这是我的机器人项目，给我一个能开始实施的接入方案。"

    class Client:
        def binding(self, role, config):
            return {"client": "software-double", "role": role, "model": "none"}

        async def run(self, turn, emit):
            assert turn["view"]["blocks"]["task"]["goal"] == goal
            environment = turn["view"]["blocks"]["workspace"]["environment"]
            assert environment["source_root"] == str(source)
            assert environment["usage_root"] == str(target)
            assert environment["usage_environment"] == "selected_not_inspected"
            assert environment["current_installation"]["executable"]
            names = {tool["name"] for tool in turn["tools"]}
            assert {"harness_read", "tool_describe", "tools_list", "capabilities_list", "resources_list"} <= names
            assert "resource_connect" not in AGENT_TOOLS

            async def call(name, args):
                response = await turn["invoke"](name, args)
                assert response["ok"], response
                return response["result"]

            schema = await call("tool_describe", {"name": "kinematics_fk"})
            assert schema["input_schema"] == FKRequest.model_json_schema()
            assert (await call("capabilities_list", {}))["capabilities"] == []
            assert (await call("resources_list", {}))["resources"] == []
            await call("harness_read", {"guide": "interfaces", "limit": 17})
            note = await call("workspace_note", {"key": "plan", "title": "Protocol fixture", "text": "Sourced plan fixture", "source": "README.md / installed guides"})
            assert note["revision"] == 1
            task = await call("task_checkpoint_get", {})
            await call("task_finish", {"task_epoch": task["execution"]["epoch"], "outcome": "succeeded", "summary": "协议替身已交付资料型目标", "evidence_refs": [{"kind": "workspace_note", "id": "plan"}]})
            await call("task_archive", {})
            return {"completed": True, "returncode": 0, "message": "协议替身，不是原生模型方案。"}

        async def close(self):
            pass

    async with Runtime(profile) as runtime:
        runtime.task_memory.begin(TaskBegin(task_id="plan", goal=goal), profile.task_limits)
        async with AgentHost(runtime).serve() as host:
            result = await host.run("plan", client=Client())
        assert result["status"] == "complete" and not target.exists()
        assert not runtime.store.all("operation")
        results = [json.loads(row[0]) for row in runtime.store.db.execute("SELECT body FROM events WHERE kind='agent.tool_result'")]
        assert any(row["tool"] == "harness_read" and row["response"]["result"]["text"] for row in results)


def test_cli_source_and_profile_accept_usage_root_without_model(tmp_path, monkeypatch, capsys):
    import robot_rsi.agent_host as host
    import robot_rsi.mcp_server as mcp

    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "destination"
    seen = []

    async def fake_run(profile, task_id, goal):
        seen.append((profile, goal))
        return {"status": "complete", "task_id": task_id}

    monkeypatch.setattr(host, "run_agent", fake_run)
    goal = "这是我的机器人项目，给我一个能开始实施的接入方案。"
    monkeypatch.setattr("sys.argv", ["robot-rsi", "agent", "run", "--project", str(source), "--usage-root", str(target), goal])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 0 and seen[-1][1] == goal
    assert seen[-1][0].project.usage_root == str(target) and not target.exists()
    capsys.readouterr()
    profile_file = tmp_path / "profile.json"
    profile_file.write_text(source_profile(source).model_dump_json())
    monkeypatch.setattr(mcp, "create_mcp", lambda profile: SimpleNamespace(run=lambda **kwargs: seen.append((profile, "mcp"))))
    monkeypatch.setattr("sys.argv", ["robot-rsi", "--profile", str(profile_file), "mcp", "--usage-root", str(target)])
    cli.main()
    assert seen[-1][0].project.usage_root == str(target) and not target.exists()
