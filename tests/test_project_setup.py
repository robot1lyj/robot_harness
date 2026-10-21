import json
import tomllib
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from robot_rsi import __version__
from robot_rsi.contracts import HarnessError, Profile
from robot_rsi.project_setup import default_project_profile, guide, initialize
from robot_rsi.prompts import base_prompt


@pytest.fixture
def wheel(tmp_path):
    path = tmp_path / f"robot_rsi_harness-{__version__}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("core.dist-info/METADATA", f"Name: robot-rsi-harness\nVersion: {__version__}\n")
    return path


def test_current_usage_root_and_preserved_user_files_and_repeat(tmp_path, wheel):
    source = tmp_path / "legacy"
    source.mkdir()
    (source / "README.md").write_text("Legacy source, unchanged")
    root = tmp_path / "robot-use"
    root.mkdir()
    for name in ("AGENTS.md", "README.md"):
        (root / name).write_text("User content")
    initialized = initialize(root, wheel, source=source, sync=False)
    assert initialized["root"] == str(root) and initialized["installation"] == "not_requested"
    assert tomllib.loads((root / "pyproject.toml").read_text())["project"]["name"] != "robot-rsi-harness"
    for role in ("development", "runtime"):
        assert "yam_url" not in json.loads((root / f"profiles/{role}.json").read_text())
        profile = Profile.model_validate_json((root / f"profiles/{role}.json").read_text())
        assert profile.state_root == str(root / ".rsi/state") and profile.robot is None
        assert profile.project.usage_root == str(root)
        assert profile.project.root == str(source if role == "development" else root)
        assert profile.host.adapter is None and profile.host.cameras is None
    assert (root / "AGENTS.md").read_text() == (root / "README.md").read_text() == "User content"
    assert (root / ".rsi/harness/AGENTS.md").is_file()
    (root / "extensions/local.py").write_text("local")
    before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert initialize(root, wheel, sync=False)["status"] == "existing"
    assert before == {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert (source / "README.md").read_text() == "Legacy source, unchanged"


@pytest.mark.parametrize("project,code", [("robot-rsi-harness", "project_role_conflict"), ("my-app", "project_exists")])
def test_project_role_conflict_does_not_redirect_or_write(tmp_path, wheel, project, code):
    root = tmp_path / "existing"
    root.mkdir()
    (root / "pyproject.toml").write_text(f'[project]\nname="{project}"\n')
    before = (root / "pyproject.toml").read_bytes()
    with pytest.raises(HarnessError) as error:
        initialize(root, wheel, sync=False)
    assert error.value.code == code
    assert (root / "pyproject.toml").read_bytes() == before
    assert sorted(p.name for p in root.iterdir()) == ["pyproject.toml"]


def test_profile_and_generated_path_collisions_never_overwrite(tmp_path, wheel):
    root = tmp_path / "use"
    (root / "profiles").mkdir(parents=True)
    (root / "profiles/runtime.json").write_text("user configuration")
    with pytest.raises(HarnessError, match="保留已有配置"):
        initialize(root, wheel, sync=False)
    assert not (root / "pyproject.toml").exists()
    other = tmp_path / "other"
    other.mkdir()
    second = tmp_path / "second"
    second.mkdir()
    (second / "extensions").symlink_to(other, target_is_directory=True)
    with pytest.raises(HarnessError) as error:
        initialize(second, wheel, sync=False)
    assert error.value.code == "invalid_project_path" and not list(other.iterdir())


def test_initial_install_delegates_only_to_uv(tmp_path, wheel, monkeypatch):
    commands = []

    def run(command, **kwargs):
        commands.append((command, kwargs["cwd"]))
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("robot_rsi.project_setup.subprocess.run", run)
    root = tmp_path / "use"
    assert initialize(root, wheel)["installation"] == "completed"
    assert commands == [(["uv", "lock"], root), (["uv", "sync", "--locked", "--no-dev", "--no-editable"], root)]


def test_installed_guide_and_bridge_share_sources_without_checkout_fallback(tmp_path, monkeypatch):
    from robot_rsi import project_setup

    monkeypatch.setattr(project_setup, "distribution", lambda _: SimpleNamespace(locate_file=lambda name: tmp_path / name))
    path = tmp_path / "robot_rsi/resources/guides/project-workflow.md"
    path.parent.mkdir(parents=True)
    path.write_text("当前项目的公开指南")
    manifest = json.loads(Path(project_setup.__file__).with_name("resources").joinpath("robot-onboarding/skill.json").read_text())
    text = guide("agent")
    assert base_prompt()["text"] in text and guide("onboarding") in text and path.read_text() in text
    assert manifest["version"] == "1.7.1"
    path.unlink()
    with pytest.raises(HarnessError) as error:
        guide("agent")
    assert error.value.code == "guide_not_installed"


def test_nearest_project_profile_and_relative_roots_use_declaring_directory(tmp_path, monkeypatch):
    root = tmp_path / "use"
    nested = root / "extensions/pkg"
    nested.mkdir(parents=True)
    (root / "profiles").mkdir()
    (root / "pyproject.toml").write_text('[tool.robot-rsi]\nprofile="profiles/runtime.json"\n')
    (root / "profiles/runtime.json").write_text(json.dumps({
        "robot": None, "state_root": ".rsi/state", "namespace": "parent",
        "host": {"cameras": None, "depth": None},
        "project": {"root": ".", "usage_root": ".", "purpose": "run"}}))
    (nested / "pyproject.toml").write_text('[project]\nname="local-extension"\n')
    monkeypatch.chdir(nested)
    profile = default_project_profile()
    assert profile.robot is None and profile.namespace == "parent"
    assert profile.state_root == str(root / ".rsi/state")
    assert profile.project.root == profile.project.usage_root == str(root)
    (nested / "pyproject.toml").write_text('[tool.robot-rsi]\nprofile="local.json"\n')
    (nested / "local.json").write_text(json.dumps({"robot": None, "namespace": "nearest"}))
    assert default_project_profile().namespace == "nearest"


@pytest.mark.parametrize("pointer,content,code", [
    ("missing.json", None, "project_profile_missing"),
    ("bad.json", "{broken", "project_profile_invalid"),
    ("bad.json", '{"role":"invalid"}', "project_profile_invalid"),
    ("", None, "project_profile_invalid"),
    (123, None, "project_profile_invalid"),
])
def test_bad_project_declaration_never_falls_back(tmp_path, pointer, content, code):
    value = json.dumps(pointer)
    (tmp_path / "pyproject.toml").write_text(f"[tool.robot-rsi]\nprofile={value}\n")
    if content is not None:
        (tmp_path / str(pointer)).write_text(content)
    with pytest.raises(HarnessError) as error:
        default_project_profile(tmp_path)
    assert error.value.code == code and error.value.details["project_file"] == str(tmp_path / "pyproject.toml")


def test_cli_explicit_and_environment_profiles_precede_project_default(tmp_path, monkeypatch, capsys):
    import sys

    from robot_rsi import cli

    (tmp_path / "pyproject.toml").write_text('[tool.robot-rsi]\nprofile="missing.json"\n')
    environment = tmp_path / "environment.json"
    explicit = tmp_path / "explicit.json"
    environment.write_text(Profile(role="development", robot=None, namespace="environment").model_dump_json())
    explicit.write_text(Profile(robot=None, namespace="explicit").model_dump_json())
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ROBOT_RSI_PROFILE", str(environment))
    seen = []

    async def run(args, profile):
        seen.append(profile)
        return 0

    monkeypatch.setattr(cli, "run_command", run)
    for arguments in (["--profile", str(explicit), "tools"], ["tools"]):
        monkeypatch.setattr(sys, "argv", ["robot-rsi", *arguments])
        with pytest.raises(SystemExit) as result:
            cli.main()
        assert result.value.code == 0
    assert [p.namespace for p in seen] == ["explicit", "environment"]
    assert seen[1].role == "development"
    capsys.readouterr()


@pytest.mark.parametrize("entry", ["mcp", "agent"])
def test_cli_source_entry_ignores_even_invalid_implicit_default(tmp_path, monkeypatch, entry, capsys):
    import sys

    from robot_rsi import agent_host, cli, mcp_server

    source = tmp_path / "source"
    source.mkdir()
    (tmp_path / "pyproject.toml").write_text('[tool.robot-rsi]\nprofile="missing.json"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ROBOT_RSI_PROFILE", raising=False)
    monkeypatch.setenv("ROBOT_RSI_STATE_ROOT", str(tmp_path / "source-state"))
    seen = []

    def create(profile):
        seen.append(profile)
        return SimpleNamespace(run=lambda **kwargs: None)

    async def run(profile, *args):
        seen.append(profile)
        return {"status": "complete", "task_id": "source-protocol"}

    monkeypatch.setattr(mcp_server, "create_mcp", create)
    monkeypatch.setattr(agent_host, "run_agent", run)
    arguments = ["mcp", "--project", str(source)] if entry == "mcp" else ["agent", "run", "--project", str(source), "普通来源方案"]
    monkeypatch.setattr(sys, "argv", ["robot-rsi", *arguments])
    if entry == "agent":
        with pytest.raises(SystemExit) as result:
            cli.main()
        assert result.value.code == 0
    else:
        cli.main()
    assert seen[0].robot is None and seen[0].project.root == str(source)
    assert seen[0].role == "development" and seen[0].state_root == str(tmp_path / "source-state")
    capsys.readouterr()


def test_no_project_declaration_keeps_legacy_fallback(tmp_path):
    assert default_project_profile(tmp_path) is None
