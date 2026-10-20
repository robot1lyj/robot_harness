"""Explicit host preparation from chosen artifacts. Never bless an existing installation."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import zipfile
from email.parser import BytesParser
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from .contracts import HarnessError
from .host_boundary import sha256, snapshot, verify


def wheel_metadata(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if any(Path(n).is_absolute() or ".." in Path(n).parts or stat.S_ISLNK(archive.getinfo(n).external_attr >> 16)
               for n in names):
            raise ValueError("Wheel contains unsafe paths or symlinks")
        metadata = [n for n in names if n.endswith(".dist-info/METADATA")]
        if len(metadata) != 1:
            raise ValueError("Wheel requires one distribution metadata entry")
        info = BytesParser().parsebytes(archive.read(metadata[0]))
    return canonicalize_name(info["Name"]), info, names


def prepare(directory, core_wheel, core_sha256, requirements, *, wheels=(), uv=None, codex=None):
    root = Path(directory).expanduser().absolute()
    if root.exists() or root.is_symlink():
        raise HarnessError("host_directory_exists", "宿主安装必须使用新目录；不采纳现有可写安装")
    root.mkdir(parents=True)
    root = root.resolve()
    try:
        artifact = root / "artifacts"
        artifact.mkdir()
        core_wheel = Path(core_wheel).resolve(strict=True)
        if sha256(core_wheel) != core_sha256:
            raise ValueError("Selected core wheel SHA256 differs from the host's expected value")
        name, metadata, core_names = wheel_metadata(core_wheel)
        if name != "robot-rsi-harness" or metadata["Version"] != "0.3.0":
            raise ValueError("Choose the core 0.3.0 release artifact")
        selected_core = artifact / core_wheel.name
        shutil.copyfile(core_wheel, selected_core)
        requirements = Path(requirements).resolve(strict=True)
        text = requirements.read_text()
        # Dependency-only hashed export. No project/lock discovery and no source builds.
        if re.search(r"(?im)^\s*(?:robot[-_]rsi[-_]harness\b|-e\b|--editable\b)", text):
            raise ValueError("Requirements must exclude the core and editable sources")
        locked = artifact / "dependencies.lock"
        locked.write_text(text)
        tools = root / "tools"
        tools.mkdir()
        for label, source in (("uv", uv or shutil.which("uv")), ("codex", codex)):
            if source:
                source = Path(source).resolve(strict=True)
                shutil.copyfile(source, tools / label)
                (tools / label).chmod(0o755)
                sibling = source.with_name("codex-code-mode-host")
                if label == "codex" and sibling.is_file():
                    shutil.copyfile(sibling, tools / sibling.name)
                    (tools / sibling.name).chmod(0o755)
        if not (tools / "uv").is_file():
            raise ValueError("Existing uv executable is required")
        log = root / "prepare.log"
        def run(*arguments):
            with log.open("a") as stream:
                subprocess.run(list(map(str, arguments)), check=True, stdout=stream, stderr=stream,
                               env={**os.environ, "UV_OFFLINE": "true", "PYTHONDONTWRITEBYTECODE": "1"})
        run(tools / "uv", "venv", "--python", Path(sys.executable).resolve(), root / "env")
        python = root / "env/bin/python"
        installation = artifact / "base.lock"
        installation.write_text(text + f"\nrobot-rsi-harness @ {selected_core.as_uri()} --hash=sha256:{core_sha256}\n")
        run(tools / "uv", "pip", "install", "--python", python, "--require-hashes", "--only-binary", ":all:", "--no-deps", "-r", installation)
        # Reject package startup hooks and base collisions before executing candidate code.
        base = snapshot(root / "env")
        site = next((root / "env/lib").glob("python*/site-packages"))
        protected_modules = {p.name.split(".")[0] for p in site.iterdir() if not p.name.endswith(".dist-info")}
        candidates = []
        for source in wheels:
            source = Path(source).resolve(strict=True)
            package, info, names = wheel_metadata(source)
            def forbidden(n):
                path = Path(n)
                parts = path.parts
                if len(parts) > 2 and parts[0].endswith(".data") and parts[1] in ("purelib", "platlib"):
                    parts = parts[2:]
                return (path.suffix in (".pth", ".egg-link")
                        or path.stem in ("sitecustomize", "usercustomize")
                        or parts[0].split(".")[0] in protected_modules)
            if package == "robot-rsi-harness" or any(forbidden(n) for n in names if n):
                raise ValueError("Local wheel cannot replace core/dependencies or inject Python startup hooks")
            for dependency in info.get_all("Requires-Dist", []):
                requirement = Requirement(dependency)
                if canonicalize_name(requirement.name) == "robot-rsi-harness":
                    if requirement.url or metadata["Version"] not in requirement.specifier:
                        raise ValueError("Local wheel requires another core artifact/version; adjust packaging compatibility explicitly")
            destination = artifact / source.name
            shutil.copyfile(source, destination)
            candidates.append({"name": package, "version": info["Version"], "file": destination.name, "sha256": sha256(destination)})
        if candidates:
            selected = artifact / "local.lock"
            selected.write_text("".join(f"{c['name']} @ {(artifact / c['file']).as_uri()} --hash=sha256:{c['sha256']}\n" for c in candidates))
            run(tools / "uv", "pip", "install", "--python", python, "--require-hashes", "--no-deps", "-r", selected)
            current = snapshot(root / "env")
            if any(current.get(n) != identity for n, identity in base.items()):
                raise ValueError("Local installation altered a protected base file")
        run(tools / "uv", "pip", "check", "--python", python)
        # Core package contents are checked against the selected wheel, not RECORD/self-report.
        with zipfile.ZipFile(selected_core) as archive:
            expected = {n: archive.read(n) for n in core_names if n.startswith("robot_rsi/") and not n.endswith("/")}
            actual = {"robot_rsi/" + str(p.relative_to(site / "robot_rsi")): p.read_bytes()
                      for p in (site / "robot_rsi").rglob("*") if p.is_file()}
            if actual != expected:
                raise ValueError("Installed core contents differ from the selected wheel")
            (root / "launch.py").write_bytes(archive.read("robot_rsi/host_boundary.py"))
        (root / "launch.py").chmod(0o755)
        # Entry point is isolated even when a coding client uses uv run or PATH.
        (root / "env/bin/robot-rsi").write_text(
            f'#!/bin/sh\nexec {shlex.quote(str(python))} -I -B -m robot_rsi.cli "$@"\n')
        (root / "env/bin/robot-rsi").chmod(0o755)
        body = json.loads(subprocess.check_output([str(python), "-I", "-B", "-c",
            "import json,sys;from robot_rsi.installation import installation_identity;print(json.dumps({'installation':installation_identity(),'python_base':sys.base_prefix}))"],
            cwd=root, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}))
        alias = next(line.split(" = ", 1)[1] for line in (root / "env/pyvenv.cfg").read_text().splitlines() if line.startswith("home = "))
        manifest = {"format": 1, "root": str(root), "core": {"version": metadata["Version"], "wheel": selected_core.name, "sha256": core_sha256},
                    "dependencies_sha256": sha256(locked), "local_wheels": candidates,
                    "python": str(python.resolve()), "python_sha256": sha256(python.resolve()),
                    "python_base": body["python_base"], "python_alias": str(Path(alias).parent),
                    "installation": body["installation"], "files": snapshot(root),
                    "origin": "new host install from chosen core wheel and hash-required dependency artifacts; never adopt running installation"}
        (root / "approved.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        return {"prepared": True, "root": str(root), "entry": str(root / "launch.py"), "core": manifest["core"], "os_protection": "only processes launched through this entry"}
    except (OSError, ValueError, subprocess.CalledProcessError, zipfile.BadZipFile) as exc:
        raise HarnessError("host_prepare_failed", "宿主安装未获认可；原安装不动，失败目录/日志保留", details={"root": str(root), "cause": str(exc)}) from exc


def update(baseline, directory, wheels):
    root = Path(baseline).resolve(strict=True).parent
    data = verify(root)
    artifact = root / "artifacts"
    replacements = {wheel_metadata(Path(p))[0]: Path(p) for p in wheels}
    for old in data["local_wheels"]:
        replacements.setdefault(old["name"], artifact / old["file"])
    return prepare(directory, artifact / data["core"]["wheel"], data["core"]["sha256"],
                   artifact / "dependencies.lock", wheels=list(replacements.values()),
                   uv=root / "tools/uv", codex=root / "tools/codex" if (root / "tools/codex").exists() else None)
