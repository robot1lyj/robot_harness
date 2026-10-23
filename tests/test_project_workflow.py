import importlib.util
import json
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import pytest

from robot_rsi.contracts import HarnessError, Profile, ProjectConfig
from robot_rsi.feedback import FrameworkReply, IssueCreate, IssueGet
from robot_rsi.project import Project, ProjectRead
from robot_rsi.runtime import TOOLS, Runtime

ROOT = Path(__file__).parents[1]
CORE_VERSION = version("robot-rsi-harness")


def test_project_cli_without_host_project_has_structured_error(tmp_path):
    file = tmp_path / "profile.json"
    file.write_text(Profile(state_root=str(tmp_path / "state")).model_dump_json())
    result = subprocess.run([sys.executable, "-m", "robot_rsi.cli", "--profile", str(file), "project"],
                            text=True, capture_output=True)
    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["code"] == "project_required"
    assert not result.stderr


def configured(tmp_path, **changes):
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    for name, body in {"common": "Facts: units are metres; preserve this constraint.",
                       "run": "operational knowledge", "dev": "DEV_ONLY hypothesis",
                       "eval": "EVAL_ONLY answers"}.items():
        (root / (name + ".md")).write_text(body)
    config = ProjectConfig(id="slider", root=str(root), common_key="common", materials=[
        {"key": "common", "title": "facts", "path": "common.md",
         "purposes": ["onboarding", "run", "improve", "evaluate"]},
        {"key": "run", "title": "operation", "path": "run.md", "purposes": ["run", "improve"]},
        {"key": "dev", "title": "hypothesis", "path": "dev.md", "purposes": ["improve", "onboarding"]},
        {"key": "eval", "title": "answers", "path": "eval.md", "purposes": ["evaluate"]},
    ])
    return Profile(state_root=str(tmp_path / "state"), project=config, **changes)


def request(**changes):
    return IssueCreate(route="framework_contract", rationale="Public result schema cannot express uncertainty",
                       component="robot-rsi-harness", expected="FK covariance", actual="extra forbidden",
                       reproducer="FKResult.model_validate(valid | {'covariance_m2': [1]})", **changes)


def reply(issue, **changes):
    return FrameworkReply.model_validate({
        "id": "reply_1", "issue_id": issue["id"], "project_id": issue["project_id"],
        "component": issue["component"], "affected_versions": "==" + CORE_VERSION, "target_version": "99.0.0",
        "disposition": "update_available", "handling": "framework", "contracts": [],
        "summary": "Simulated future version notice", "details": "Protocol fixture, no real release",
        "migration": "Install target outside active tasks", "validation": "Recompare selected task",
        "provenance": "simulated_fixture", **changes}).model_dump()


async def test_purpose_scope_lazy_materials_and_bounded_views(tmp_path, monkeypatch):
    profile = configured(tmp_path)
    opened = []
    original = Path.read_text

    def read(path, *args, **kwargs):
        opened.append(path.name)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    async with Runtime(profile) as runtime:
        opened.clear()
        for tool, arguments in [("workspace_read", {}), ("task_begin", {"task_id": "one", "goal": "run"}),
                                ("task_context", {"task_id": "one"})]:
            result = await runtime.invoke(tool, arguments)
            assert result["ok"], result
            assert "DEV_ONLY" not in json.dumps(result) and "EVAL_ONLY" not in json.dumps(result)
        assert "dev.md" not in opened and "eval.md" not in opened
        assert (await runtime.invoke("project_read", {"key": "dev"}))["error"]["code"] == "not_found"
        assert (await runtime.invoke("project_read", {"key": "run"}))["result"]["text"] == "operational knowledge"
        cards = runtime.project.read(ProjectRead())
        assert cards["visible_count"] == 2 and len(cards["materials"]) == 1
    for purpose, role, key in [("onboarding", "development", "dev"), ("improve", "development", "dev"),
                               ("evaluate", "evaluation", "eval")]:
        other = profile.model_copy(update={"role": role, "project": profile.project.model_copy(update={"purpose": purpose})})
        assert Project(other).read(ProjectRead(key=key))["text"]
    with pytest.raises(HarnessError, match="existing role"):
        Project(profile.model_copy(update={"project": profile.project.model_copy(update={"purpose": "improve"})}))
    (tmp_path / "project/run.md").unlink()
    (tmp_path / "project/run.md").symlink_to(tmp_path / "project/dev.md")
    with pytest.raises(HarnessError, match="without symlinks"):
        Project(profile).read(ProjectRead(key="run"))


