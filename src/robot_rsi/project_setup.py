"""Explicit usage-project bootstrap; uv owns dependency resolution and installation."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tomllib
import zipfile
from email.parser import BytesParser
from importlib.metadata import distribution
from importlib.resources import files
from pathlib import Path

from pydantic import ValidationError

from . import __version__
from .contracts import HarnessError, Profile
from .prompts import base_prompt


def default_project_profile(directory=None):
    """Resolve the nearest declared usage Profile; explicit selections bypass this helper."""
    current = Path(directory or Path.cwd()).resolve()
    for root in (current, *current.parents):
        declaration = root / "pyproject.toml"
        if not declaration.is_file():
            continue
        try:
            data = tomllib.loads(declaration.read_text())
            settings = data.get("tool", {}).get("robot-rsi", {})
        except (OSError, ValueError, AttributeError) as exc:
            raise HarnessError("project_profile_invalid", "无法读取项目配置声明",
                               details={"project_file": str(declaration), "reason": str(exc)}) from exc
        if not isinstance(settings, dict):
            raise HarnessError("project_profile_invalid", "tool.robot-rsi 必须是配置表",
                               details={"project_file": str(declaration)})
        if "profile" not in settings:
            continue
        pointer = settings["profile"]
        if not isinstance(pointer, str) or not pointer.strip():
            raise HarnessError("project_profile_invalid", "项目 profile 声明必须是非空路径",
                               details={"project_file": str(declaration)})
        path = (root / Path(pointer).expanduser()).resolve()
        details = {"project_file": str(declaration), "profile_file": str(path)}
        if not path.is_file():
            raise HarnessError("project_profile_missing", "项目声明的 Profile 文件不存在", details=details)
        try:
            content = json.loads(path.read_text())
            if isinstance(content, dict):
                # Only the existing Profile's filesystem roots use the declaring project root.
                for scope, fields in ((content, ("state_root",)), (content.get("project"), ("root", "usage_root"))):
                    if isinstance(scope, dict):
                        for field in fields:
                            if isinstance(scope.get(field), str):
                                scope[field] = str((root / Path(scope[field]).expanduser()).resolve())
            return Profile.model_validate(content)
        except ValidationError as exc:
            raise HarnessError("project_profile_invalid", "项目声明的 Profile 字段无效",
                               details={**details, "fields": [list(e["loc"]) for e in exc.errors()]}) from exc
        except (OSError, ValueError) as exc:
            raise HarnessError("project_profile_invalid", "项目声明的 Profile 无法读取或解析",
                               details={**details, "reason": str(exc)}) from exc
    return None


def guide(section):
    resource = files("robot_rsi").joinpath("resources")
    if section == "base":
        return base_prompt()["text"]
    if section == "onboarding":
        return resource.joinpath("robot-onboarding/SKILL.md").read_text()
    if section == "agent":
        return (f"Installed Robot RSI Harness {__version__}\n\n" + guide("base") + "\n\n"
                + guide("onboarding") + "\n\n" + guide("project"))
    name = "project-workflow" if section == "project" else section
    path = Path(distribution("robot-rsi-harness").locate_file(
        f"robot_rsi/resources/guides/{name}.md"))
    if not path.is_file() or path.is_symlink():
        raise HarnessError("guide_not_installed", "请安装含公共指南的 wheel；不回退读取开发树")
    return path.read_text()


def _wheel(path):
    path = Path(path).expanduser().resolve(strict=True)
    try:
        with zipfile.ZipFile(path) as archive:
            entries = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
            if len(entries) != 1:
                raise ValueError("Expected one distribution metadata entry")
            data = BytesParser().parsebytes(archive.read(entries[0]))
        if data["Name"] != "robot-rsi-harness" or data["Version"] != __version__:
            raise ValueError("core wheel must match the installed bootstrap version")
    except (ValueError, zipfile.BadZipFile) as exc:
        raise HarnessError("invalid_core_wheel", str(exc)) from exc
    return path


def initialize(directory, core_wheel, *, source=None, sync=True):
    root = Path(directory).expanduser().resolve()
    wheel = _wheel(core_wheel)
    source = Path(source).expanduser().resolve(strict=True) if source else None
    if source and (not source.is_dir() or source == root):
        raise HarnessError("invalid_source", "旧来源须是与使用目录分开的现有目录")
    project_file = root / "pyproject.toml"
    if project_file.is_symlink():
        raise HarnessError("project_exists", "保留已有 pyproject 符号链接，请显式整理项目角色")
    if project_file.exists():
        data = tomllib.loads(project_file.read_text())
        if data.get("project", {}).get("name") == "robot-rsi-harness":
            raise HarnessError("project_role_conflict", "当前目录是框架工程；先备份并转换当前 worktree 的使用角色，不能另选隐藏目录")
        if data.get("tool", {}).get("robot-rsi", {}).get("profile"):
            return {"status": "existing", "root": str(root), "overwritten": [],
                    "installation": "not_repeated", "next": "保留项目；依赖更新请显式修改声明、uv lock 和 uv sync --locked --no-dev --no-editable"}
        raise HarnessError("project_exists", "已有使用项目声明保持不变；请将固定框架依赖与 Profile 入口合入当前项目")
    for name in ("profiles/development.json", "profiles/runtime.json"):
        if (root / name).exists() or (root / name).is_symlink():
            raise HarnessError("project_exists", f"保留已有配置 {name}，未初始化")
    for name in ("profiles", "extensions", "skills", "assets", "wheels", ".rsi", ".rsi/state", ".rsi/harness"):
        path = root / name
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise HarnessError("invalid_project_path", f"生成路径必须是当前项目内的普通目录：{name}")
    destination = root / "wheels" / wheel.name
    if destination.is_symlink() or (destination.exists() and destination.read_bytes() != wheel.read_bytes()):
        raise HarnessError("project_exists", "已有同名 wheel 保持不变；请显式选择版本")
    for name in ("README.md", "AGENTS.md", ".gitignore"):
        if (root / name).is_symlink():
            raise HarnessError("invalid_project_path", f"入口 {name} 不能是符号链接")
    root.mkdir(parents=True, exist_ok=True)
    for name in ("profiles", "extensions", "skills", "assets", "wheels", ".rsi/state", ".rsi/harness"):
        (root / name).mkdir(parents=True, exist_ok=True)
    created, preserved = [], []

    def write(name, content):
        path = root / name
        if path.exists():
            preserved.append(name)
            return False
        with path.open("x") as stream:
            stream.write(content)
        created.append(name)
        return True

    slug = re.sub(r"[^a-z0-9-]+", "-", root.name.lower()).strip("-")[:48] or "robot-use"
    write("pyproject.toml", f'''[project]
name = {json.dumps(slug)}
version = "0.1.0"
requires-python = ">=3.12,<3.14"
dependencies = ["robot-rsi-harness=={__version__}"]

[tool.uv]
package = false

[tool.uv.sources]
robot-rsi-harness = {{path = "wheels/{wheel.name}"}}

[tool.robot-rsi]
profile = "profiles/runtime.json"
''')
    if destination != wheel and not destination.exists():
        shutil.copyfile(wheel, destination)
        created.append("wheels/" + wheel.name)
    for role, purpose in (("development", "onboarding"), ("runtime", "run")):
        profile = Profile(role=role, station="robot-project", namespace=slug,
            robot=None, state_root=str(root / ".rsi/state"),
            host={"adapter": None, "cameras": None, "depth": None},
            project={"id": slug, "root": str(source if role == "development" and source else root),
                     "usage_root": str(root), "purpose": purpose},
            workspace={"title": root.name, "purpose": "在当前项目维护和使用机器人能力",
                       "constraints": ["初始化未配置机器人或相机；连接与动作须有宿主授权及实际实现。"]})
        write(f"profiles/{role}.json", profile.model_dump_json(indent=2, exclude_defaults=True) + "\n")
    resource = files("robot_rsi").joinpath("resources/project")
    for name in ("README.md", "AGENTS.md"):
        content = resource.joinpath(name).read_text().replace("{{CORE_VERSION}}", __version__)
        if not write(name, content):
            write(".rsi/harness/" + name, content)
    write(".gitignore", ".venv/\n.rsi/\n__pycache__/\n*.pyc\nwheels/\n")
    if sync:
        for command in (["uv", "lock"], ["uv", "sync", "--locked", "--no-dev", "--no-editable"]):
            result = subprocess.run(command, cwd=root, capture_output=True, text=True)
            if result.returncode:
                raise HarnessError("project_install_failed", "项目文件已保留；按返回的 uv 命令修正安装后接续",
                                   details={"command": command, "stderr": result.stderr})
    return {"status": "created", "root": str(root), "created": created, "preserved": preserved,
            "profile": "profiles/runtime.json", "installation": "completed" if sync else "not_requested",
            "next": "在当前目录用已有 coding Agent；先运行 uv run --no-sync robot-rsi guide agent。已有入口需合并 .rsi/harness 的短桥接。"}
