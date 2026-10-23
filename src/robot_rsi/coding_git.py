"""Scoped local Git operations; never execute selected project hooks or filters."""
import os
import stat
import subprocess
import tempfile
from pathlib import Path
from .host_boundary import sha256

def git(project, *args, input=None, env=None):
    # No project hooks, filters, external diff, pager or fsmonitor commands on the host.
    environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    environment.update(env or {})
    argv = ["/usr/bin/git", "--no-pager", "--literal-pathspecs", "-C", str(project),
            "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "commit.gpgSign=false"]
    filters = subprocess.run([*argv, "config", "--name-only", "--get-regexp", r"^filter\..*\.(clean|smudge|process|required)$"],
                             capture_output=True, env=environment)
    if filters.returncode not in (0, 1):
        raise ValueError(filters.stderr.decode(errors="replace"))
    for key in filters.stdout.decode().splitlines():
        argv += ["-c", key + ("=false" if key.endswith(".required") else "=")]
    reply = subprocess.run([*argv, *args], input=input,
                           capture_output=True, env=environment)
    if reply.returncode:
        raise ValueError(reply.stderr.decode(errors="replace").strip() or "Git operation failed")
    return reply.stdout.decode()

def local_file(project, name, *, evidence=False):
    path = Path(name)
    if (not name or path.is_absolute() or ".." in path.parts or any(ord(c) < 32 for c in name)
            or any(p in {".git", ".venv", ".env", "auth.json", ".codex", ".ssh"} for p in path.parts)
            or (not evidence and ".rsi" in path.parts)
            or (project / path).resolve() != project / path):
        raise ValueError(f"Not a regular project source/evidence path: {name}")
    target = project / path
    if target.exists() and not target.is_file():
        raise ValueError(f"Select a file, not a directory: {name}")
    return target

def start_git(project):
    try:
        if Path(git(project, "rev-parse", "--show-toplevel").strip()).resolve() != project:
            raise ValueError("Select the repository/worktree root for coding delivery")
        dirty = set()
        for args in (("ls-files", "--modified", "--deleted", "-z"),
                     ("diff", "--cached", "--no-ext-diff", "--no-textconv", "--name-only", "-z"),
                     ("ls-files", "--others", "--exclude-standard", "-z")):
            dirty.update(p for p in git(project, *args).split("\0") if p)
        initial = {}
        for name in dirty:
            try:
                target = local_file(project, name)
                initial[name] = sha256(target) if target.is_file() else None
            except ValueError:
                continue  # excluded state/credentials are not source or delivery inputs
        return {"head": git(project, "rev-parse", "HEAD").strip(),
                "branch": git(project, "symbolic-ref", "--short", "HEAD").strip(),
                "git_dir": git(project, "rev-parse", "--absolute-git-dir").strip(),
                "common_dir": git(project, "rev-parse", "--path-format=absolute", "--git-common-dir").strip(),
                "dirty": sorted(dirty),
                "staged": git(project, "diff", "--cached", "--no-ext-diff", "--no-textconv", "--name-only", "-z").split("\0")[:-1],
                "external_git": (project / ".git").is_file(), "error": None,
                "initial_files": initial}
    except (OSError, ValueError) as exc:
        return {"error": str(exc), "dirty": [], "staged": []}

def same_repository(project, start):
    if (Path(git(project, "rev-parse", "--show-toplevel").strip()).resolve() != project
            or git(project, "rev-parse", "--absolute-git-dir").strip() != start["git_dir"]
            or git(project, "rev-parse", "--path-format=absolute", "--git-common-dir").strip() != start["common_dir"]):
        raise ValueError("Repository/Git metadata changed; do not redirect local host operations")

def host_commit(project, start, handoff):
    """Clean-at-start paths only; never run project commands or alter unrelated index entries."""
    paths = list(dict.fromkeys(handoff.paths))
    same_repository(project, start)
    overlap = set(paths) & set(start["dirty"])
    if overlap:
        raise ValueError("Initial mixed changes need an explicit hunk handoff: " + ", ".join(sorted(overlap)))
    if (git(project, "rev-parse", "HEAD").strip() != start["head"]
            or git(project, "symbolic-ref", "--short", "HEAD").strip() != start["branch"]):
        raise ValueError("Branch/HEAD changed during coding; host commit needs a fresh scoped handoff")
    if git(project, "diff", "--cached", "--no-ext-diff", "--no-textconv", "--name-only", "--", *paths).strip():
        raise ValueError("Selected paths now have staged changes; preserve the index and hand off")
    entries = []
    for name in paths:
        target = local_file(project, name)
        if target.exists():
            blob = git(project, "hash-object", "-w", "--no-filters", "--stdin", input=target.read_bytes()).strip()
            mode = "100755" if target.stat().st_mode & stat.S_IXUSR else "100644"
            entries.append(f"{mode} {blob}\t{name}\n")
        else:
            entries.append(f"0 {'0' * len(start['head'])}\t{name}\n")
    with tempfile.TemporaryDirectory(prefix="robot-rsi-git-") as directory:
        index = {"GIT_INDEX_FILE": str(Path(directory) / "index")}
        git(project, "read-tree", start["head"], env=index)
        git(project, "update-index", "--index-info", input="".join(entries).encode(), env=index)
        tree = git(project, "write-tree", env=index).strip()
        if tree == git(project, "rev-parse", start["head"] + "^{tree}").strip():
            raise ValueError("No selected source changes; no empty host commit")
        sha = git(project, "commit-tree", tree, "-p", start["head"],
                  input=(handoff.commit_message.strip() or handoff.summary).encode()).strip()
        git(project, "update-ref", "HEAD", sha, start["head"])
        # Only our initially clean paths change in the real index; other staging is untouched.
        git(project, "update-index", "--index-info", input="".join(entries).encode())
    return sha

def unreported_source_changes(project, record, handoff):
    """Existing file events, plus Git's changed-path list when a handoff claims no development."""
    start = record["start"]
    names = set(record.get("file_changes", []))
    if (handoff.kind == "runtime" or not handoff.paths) and not start["error"]:
        same_repository(project, start)
        names.update(p for p in git(project, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", start["head"]).split("\0") if p)
        names.update(p for p in git(project, "ls-files", "--others", "--exclude-standard", "-z").split("\0") if p)
    excluded = {str(local_file(project, c.path).relative_to(project)) for c in handoff.unrelated_changes}
    sources = set()
    for name in names:
        try:
            path = Path(name)
            name = str(path.relative_to(project)) if path.is_absolute() else str(path)
            target = local_file(project, name)
        except ValueError:
            continue
        # A light code hint, not an ownership claim or a scan of generated results/state.
        if (target.suffix not in {".py", ".pyi", ".js", ".ts", ".jsx", ".tsx", ".c", ".cc", ".cpp", ".h", ".hpp", ".rs", ".go", ".java", ".sh", ".toml"}
                and target.name not in {"Makefile", "CMakeLists.txt", "package.json"}):
            continue
        if name in excluded or (handoff.kind != "runtime" and name in handoff.paths):
            continue
        initial = start.get("initial_files", {})
        if name in initial:
            changed = (sha256(target) if target.is_file() else None) != initial[name]
        elif not start["error"]:
            entry = git(project, "ls-tree", "--format=%(objectmode) %(objectname)", start["head"], "--", name).strip()
            if target.is_file():
                blob = git(project, "hash-object", "--no-filters", "--stdin", input=target.read_bytes()).strip()
                mode = "100755" if target.stat().st_mode & stat.S_IXUSR else "100644"
                changed = entry != mode + " " + blob
            else:
                changed = bool(entry)
        else:
            changed = True  # Actual file event, but no usable repository baseline.
        if changed:
            sources.add(name)
    return sorted(sources)
