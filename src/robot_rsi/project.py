"""Host-selected project materials; purpose narrows existing scope, never grants a role."""
from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import Field

from .catalog import relevance
from .contracts import Contract, HarnessError, Identifier, Profile

PURPOSE_ROLES = {"onboarding": "development", "improve": "development",
                 "run": "runtime", "evaluate": "evaluation"}


class ProjectRead(Contract):
    key: Identifier | None = None
    query: str = Field(default="", max_length=200)
    limit: int = Field(default=3, ge=1, le=3)


class ProjectFiles(Contract):
    path: str = Field(default=".", min_length=1, max_length=240)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=6000, ge=1, le=16000)


EXCLUDED = {".git", ".codex", ".rsi", ".venv", "venv", "node_modules", "__pycache__", ".ssh", ".env", "auth.json"}


def source_profile(root, *, state_root=None):
    """Minimal host-selected scope; no adapter, fabricated resources or project edits."""
    import os

    try:
        path = Path(root).expanduser().resolve(strict=True)
    except OSError as exc:
        raise HarnessError("project_not_found", "Provide an existing project directory") from exc
    if not path.is_dir():
        raise HarnessError("invalid_project", "Project root must be a directory")
    identity = "project_" + hashlib.sha256(str(path).encode()).hexdigest()[:16]
    return Profile(role="development", station="source-project", namespace=identity,
                   state_root=state_root or os.environ.get("ROBOT_RSI_STATE_ROOT", Profile().state_root),
                   robot=None, host={"cameras": None, "depth": None},
                   project={"id": identity, "root": str(path), "purpose": "onboarding"},
                   workspace={"title": path.name, "purpose": "Read-only project inspection"},
                   task_limits={"duration_s": 600, "tool_calls": 80})


class Project:
    def __init__(self, profile):
        self.config = profile.project
        self.common = None
        self.materials = {}
        if self.config is None:
            return
        config = self.config
        if PURPOSE_ROLES[config.purpose] != profile.role:
            raise HarnessError("purpose_mismatch", "Project purpose must use its existing role")
        try:
            self.root = Path(config.root).expanduser().resolve(strict=True)
        except OSError as exc:
            raise HarnessError("project_not_found", "Provide an existing project directory") from exc
        if not self.root.is_dir():
            raise HarnessError("invalid_project", "Project root must be a directory")
        if len({m.key for m in config.materials}) != len(config.materials):
            raise HarnessError("invalid_project", "Material keys must be unique")
        self.materials = {m.key: m for m in config.materials if config.purpose in m.purposes}
        if config.common_key is not None and config.common_key not in self.materials:
            raise HarnessError("invalid_project", "Common facts must be visible for this purpose")
        if config.common_key is not None:
            self.common = self.read(ProjectRead(key=config.common_key))

    @property
    def can_read_sources(self):
        return self.config is not None and self.config.purpose in ("onboarding", "improve")

    def files(self, args):
        if not self.can_read_sources:
            raise HarnessError("source_access_denied", "Source access requires host-selected onboarding/improve scope")
        relative = Path(args.path)
        path = self.root / relative
        if (relative.is_absolute() or ".." in relative.parts or any(p in EXCLUDED for p in relative.parts)
                or path.resolve() != path):
            raise HarnessError("invalid_project_path", "Use a regular path inside the selected project; no symlinks or excluded state")
        try:
            source = str(relative)
            if path.is_dir():
                entries = sorted((p for p in path.iterdir() if p.name not in EXCLUDED and not p.is_symlink()), key=lambda p:p.name)
                end = args.offset + min(args.limit, 50)
                return {"path": source, "source": "host_selected_project", "unit": "entries",
                        "entries": [{"path": str(p.relative_to(self.root)), "kind": "directory" if p.is_dir() else "file"} for p in entries[args.offset:end]],
                        "total": len(entries), "next_offset": end if end < len(entries) else None}
            if not path.is_file():
                raise HarnessError("not_found", "Project file/directory does not exist")
            if path.stat().st_size > 1024 * 1024:
                raise HarnessError("project_file_too_large", "Select a text source file of at most 1 MiB")
            text = path.read_text(encoding="utf-8")
            if "\x00" in text:
                raise HarnessError("not_text", "Select a UTF-8 text source")
            end = args.offset + args.limit
            return {"path": source, "source": "host_selected_project", "unit": "characters",
                    "sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "offset": args.offset, "line_start": text[:args.offset].count("\n") + 1,
                    "text": text[args.offset:end], "total": len(text),
                    "next_offset": end if end < len(text) else None}
        except UnicodeError as exc:
            raise HarnessError("not_text", "Select a UTF-8 text source") from exc
        except OSError as exc:
            raise HarnessError("project_read_failed", "Project source could not be read") from exc

    def read(self, args):
        if self.config is None:
            raise HarnessError("project_required", "Host has not selected a project")
        if args.key is not None:
            material = self.materials.get(args.key)
            if material is None:
                raise HarnessError("not_found", "No material visible for this purpose")
            relative = Path(material.path)
            path = self.root / relative
            if relative.is_absolute() or ".." in relative.parts or path.resolve() != path or not path.is_file():
                raise HarnessError("invalid_material", "Material must be a regular relative file without symlinks")
            body = path.read_text()
            return {"key": material.key, "title": material.title, "text": body,
                    "sha256": hashlib.sha256(body.encode()).hexdigest(), "source": material.path}
        cards = [(relevance(args.query, m.title + " " + m.key), m) for m in self.materials.values()
                 if m.key != self.config.common_key]
        cards = sorted(((score, m) for score, m in cards if score or not args.query),
                       key=lambda pair: (-pair[0], pair[1].key))
        return {"materials": [{"key": m.key, "title": m.title} for _, m in cards[:args.limit]],
                "visible_count": len(self.materials), "details": "project_read(key=...)"}

    def brief(self, installation_digest, skills):
        if self.config is None:
            return None
        return {"id": self.config.id, "purpose": self.config.purpose,
                "root": str(self.root), "source_access": "project_files" if self.can_read_sources else None,
                "common": self.common, "installation_digest": installation_digest,
                "skill_versions": skills, **self.read(ProjectRead()),
                "next": ["binding_read", "skills_list", "issue_get"]}
