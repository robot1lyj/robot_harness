#!/usr/bin/python3 -I
"""Standalone host launcher: standard library only, before any selected core import."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def snapshot(directory):
    """Full file/symlink identity, including bytecode; never adopt a running install."""
    result = {}
    for path in sorted(Path(directory).rglob("*")):
        name = str(path.relative_to(directory))
        if name == "approved.json":
            continue
        mode = stat.S_IMODE(path.lstat().st_mode)
        if path.is_symlink():
            result[name] = {"link": os.readlink(path), "mode": mode}
        elif path.is_file():
            result[name] = {"sha256": sha256(path), "mode": mode}
        elif path.is_dir():
            result[name] = {"directory": True, "mode": mode}
        else:
            raise ValueError(f"Unsupported installation entry: {name}")
    return result


def load(root):
    root = Path(root).resolve(strict=True)
    path = root / "approved.json"
    if path.is_symlink():
        raise ValueError("Host baseline must be an ordinary file")
    data = json.loads(path.read_text())
    if data["root"] != str(root) or data["format"] != 1:
        raise ValueError("Baseline belongs to another host installation")
    return data


def verify(root):
    data = load(root)
    actual = snapshot(root)
    if actual != data["files"]:
        changed = sorted(k for k in set(actual) | set(data["files"])
                         if actual.get(k) != data["files"].get(k))
        raise ValueError("Installation differs from approved artifacts: " + ", ".join(changed[:8]))
    if sha256(data["python"]) != data["python_sha256"]:
        raise ValueError("Selected Python executable changed")
    return data


def mount_readonly(path):
    """Observe the actual mount namespace, not an environment-variable claim."""
    path = Path(path).resolve()
    matches = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        columns = line.split()
        mount = Path(columns[4].replace("\\040", " ").replace("\\134", "\\"))
        if path == mount or mount in path.parents:
            matches.append((len(mount.parts), "ro" in columns[5].split(",")))
    return max(matches, default=(0, False))[1]


def protection():
    baseline = os.environ.get("ROBOT_RSI_HOST_BASELINE")
    if not baseline:
        return {"protected": False, "reason": "not launched by the controlled host entry"}
    try:
        root = Path(baseline).resolve(strict=True).parent
        prefix = Path(sys.prefix).resolve(strict=True)
        installed = root / "env"
        module = Path(__file__).resolve(strict=True)
        # A bind-mounted project/.venv retains a different pathname, but the same
        # installation inode. Also prove this module belongs to that environment;
        # an ambient editable checkout must never inherit a baseline claim.
        if not prefix.samefile(installed) or not any(module.is_relative_to(base) for base in (prefix, installed)):
            return {"protected": False, "reason": "baseline does not belong to this selected installation"}
        relative = module.relative_to(prefix if module.is_relative_to(prefix) else installed)
        if not module.samefile(installed / relative):
            return {"protected": False, "reason": "module does not belong to selected installation"}
        data = load(root)
        protected = (mount_readonly(root) and mount_readonly(installed)
                     and mount_readonly(prefix) and mount_readonly(module))
        return {"protected": protected, "baseline": str(root / "approved.json"),
                "core": data["core"], "installation": data["installation"]["digest"],
                "scope": "this controlled process and descendants; excludes host/desktop",
                "reason": "observed read-only mounts; startup artifact check" if protected
                else "required read-only mounts are absent"}
    except (OSError, ValueError, KeyError):
        return {"protected": False, "reason": "host baseline/mounts unavailable"}


def command(root, project, mode, arguments, *, reads=(), auth=None, network=False):
    root, project = Path(root).resolve(strict=True), Path(project).resolve(strict=True)
    data = load(root)
    if not project.is_dir() or project == Path("/") or project.is_relative_to(root) or root.is_relative_to(project):
        raise ValueError("Writable project and protected host installation must be disjoint")
    alias = project / ".venv"
    if alias.is_symlink() or (alias.exists() and not alias.is_dir()):
        raise ValueError("Project .venv mount point must be an ordinary directory")
    alias.mkdir(exist_ok=True)
    home = project / ".rsi/host-home"
    if any(p.is_symlink() for p in (project / ".rsi", home, home / "codex")):
        raise ValueError("Host state paths cannot be symlinks")
    (home / "codex").mkdir(parents=True, exist_ok=True)
    read_paths = [Path(p).resolve(strict=True) for p in reads]
    for path in read_paths:
        if any(path == p or path in p.parents or p in path.parents for p in (project, root)):
            raise ValueError("Read scopes must not overlap the project or host installation")
    protected = list(dict.fromkeys([root, Path(data["python_base"]), Path(data["python_alias"]), *read_paths]))
    # /tmp is writable scratch. Its synthetic ancestors must be separate RO mounts,
    # otherwise a child could rename a parent and substitute the old pathname.
    guards = sorted({Path("/tmp") / p.parts[2] for p in protected
                     if p.is_relative_to("/tmp") and len(p.parts) > 2
                     and (len(p.parts) > 3 or p.is_dir())})
    options = ["/usr/bin/bwrap", "--unshare-user", "--unshare-pid", "--unshare-ipc",
               "--unshare-uts", "--die-with-parent", "--new-session", "--cap-drop", "ALL",
               "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
               "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
               "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--clearenv"]
    if not network:
        options += ["--unshare-net"]
    for path in guards:
        options += ["--tmpfs", str(path)]
    for name in ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/passwd",
                 "/etc/group", "/etc/ssl", "/etc/ld.so.cache"):
        if Path(name).exists():
            options += ["--ro-bind", name, name]
    for path in read_paths:
        options += ["--ro-bind", str(path), str(path)]
    python_base, python_alias = Path(data["python_base"]), Path(data["python_alias"])
    for path in dict.fromkeys([root, python_base]):
        options += ["--ro-bind", str(path), str(path)]
    if python_alias != python_base:
        # Resolve uv's stable alias to the already-selected runtime, never its new host target.
        options += ["--ro-bind", str(python_base), str(python_alias)]
    options += ["--bind", str(project), str(project),
                "--ro-bind", str(root / "env"), str(alias),
                "--bind", str(home), "/run/robot-rsi-home"]
    if auth:
        options += ["--ro-bind", str(Path(auth).resolve(strict=True)), "/run/robot-rsi-home/codex/auth.json"]
    for key, value in {
        "PATH": str(root / "tools") + ":" + str(root / "env/bin") + ":/usr/bin:/bin",
        "HOME": "/run/robot-rsi-home", "CODEX_HOME": "/run/robot-rsi-home/codex", "LANG": "C.UTF-8",
        "VIRTUAL_ENV": str(root / "env"), "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1", "PYTHONSAFEPATH": "1",
        "UV_CACHE_DIR": str(project / ".rsi/uv-cache"), "UV_OFFLINE": "true",
        "ROBOT_RSI_HOST_BASELINE": str(root / "approved.json"),
    }.items():
        options += ["--setenv", key, value]
    for path in guards:
        options += ["--remount-ro", str(path)]
    options += ["--remount-ro", "/proc", "--remount-ro", "/", "--chdir", str(project),
                "--", "/usr/bin/python3", "-I", str(root / "launch.py"), "_inside",
                "--project", str(project), "--mode", mode, "--", *arguments]
    return options


def main():
    parser = argparse.ArgumentParser(description="固定安装的 Linux 受控入口")
    parser.add_argument("action", choices=["verify", "run", "coding", "exec", "_inside"])
    parser.add_argument("--project", type=Path)
    parser.add_argument("--read", type=Path, action="append", default=[])
    parser.add_argument("--auth", type=Path)
    parser.add_argument("--network", action="store_true")
    parser.add_argument("--resume-session")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--no-commit", action="store_true")
    parser.add_argument("--mode", choices=["run", "coding", "exec"])
    args, arguments = parser.parse_known_args()
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    root = Path(__file__).resolve().parent
    try:
        if args.action == "verify":
            data = verify(root)
            print(json.dumps({"verified": True, "core": data["core"],
                              "os_protection": False, "entry": str(root / "launch.py")}))
            return
        if args.action == "_inside":
            if not mount_readonly(root) or not mount_readonly(root / "env"):
                raise ValueError("Controlled mount namespace required; no full-access fallback")
            verify(root)  # once, inside read-only mounts, before selected Python/core/client execution
            if args.mode == "run":
                argv = [str(root / "env/bin/python"), "-I", "-B", "-m", "robot_rsi.cli", *arguments]
            elif args.mode == "coding":
                argv = [str(root / "tools/codex"), "exec", *arguments]
            else:
                argv = arguments
            if not argv:
                raise ValueError("A controlled command is required")
            os.execvpe(argv[0], argv, os.environ)
        if args.project is None:
            raise ValueError("Select the writable project")
        if args.action == "coding" and not any(p in arguments for p in ("--help", "-h", "--version", "-V")):
            verify(root)  # trusted selected host code before importing the local exit seam
            argv = [str(root / "env/bin/python"), "-I", "-B", "-m", "robot_rsi.coding_host",
                    "--host", str(root), "--project", str(args.project)]
            for path in args.read:
                argv += ["--read", str(path)]
            if args.auth:
                argv += ["--auth", str(args.auth)]
            for name in ("network", "status", "no_commit"):
                if getattr(args, name):
                    argv.append("--" + name.replace("_", "-"))
            if args.resume_session:
                argv += ["--resume-session", args.resume_session]
            os.execvpe(argv[0], [*argv, "--", *arguments], os.environ)
        argv = command(root, args.project, args.action, arguments,
                       reads=args.read, auth=args.auth, network=args.network)
        raise SystemExit(subprocess.run(argv, close_fds=True).returncode)
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": {"code": "host_boundary_failed", "message": str(exc)}}), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
