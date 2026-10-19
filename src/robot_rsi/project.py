"""Host-selected project materials; purpose narrows existing scope, never grants a role."""
from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import Field

from .catalog import relevance
from .contracts import Contract, HarnessError, Identifier

PURPOSE_ROLES = {"onboarding": "development", "improve": "development",
                 "run": "runtime", "evaluate": "evaluation"}


class ProjectRead(Contract):
    key: Identifier | None = None
    query: str = Field(default="", max_length=200)
    limit: int = Field(default=3, ge=1, le=3)


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
        self.root = Path(config.root).expanduser().resolve(strict=True)
        if len({m.key for m in config.materials}) != len(config.materials):
            raise HarnessError("invalid_project", "Material keys must be unique")
        self.materials = {m.key: m for m in config.materials if config.purpose in m.purposes}
        if config.common_key not in self.materials:
            raise HarnessError("invalid_project", "Common facts must be visible for this purpose")
        self.common = self.read(ProjectRead(key=config.common_key))

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
                "common": self.common, "installation_digest": installation_digest,
                "skill_versions": skills, **self.read(ProjectRead()),
                "next": ["binding_read", "skills_list", "issue_get"]}
