"""Workspace information is scoped data, separate from control configuration and episodes."""

from __future__ import annotations

import time

from .catalog import relevance
from .contracts import HarnessError, WorkspaceNote, WorkspaceRead


class Workspace:
    def __init__(self, profile, store):
        self.profile, self.store = profile, store

    def brief(self, robot, cameras, skills):
        info = self.profile.workspace
        return {
            "workspace": {
                "id": self.profile.station,
                "title": info.title or self.profile.station,
                "purpose": info.purpose,
                "constraints": info.constraints,
                "source": "host_profile",
            },
            "environment": {
                "qualification": getattr(robot, "qualification", "no_robot"),
                "arms": list(getattr(robot, "arms", ())),
                "cameras": [camera["role"] for camera in cameras.describe()["cameras"]] if cameras else [],
                "frames": list(getattr(robot, "frames", ())),
            },
            "skills": {
                "available": sum(skill["available"] for skill in skills),
                "find": "skills_list",
                "details": "skill_describe",
            },
            "notes": {"count": len(self._active_notes()), "find": "workspace_read(query=...)"},
            "scope": self.store.scope,
            "state": "configuration_only; use robot_observe for current scene",
        }

    def _active_notes(self):
        return [note for note in self.store.all("workspace_note") if not note["archived"]]

    def read(self, args: WorkspaceRead):
        if args.key is not None:
            note = self.store.get("workspace_note", "workspace-note_" + args.key)
            if note["archived"]:
                raise HarnessError("not_found", "Note is archived")
            return note
        matches = []
        for note in self._active_notes():
            score = relevance(args.query or "", " ".join((note["title"], note["text"])))
            if score:
                card = {key: note[key] for key in ("key", "title", "source", "revision")}
                matches.append((score, note["updated_at"], card))
        matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
        end = args.offset + args.limit
        return {
            "notes": [card for _, _, card in matches[args.offset : end]],
            "total": len(matches),
            "next_offset": end if end < len(matches) else None,
        }

    def note(self, args: WorkspaceNote):
        identity = "workspace-note_" + args.key
        try:
            previous = self.store.get("workspace_note", identity)
        except HarnessError as exc:
            if exc.code != "not_found" or args.archive:
                raise
            previous = None
        if args.archive:
            content = {key: previous[key] for key in ("key", "title", "text", "source")}
        else:
            content = args.model_dump(exclude={"archive"})
        if (
            previous
            and previous["archived"] == args.archive
            and all(previous[key] == value for key, value in content.items())
        ):
            return {"key": args.key, "revision": previous["revision"], "changed": False}
        record = {
            **content,
            "revision": previous["revision"] + 1 if previous else 1,
            "updated_at": time.time(),
            "archived": args.archive,
        }
        self.store.put("workspace_note", identity, record)
        return {"key": args.key, "revision": record["revision"], "changed": True}
