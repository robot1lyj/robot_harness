import json
import subprocess
import sys
import zipfile

import pytest

from robot_rsi.contracts import HarnessError
from robot_rsi.host_boundary import command, protection, sha256, snapshot, verify
from robot_rsi.host_install import prepare, wheel_metadata


def approved(tmp_path):
    root = tmp_path / "host"
    (root / "env").mkdir(parents=True)
    (root / "env/core.py").write_text("approved")
    (root / "python").write_text("interpreter")
    data = {"format": 1, "root": str(root), "python": str(root / "python"),
            "python_sha256": sha256(root / "python"), "files": snapshot(root)}
    (root / "approved.json").write_text(json.dumps(data))
    return root


@pytest.mark.parametrize("mutation", ["write", "bytecode", "mode", "symlink"])
def test_startup_verification_is_fresh_and_does_not_adopt_changes(tmp_path, mutation):
    root = approved(tmp_path)
    original = (root / "approved.json").read_bytes()
    verify(root)
    if mutation == "write":
        (root / "env/core.py").write_text("tampered")
    elif mutation == "bytecode":
        (root / "env/core.pyc").write_bytes(b"injected")
    elif mutation == "mode":
        (root / "env/core.py").chmod(0o777)
    else:
        (root / "env/core.py").unlink()
        (root / "env/core.py").symlink_to(root / "python")
    with pytest.raises(ValueError, match="differs"):
        verify(root)
    assert (root / "approved.json").read_bytes() == original


def test_mutable_process_cannot_claim_os_protection_from_an_environment_variable(tmp_path, monkeypatch):
    root = approved(tmp_path)
    data = json.loads((root / "approved.json").read_text())
    data.update(core={"version": "fixture"}, installation={"digest": "fixture"})
    (root / "approved.json").write_text(json.dumps(data))
    monkeypatch.setenv("ROBOT_RSI_HOST_BASELINE", str(root / "approved.json"))
    assert not protection()["protected"]


def test_host_never_adopts_existing_installation_or_wrong_artifact(tmp_path):
    with pytest.raises(HarnessError, match="新目录"):
        prepare(tmp_path, "unused", "unused", "unused")
    wheel = tmp_path / "core.whl"
    wheel.write_bytes(b"wrong artifact")
    with pytest.raises(HarnessError) as error:
        prepare(tmp_path / "new", wheel, "0" * 64, "unused")
    assert "SHA256" in error.value.details["cause"]


@pytest.mark.parametrize("entry", ["../escape.py", "/absolute.py"])
def test_wheel_paths_cannot_escape_installation(tmp_path, entry):
    wheel = tmp_path / "bad.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(entry, "bad")
    with pytest.raises(ValueError, match="unsafe"):
        wheel_metadata(wheel)


@pytest.mark.parametrize("filename,name,release", [
    ("other-0.3.1-py3-none-any.whl", "other", "0.3.1"),
    ("robot_rsi_harness-0.3.1-py3-none-any.whl", "robot-rsi-harness", "0.3.0"),
])
def test_selected_core_release_name_and_metadata_must_match(tmp_path, filename, name, release):
    wheel = tmp_path / filename
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("core.dist-info/METADATA", f"Name: {name}\nVersion: {release}\n")
    with pytest.raises(HarnessError) as error:
        prepare(tmp_path / "new", wheel, sha256(wheel), "unused")
    assert "matching release metadata" in error.value.details["cause"]
    assert not (tmp_path / "new/approved.json").exists()


def test_host_preparation_is_before_project_profile_or_runtime_loading(tmp_path):
    reply = subprocess.run([sys.executable, "-m", "robot_rsi.cli", "--profile", "missing.json",
                            "host", "prepare", "--directory", str(tmp_path), "--core-wheel", "unused",
                            "--core-sha256", "unused", "--requirements", "unused"],
                           capture_output=True, text=True)
    assert reply.returncode == 1
    assert json.loads(reply.stdout)["error"]["code"] == "host_directory_exists"


def test_writable_scope_cannot_swallow_baseline_or_follow_venv_symlink(tmp_path):
    root = approved(tmp_path)
    with pytest.raises(ValueError, match="disjoint"):
        command(root, tmp_path, "run", [])
    project = tmp_path / "project"
    project.mkdir()
    (project / ".venv").symlink_to(root / "env")
    with pytest.raises(ValueError, match="ordinary"):
        command(root, project, "run", [])