async def test_feedback_link_version_paths_host_boundary_and_changed_blocks(tmp_path):
    profile = configured(tmp_path)
    async with Runtime(profile) as runtime:
        issue = runtime.feedback.create(request())
        assert runtime.feedback.get(IssueGet())["issues"][0]["status"] == "pending"
        await runtime.invoke("task_begin", {"task_id": "one", "goal": "run"})
        first = (await runtime.invoke("task_context", {"task_id": "one", "blocks": True}))["result"]
        result = runtime.feedback.import_reply(reply(issue))
        assert result["status"] == "update_available" and result["installed_version"] == CORE_VERSION
        changed = (await runtime.invoke("task_context", {"task_id": "one", "blocks": True, "delivered": first["delivered"]}))["result"]
        assert "workspace" in changed["blocks"]
        repeated = (await runtime.invoke("task_context", {"task_id": "one", "blocks": True, "delivered": changed["delivered"]}))["result"]
        assert repeated["blocks"] == {}
        assert "Protocol fixture" not in json.dumps(changed["blocks"])
        assert runtime.feedback.import_reply(reply(issue))["status"] == "update_available"
        with pytest.raises(HarnessError, match="immutable"):
            runtime.feedback.import_reply(reply(issue, summary="changed"))
        with pytest.raises(HarnessError, match="match the issue"):
            runtime.feedback.import_reply(reply(issue, id="bad", project_id="other"))
        with pytest.raises(HarnessError, match="actual installed"):
            runtime.feedback.attest("reply_1", "one", "missing", "cannot claim install")
        assert runtime.feedback.import_reply(reply(issue, id="needs", disposition="needs_information", target_version=None))["status"] == "needs_information"
        assert runtime.feedback.import_reply(reply(issue, id="range", affected_versions=">=9"))["status"] == "version_not_applicable"
        assert runtime.feedback.import_reply(reply(issue, id="other", contracts=["unselected.contract"]))["status"] == "unrelated_dependencies"
        assert all(c.get("reply_id") != "other" for c in runtime.feedback.cards())
        assert runtime.feedback.import_reply(reply(issue, id="present", target_version=CORE_VERSION, disposition="answered"))["status"] == "installed_needs_validation"
        assert not {"feedback_import", "feedback_attest", "import_reply"} & TOOLS.keys()
        assert not (await runtime.invoke("feedback_import", reply(issue)))["ok"]
        await runtime.invoke("task_begin", {"task_id": "source", "goal": "Read the workflow"})
        await runtime.invoke("skill_describe", {"name": "workspace_context"}, task_id="source")
        source = runtime.task_memory._load("source")
        assert source["skill_versions"]["workspace_context"] == "0.4.2"
        await runtime.invoke("task_cancel", {"task_id": "source"})
        await runtime.invoke("task_archive", {"task_id": "source"})
        runtime.registry.disable("workspace_context")
        historical = runtime.feedback.create(request(source_task="source"))
        assert historical["skill_versions"] == source["skill_versions"]
        assert historical["source_installation_digest"] == source["execution"]["installation_digest"]
    async with Runtime(profile) as restarted:
        assert restarted.feedback.get(IssueGet(issue_id=issue["id"]))["replies"]
    async with Runtime(profile.model_copy(update={"role": "development", "project": profile.project.model_copy(update={"purpose": "improve"})})) as other:
        assert other.feedback.get(IssueGet())["issues"] == []


