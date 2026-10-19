import json
import subprocess
import sys

from robot_rsi.agent_host import AgentHost
from robot_rsi.contracts import TaskBegin
from robot_rsi.mcp_server import create_mcp
from robot_rsi.project import source_profile
from robot_rsi.prompts import base_prompt
from robot_rsi.runtime import Runtime


async def test_root_only_source_scope_has_no_fabricated_capabilities_and_files_are_bounded(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "AGENTS.md").write_text("User-owned project guidance; preserve this file.")
    (root / "README.md").write_text("source text " * 1000)
    (root / "escape").symlink_to(tmp_path)
    (root / ".codex").mkdir()
    (root / ".codex/hidden").write_text("PRIVATE_STATE")
    profile = source_profile(root, state_root=str(tmp_path / "state"))
    async with Runtime(profile) as runtime:
        assert runtime.robot is None and runtime.bindings.catalog() == []
        assert runtime.adapter.describe() == []
        listed = (await runtime.invoke("project_files", {}))["result"]
        assert listed["entries"] == [{"path": "AGENTS.md", "kind": "file"}, {"path": "README.md", "kind": "file"}]
        one = (await runtime.invoke("project_files", {"path": "README.md", "limit": 7}))["result"]
        two = (await runtime.invoke("project_files", {"path": "README.md", "offset": one["next_offset"], "limit": 7}))["result"]
        assert one["text"] + two["text"] == (root / "README.md").read_text()[:14]
        for path in ("../other", str(tmp_path), "escape/x", ".codex/hidden"):
            assert (await runtime.invoke("project_files", {"path": path}))["error"]["code"] == "invalid_project_path"
        assert (await runtime.invoke("project_files", {"path": "missing"}))["error"]["code"] == "not_found"
    for role, purpose in (("runtime", "run"), ("evaluation", "evaluate")):
        other = profile.model_copy(update={"role": role, "project": profile.project.model_copy(update={"purpose": purpose})})
        async with Runtime(other) as runtime:
            assert (await runtime.invoke("project_files", {}))["error"]["code"] == "source_access_denied"
    assert (root / "AGENTS.md").read_text() == "User-owned project guidance; preserve this file."


def test_base_prompt_and_mcp_initialization_share_installed_source(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    profile = source_profile(root, state_root=str(tmp_path / "state"))
    prompt = base_prompt()
    server = create_mcp(profile)
    instructions = server._mcp_server.create_initialization_options().instructions
    assert instructions.startswith(prompt["text"])
    assert prompt["identity"]["bytes"] == len(prompt["text"].encode())
    assert all(ord(c) < 128 for c in prompt["text"])
    assert "MCP protocol" in instructions


async def test_information_goal_discovers_workflow_finishes_with_note_and_returns_conclusion(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "README.md").write_text("A small recorded-source project.")
    profile = source_profile(root, state_root=str(tmp_path / "state"))

    class ProtocolClient:
        def binding(self, role, config):
            return {"client": "protocol_double", "role": role, "model": "none"}

        async def run(self, turn, emit):
            async def call(name, arguments):
                result = await turn["invoke"](name, arguments)
                assert result["ok"], result
                return result["result"]

            assert turn["base_prompt"]["text"] == base_prompt()["text"]
            assert "这是我的机器人项目，你看看" in turn["prompt"]
            cards = await call("skills_list", {"query": "机器人项目"})
            assert any(c["name"] == "robot_onboarding" for c in cards["skills"])
            skill = await call("skill_describe", {"name": "robot_onboarding"})
            assert "Inspect" in skill["instructions"]
            source = await call("project_files", {"path": "README.md"})
            await call("workspace_note", {"key": "project_summary", "title": "Source facts",
                                           "text": source["text"], "source": "README.md:1"})
            task = await call("task_checkpoint_get", {})
            await call("task_finish", {"task_epoch": task["execution"]["epoch"], "outcome": "succeeded",
                                       "summary": "已检查来源并保存项目事实。",
                                       "evidence_refs": [{"kind": "workspace_note", "id": "project_summary"}]})
            await call("task_archive", {})
            return {"completed": True, "returncode": 0, "message": "项目是录制数据工具，尚未接入设备。来源：README.md:1。"}

        async def close(self):
            pass

    async with Runtime(profile) as runtime:
        runtime.task_memory.begin(TaskBegin(task_id="inspect", goal="这是我的机器人项目，你看看"), profile.task_limits)
        async with AgentHost(runtime).serve() as host:
            result = await host.run("inspect", client=ProtocolClient())
        assert result["status"] == "complete" and "README.md:1" in result["message"]
        assert runtime.store.all("operation") == []
        assert runtime.agent_state.control("inspect")["active_session"] is None


def test_project_entry_missing_directory_has_exact_unmet_input(tmp_path):
    result = subprocess.run([sys.executable, "-m", "robot_rsi.cli", "agent", "run", "--project", str(tmp_path / "absent"),
                             "这是我的机器人项目，你看看"], capture_output=True, text=True)
    assert result.returncode == 1 and json.loads(result.stdout)["error"]["code"] == "project_not_found"
