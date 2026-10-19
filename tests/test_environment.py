import json
from importlib.resources import files

from robot_rsi.agent_host import AgentHost
from robot_rsi.contracts import TaskContext
from robot_rsi.environment import configured_environment
from robot_rsi.mcp_server import create_mcp
from robot_rsi.project import source_profile
from robot_rsi.runtime import Runtime
from robot_rsi.task_context import build_task_context


async def test_environment_arrives_on_start_and_restore_without_selecting_a_skill(profile):
    views = []

    class Client:
        def binding(self, role, config):
            return {"client": "protocol_fixture", "role": role, "model": "none"}

        async def run(self, turn, emit):
            views.append(turn["view"])
            return {"completed": True, "returncode": 0}

        async def close(self):
            pass

    async with Runtime(profile) as runtime:
        task = (await runtime.invoke("task_begin", {"task_id": "resume", "goal": "Inspect this environment"}))["result"]
        assert task["environment"]["id"] == f"{profile.station}/{profile.namespace}"
        async with AgentHost(runtime).serve() as host:
            await host.run("resume", client=Client())
        assert not runtime.robot.commands and not runtime.store.all("operation")
    async with Runtime(profile) as runtime:
        async with AgentHost(runtime).serve() as host:
            await host.run("resume", client=Client())
        assert not runtime.robot.commands and not runtime.store.all("operation")
        environment = runtime.workspace_brief("resume")["environment"]
        assert all(view["blocks"]["workspace"]["environment"] == environment for view in views)
        assert environment["state"].startswith("configuration_only")
        assert environment["installation_digest"] == task["execution"]["installation_digest"]
        assert not runtime.task_memory._load("resume")["skill_versions"]
        args = TaskContext(task_id="resume", blocks=True)
        initial = build_task_context(runtime, args)
        repeated = build_task_context(runtime, args.model_copy(update={"delivered": initial["delivered"]}))
        assert repeated["blocks"] == {}
        await runtime.invoke("workspace_note", {"key": "one", "title": "one", "text": "changed", "source": "user"})
        changed = build_task_context(runtime, args.model_copy(update={"delivered": repeated["delivered"]}))
        assert list(changed["blocks"]) == ["workspace"]
        assert changed["blocks"]["workspace"]["environment"] == environment


async def test_environment_scope_and_mcp_configuration_do_not_create_an_owner(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    profile = source_profile(root, state_root=str(tmp_path / "state"))
    import robot_rsi.mcp_server as module

    def unexpected(*args, **kwargs):
        raise AssertionError("MCP initialization must not instantiate an extra Runtime")

    monkeypatch.setattr(module, "Runtime", unexpected)
    instructions = create_mcp(profile)._mcp_server.create_initialization_options().instructions
    configuration = json.loads(instructions.split("\nHost environment: ")[1])
    assert configuration == configured_environment(profile)
    assert configuration["qualification"] == "source_only"
    first = profile.model_copy(update={"namespace": "a"})
    second = profile.model_copy(update={"namespace": "b"})
    async with Runtime(first) as runtime:
        await runtime.invoke("task_begin", {"task_id": "one", "goal": "inspect"})
        await runtime.invoke("workspace_note", {"key": "private", "title": "a", "text": "A_ONLY", "source": "user"})
        assert runtime.workspace_brief()["environment"]["tool_scope"]["source_read"]
    async with Runtime(second) as runtime:
        assert not runtime.store.all("task_checkpoint") and not runtime.store.all("workspace_note")
        assert "A_ONLY" not in json.dumps(runtime.workspace_brief())
    evaluation = first.model_copy(update={"role": "evaluation", "project": first.project.model_copy(update={"purpose": "evaluate"})})
    async with Runtime(evaluation) as runtime:
        assert not runtime.store.all("task_checkpoint") and not runtime.store.all("workspace_note")
        assert not runtime.workspace_brief()["environment"]["tool_scope"]["source_read"]


async def test_workflow_upgrade_retains_fixed_old_artifact_and_installation(profile, tmp_path):
    package = tmp_path / "old_workflow"
    package.mkdir()
    manifest = json.loads(files("robot_rsi").joinpath("resources/robot-onboarding/skill.json").read_text())
    current_version = manifest["version"]
    manifest["version"] = "1.0.0"
    (package / "skill.json").write_text(json.dumps(manifest))
    (package / "SKILL.md").write_text("Old immutable workflow")
    async with Runtime(profile) as runtime:
        runtime.registry.register(package)
        runtime.registry.enable("robot_onboarding", "1.0.0")
        await runtime.invoke("task_begin", {"task_id": "old", "goal": "robot project onboarding"})
        await runtime.invoke("skill_describe", {"name": "robot_onboarding"}, task_id="old")
        record = runtime.task_memory._load("old")
        record["execution"]["installation_digest"] = "old_installation"
        runtime.task_memory._write(record)
        runtime.registry.enable("robot_onboarding", current_version)
        fixed = runtime.registry.resolve("robot_onboarding", "old")
        assert fixed["version"] == "1.0.0" and fixed["instructions"] == "Old immutable workflow"
        summary = runtime.workspace_brief("old")["environment"]
        assert summary["installation_digest"] == "old_installation"
        assert next(c for c in summary["skills"]["skills"] if c["name"] == "robot_onboarding")["version"] == "1.0.0"