async def test_update_remains_discoverable_and_snapshot_pages_cover_pending(tmp_path):
    async with Runtime(configured(tmp_path)) as runtime:
        issue = runtime.feedback.create(request())
        runtime.feedback.import_reply(reply(issue))
        pending = [runtime.feedback.create(request()) for _ in range(3)]
        await runtime.invoke("task_begin", {"task_id": "browse", "goal": "Check relevant changes"})
        workspace = (await runtime.invoke("workspace_read", {}, task_id="browse"))["result"]
        assert any(c.get("reply_id") == "reply_1" for c in workspace["framework_updates"])
        index = workspace["framework_update_index"]
        assert (index["total"], index["update_count"], index["pending_count"]) == (4, 1, 3)
        assert index["browse"] == {"tool": "issue_get", "arguments": {}}
        page = (await runtime.invoke("issue_get", {}))["result"]
        assert page["total"] == 4 and len(page["issues"]) == 3 and page["next_cursor"]
        added = runtime.feedback.create(request())
        following = (await runtime.invoke("issue_get", {"cursor": page["next_cursor"]}))["result"]
        assert following["total"] == 4 and following["next_cursor"] is None
        covered = page["issues"] + following["issues"]
        assert {c["issue_id"] for c in covered} == {issue["id"], *(p["id"] for p in pending)}
        assert added["id"] not in {c["issue_id"] for c in covered}
        assert runtime.feedback.get(IssueGet())["total"] == 5
        assert len(runtime.feedback.get(IssueGet(kind="updates"))["issues"]) == 1
        assert runtime.feedback.get(IssueGet(kind="pending"))["total"] == 4
        runtime.feedback.import_reply(reply(issue, id="unrelated", contracts=["unselected.contract"]))
        assert runtime.feedback.get(IssueGet())["total"] == 5
        assert all(c.get("reply_id") != "unrelated" for c in runtime.feedback.cards())
        first = (await runtime.invoke("task_context", {"task_id": "browse", "blocks": True}))["result"]
        again = (await runtime.invoke("task_context", {"task_id": "browse", "blocks": True,
                                                       "delivered": first["delivered"]}))["result"]
        assert again["blocks"] == {}
        assert "Protocol fixture" not in json.dumps(workspace)


async def test_example_oracle_stays_with_host_and_real_output_is_allowed(tmp_path):
    pytest.importorskip("lab_adapter")
    from robot_rsi.experience import Experience

    spec = importlib.util.spec_from_file_location("improve_demo", ROOT / "examples/project_adapter/improve_demo.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    demo.prepare(tmp_path)
    parameters = {"position_m": .3, "offset_mm": 0}
    private_oracle = [91.234567, 82.345678, 73.456789]
    async with Runtime(demo.profile(tmp_path)) as runtime:
        runtime.registry.register(tmp_path / "baseline")
        runtime.registry.enable("lab_offset", "1.0.0")
        ref = await demo.binding(runtime)
        failed = await demo.scored_task(runtime, "private-oracle", {"parameters": parameters, "expected": private_oracle}, ref)
        succeeded = await demo.scored_task(runtime, "real-output", {"parameters": parameters, "expected": [.3, 0, .25]}, ref)
        selected = Experience(runtime.store).export("private-oracle")
        assert failed["task_result"]["outcome"] == "failed"
        assert succeeded["task_result"]["outcome"] == "succeeded"
        for row in (failed, succeeded):
            assert row["context_before_execution"]["checkpoint"]["goal"] == demo.input_goal(parameters)
            assert row["task_result"]["goal"] == demo.input_goal(parameters)
            assert row["operation"]["args"]["parameters"] == parameters
            assert row["operation"]["result"]["tip_m"] == [.3, 0, .25]
        # Inspect actual records, not just DEV_ONLY/EVAL_ONLY sentinels. Legitimate measured
        # coordinates above remain visible even when they equal the real oracle.
        records = json.dumps([failed["context_before_execution"], failed["task_result"], selected])
        assert all(str(value) not in records for value in private_oracle)
        assert "expected" not in failed["task_result"]["summary"]
