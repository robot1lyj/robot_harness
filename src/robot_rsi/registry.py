"""Host-managed immutable skill pool; scoped bindings and incremental SQLite metadata."""

from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.metadata
import importlib.util
import inspect
import json
import shutil
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .catalog import relevance
from .contracts import Contract, HarnessError, Identifier
from .storage import canonical

BUILTINS = ("pick_place", "workspace-context", "task-memory")


class Manifest(Contract):
    name: Identifier
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    kind: Literal["code", "agent_workflow"]
    category: str
    description: str = Field(min_length=1, max_length=1000)
    qualification: str
    resources: list[Literal["manipulator"]] = Field(default_factory=list)
    requires: dict[str, int] = Field(default_factory=dict)
    keywords: list[str] = Field(default_factory=list)
    input_model: str
    output_model: str
    entry: str
    failure: str | None = None


def symbol(reference, directory, digest):
    module, attribute = reference.split(":")
    if module == "skill":
        name = "_rsi_skill_" + digest
        if name not in sys.modules:
            spec = importlib.util.spec_from_file_location(name, directory / "skill.py")
            loaded = importlib.util.module_from_spec(spec)
            sys.modules[name] = loaded
            try:
                spec.loader.exec_module(loaded)
            except BaseException:
                sys.modules.pop(name, None)
                raise
        return getattr(sys.modules[name], attribute)
    # Installed trusted packages may supply standard Python module references.
    return getattr(importlib.import_module(module), attribute)


class Registry:
    def __init__(self, profile, bindings=None, robot=None):
        self.scope = {k: getattr(profile, k) for k in ("role", "station", "namespace", "robot")}
        self.workspace = hashlib.sha256(canonical(self.scope).encode()).hexdigest()
        self.root = Path(profile.state_root).expanduser().resolve() / profile.role / "skill-pool"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.root / "index.sqlite3", timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS skills (
                name TEXT, version TEXT, digest TEXT, card TEXT, body TEXT,
                directory TEXT, deleted INTEGER DEFAULT 0,
                PRIMARY KEY(name, version));
            CREATE TABLE IF NOT EXISTS bindings (
                workspace TEXT, name TEXT, version TEXT, enabled INTEGER,
                PRIMARY KEY(workspace, name));
            CREATE TABLE IF NOT EXISTS workspaces (workspace TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS pins (
                workspace TEXT, task TEXT, name TEXT, version TEXT,
                PRIMARY KEY(workspace, task, name));
            CREATE TABLE IF NOT EXISTS changes (
                seq INTEGER PRIMARY KEY, name TEXT, version TEXT, action TEXT);
        """)
        self.db.create_function(
            "relevance", 3, lambda q, text, aliases: relevance(q, text, json.loads(aliases))
        )
        self.bindings, self.robot = bindings, robot
        self._details, self._entries, self._queries = {}, {}, {}
        self._seen = 0
        self.counters = dict(
            instruction_reads=0,
            schema_exports=0,
            entry_loads=0,
            directory_scans=0,
            detail_loads=0,
            index_updates=0,
            query_hits=0,
            query_builds=0,
        )
        # Fixed package resources, only at pool creation; never recursive runtime discovery.
        if not self.db.execute("SELECT 1 FROM skills LIMIT 1").fetchone():
            for folder in BUILTINS:
                self.register(files("robot_rsi").joinpath("resources", folder), builtin=True)
        if not self.db.execute(
            "SELECT 1 FROM workspaces WHERE workspace=?", (self.workspace,)
        ).fetchone():
            with self.db:
                for folder in BUILTINS:
                    data = json.loads(
                        files("robot_rsi").joinpath("resources", folder, "skill.json").read_text()
                    )
                    current = self.db.execute(
                        "SELECT version FROM skills WHERE name=? AND version=? AND deleted=0",
                        (data["name"], data["version"]),
                    ).fetchone()
                    if not current:
                        current = self.db.execute(
                            "SELECT version FROM skills WHERE name=? AND deleted=0 ORDER BY rowid DESC LIMIT 1",
                            (data["name"],),
                        ).fetchone()
                    if current:
                        self.db.execute(
                            "INSERT INTO bindings VALUES(?,?,?,1)",
                            (self.workspace, data["name"], current[0]),
                        )
                self.db.execute("INSERT INTO workspaces VALUES(?)", (self.workspace,))

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def close(self):
        self.db.close()

    def register(self, directory, *, builtin=False):
        try:
            return self._register(directory, builtin=builtin)
        except (ValueError, OSError, ImportError, AttributeError, TypeError) as exc:
            raise HarnessError(
                "invalid_skill",
                "Candidate package failed validation",
                details={"type": type(exc).__name__},
            ) from exc

    def _register(self, directory, *, builtin=False):
        directory = Path(str(directory)).resolve()
        manifest = Manifest.model_validate_json((directory / "skill.json").read_text())
        instructions = (directory / "SKILL.md").read_text()
        self.counters["instruction_reads"] += 1
        content = {"skill.json": manifest.model_dump(), "SKILL.md": instructions}
        if (directory / "skill.py").exists():
            content["skill.py"] = (directory / "skill.py").read_text()
        digest = hashlib.sha256(canonical(content).encode()).hexdigest()
        prior = self.db.execute(
            "SELECT * FROM skills WHERE name=? AND version=?", (manifest.name, manifest.version)
        ).fetchone()
        if prior:
            if prior["digest"] != digest or prior["deleted"]:
                raise HarnessError("skill_version_conflict", "Publish a new immutable version")
            return dict(name=manifest.name, version=manifest.version, digest=digest, changed=False)
        # Validate the full candidate before publishing any index/binding change.
        input_model = symbol(manifest.input_model, directory, digest)
        output_model = symbol(manifest.output_model, directory, digest)
        for model in (input_model, output_model):
            if not isinstance(model, type) or not issubclass(model, BaseModel):
                raise HarnessError("invalid_skill", "Contract reference must be a Pydantic model")
        entry = symbol(manifest.entry, directory, digest) if manifest.kind == "code" else None
        if manifest.kind == "code" and not inspect.iscoroutinefunction(entry):
            raise HarnessError("invalid_skill", "Code entry must be an async function")
        if manifest.kind == "agent_workflow":
            from .runtime import TOOLS

            if manifest.entry not in TOOLS:
                raise HarnessError("invalid_skill", "Workflow entry must use an existing tool")
        schema, output_schema = input_model.model_json_schema(), output_model.model_json_schema()
        self.counters["schema_exports"] += 2
        target = directory
        if not builtin:
            target = self.root / "packages" / digest
            target.parent.mkdir(exist_ok=True)
            staging = Path(tempfile.mkdtemp(dir=target.parent))
            try:
                # Only declared execution content; no recursive copies or workspace data.
                for name, value in content.items():
                    (staging / name).write_text(canonical(value) if name == "skill.json" else value)
                if not target.exists():
                    staging.rename(target)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        data = manifest.model_dump()
        data["entrypoint"] = (
            {"tool": "task_run", "arguments": {"skill": manifest.name, "parameters": {}}}
            if manifest.kind == "code"
            else {"tool": manifest.entry, "arguments": {}}
        )
        body = {
            **data,
            "input_schema": schema,
            "output_schema": output_schema,
            "instructions": instructions,
            "digest": digest,
        }
        card = {
            k: data[k]
            for k in (
                "name",
                "version",
                "kind",
                "category",
                "description",
                "requires",
                "keywords",
                "entrypoint",
            )
        }
        with self.db:
            self.db.execute(
                "INSERT INTO skills VALUES(?,?,?,?,?,?,0)",
                (
                    manifest.name,
                    manifest.version,
                    digest,
                    canonical(card),
                    canonical(body),
                    str(target),
                ),
            )
            self.db.execute(
                "INSERT INTO changes(name,version,action) VALUES(?,?,?)",
                (manifest.name, manifest.version, "registered"),
            )
        if "skill.py" in content:
            sys.modules.pop("_rsi_skill_" + digest, None)
        self.counters["index_updates"] += 1
        return dict(name=manifest.name, version=manifest.version, digest=digest, changed=True)

    def discover(self):
        """Explicit host management operation, never called by a tool request."""
        results = []
        for ep in importlib.metadata.entry_points(group="robot_rsi.skills"):
            packages = ep.load()()
            if not isinstance(packages, (list, tuple)):
                packages = [packages]
            results.extend(
                self.register(path, builtin=ep.dist.name == "robot-rsi-harness")
                for path in packages
            )
        return results

    def enable(self, name, version):
        with self.transaction():
            row = self._row(name, version)
            # Code update activation waits for tasks in every workspace using an old version.
            if (
                json.loads(row["card"])["kind"] == "code"
                and self.db.execute(
                    "SELECT 1 FROM pins WHERE name=? AND version<>? LIMIT 1", (name, version)
                ).fetchone()
            ):
                raise HarnessError("task_boundary_required", "Finish or archive pinned tasks first")
            self.db.execute(
                "INSERT INTO changes(name,version,action) VALUES(?,?,?)",
                (name, version, "binding:" + self.workspace),
            )
            self.db.execute(
                "INSERT INTO bindings VALUES(?,?,?,1) ON CONFLICT(workspace,name) "
                "DO UPDATE SET version=excluded.version,enabled=1",
                (self.workspace, name, version),
            )
            return {
                "name": name,
                "version": version,
                "enabled": True,
                "activation": "task_boundary; restart for changed installed modules/dependencies",
            }

    def disable(self, name):
        with self.db:
            self.db.execute(
                "INSERT INTO changes(name,version,action) VALUES(?,?,?)",
                (name, "", "binding:" + self.workspace),
            )
            self.db.execute(
                "UPDATE bindings SET enabled=0 WHERE workspace=? AND name=?", (self.workspace, name)
            )
        return {"name": name, "enabled": False}

    def delete(self, name, version):
        with self.transaction():
            row = self._row(name, version)
            if (
                self.db.execute(
                    "SELECT 1 FROM bindings WHERE name=? AND version=?", (name, version)
                ).fetchone()
                or self.db.execute(
                    "SELECT 1 FROM pins WHERE name=? AND version=?", (name, version)
                ).fetchone()
            ):
                raise HarnessError(
                    "skill_in_use", "Workspace or active task still references this version"
                )
            directory = Path(row["directory"])
            if directory.is_relative_to(self.root / "packages"):
                shutil.rmtree(directory)
            self.db.execute(
                "UPDATE skills SET deleted=1 WHERE name=? AND version=?", (name, version)
            )
            self.db.execute(
                "INSERT INTO changes(name,version,action) VALUES(?,?,?)",
                (name, version, "deleted; contract snapshot retained"),
            )
            self._details.pop((name, version), None)
            self._entries.pop((name, version), None)
            return {"name": name, "version": version, "deleted": True, "history_retained": True}

    def unbind(self, name):
        with self.db:
            self.db.execute(
                "INSERT INTO changes(name,version,action) VALUES(?,?,?)",
                (name, "", "binding:" + self.workspace),
            )
            self.db.execute(
                "DELETE FROM bindings WHERE workspace=? AND name=?", (self.workspace, name)
            )

    def _row(self, name, version=None, *, history=False):
        if version is None:
            bound = self.db.execute(
                "SELECT version FROM bindings WHERE workspace=? AND name=? AND enabled=1",
                (self.workspace, name),
            ).fetchone()
            if not bound:
                raise HarnessError("unknown_skill", "Skill is not enabled in this workspace")
            version = bound[0]
        row = self.db.execute(
            "SELECT * FROM skills WHERE name=? AND version=?", (name, version)
        ).fetchone()
        if row is None or (row["deleted"] and not history):
            raise HarnessError("unknown_skill", "No registered executable version")
        return row

    def versions(self):
        return {
            row["name"]: row["version"]
            for row in self.db.execute(
                "SELECT b.name,b.version FROM bindings b JOIN skills s USING(name,version) "
                "WHERE b.workspace=? AND b.enabled=1 AND s.deleted=0",
                (self.workspace,),
            )
        }

    def pin(self, task, name, version, *, inherited_task=None, restore=False):
        with self.transaction():
            self._row(name, version)
            prior = self.db.execute(
                "SELECT version FROM pins WHERE workspace=? AND task=? AND name=?",
                (self.workspace, task, name),
            ).fetchone()
            if prior and prior[0] != version:
                raise HarnessError("skill_version_conflict", "Task already pins another version")
            inherited = (
                self.db.execute(
                    "SELECT version FROM pins WHERE workspace=? AND task=? AND name=?",
                    (self.workspace, inherited_task, name),
                ).fetchone()
                if inherited_task
                else None
            )
            current = self.db.execute(
                "SELECT version FROM bindings WHERE workspace=? AND name=? AND enabled=1",
                (self.workspace, name),
            ).fetchone()
            if (
                not restore
                and not prior
                and not (inherited and inherited[0] == version)
                and not (current and current[0] == version)
            ):
                raise HarnessError(
                    "skill_version_conflict", "Workspace selection changed before admission"
                )
            self.db.execute(
                "INSERT OR IGNORE INTO pins VALUES(?,?,?,?)",
                (self.workspace, task, name, version),
            )

    def release(self, task):
        with self.db:
            self.db.execute("DELETE FROM pins WHERE workspace=? AND task=?", (self.workspace, task))

    def missing(self, skill, *, bindings=None):
        bindings = self.bindings if bindings is None else bindings
        missing = bindings.missing(skill["requires"]) if bindings else []
        if (
            bindings
            and skill["kind"] == "code"
            and "manipulator" in skill["resources"]
            and skill["requires"].get("scene.observe") != 1
        ):
            missing.extend(
                item for item in bindings.missing({"scene.observe": 1}) if item not in missing
            )
        return missing

    def describe(self, name, version=None, *, history=False, bindings=None):
        row = self._row(name, version, history=history)
        key = (name, row["version"])
        if key not in self._details:
            self._details[key] = json.loads(row["body"])
            self.counters["detail_loads"] += 1
        body = self._details[key]
        missing = self.missing(body, bindings=bindings)
        return {
            **body,
            "available": not row["deleted"]
            and not missing
            and (
                body["kind"] == "agent_workflow"
                or not body["resources"]
                or self.robot is None
                or self.robot.supports_motion
            ),
            "missing_capabilities": missing,
        }

    def resolve(self, name, task=None, *, bindings=None):
        version = None
        if task:
            version = self.db.execute(
                "SELECT version FROM pins WHERE workspace=? AND task=? AND name=?",
                (self.workspace, task, name),
            ).fetchone()
        result = self.describe(name, version[0] if version else None, bindings=bindings)
        if not result["available"]:
            raise HarnessError("unsupported", "Skill capabilities are unavailable")
        return result

    def load(self, name, version, *, bindings=None):
        row = self._row(name, version)
        key = (name, version)
        if key not in self._entries:
            body = self.describe(name, version, bindings=bindings)
            directory = Path(row["directory"])
            self._entries[key] = (
                symbol(body["input_model"], directory, body["digest"]),
                symbol(body["output_model"], directory, body["digest"]),
                symbol(body["entry"], directory, body["digest"])
                if body["kind"] == "code"
                else None,
            )
            self.counters["entry_loads"] += 1
        return self._entries[key]

    def _sync_queries(self):
        changes = self.db.execute(
            "SELECT * FROM changes WHERE seq>? ORDER BY seq", (self._seen,)
        ).fetchall()
        for change in changes:
            self._seen = change["seq"]
            if (
                change["action"].startswith("binding:")
                and change["action"] != "binding:" + self.workspace
            ):
                continue
            candidates = self.db.execute(
                "SELECT card FROM skills WHERE name=?", (change["name"],)
            ).fetchall()
            for key, result in list(self._queries.items()):
                query = key[0]
                if (
                    not query
                    or any(c["name"] == change["name"] for c in result["skills"])
                    or any(
                        relevance(
                            query,
                            " ".join((d["name"], d["description"], d["category"])),
                            d["keywords"],
                        )
                        for d in (json.loads(row[0]) for row in candidates)
                    )
                ):
                    self._queries.pop(key, None)

    def cards(self, args, *, bindings=None):
        self._sync_queries()
        bindings = self.bindings if bindings is None else bindings
        catalog = bindings.catalog() if bindings else []
        provided = {c["name"]: c["contract_version"] for c in catalog}
        if bindings:
            provided = {k: v for k, v in provided.items() if not bindings.missing({k: v})}
        motion = self.robot is None or self.robot.supports_motion
        # Instruction/schema caches remain versioned metadata; availability is request-local.
        key = (args.query, args.available_only, args.limit, args.offset,
               canonical([catalog, provided, motion]))
        if key in self._queries:
            self.counters["query_hits"] += 1
            return copy.deepcopy(self._queries[key])
        self.counters["query_builds"] += 1
        # SQL projects bounded metadata, without reading instruction/schema bodies.
        rows = self.db.execute(
            """
            SELECT s.card,json_extract(body,'$.resources') AS resources,count(*) OVER() AS total,
              relevance(?, json_extract(card,'$.name')||' '||json_extract(card,'$.description')||' '||
                        json_extract(card,'$.category'),json_extract(card,'$.keywords')) AS score,
              NOT EXISTS(SELECT 1 FROM json_each(json_extract(card,'$.requires')) r
                         WHERE coalesce(json_extract(?, '$.'||'"'||r.key||'"'),0) != r.value)
              AND (? OR json_extract(card,'$.kind')='agent_workflow'
                   OR json_array_length(json_extract(body,'$.resources'))=0)
              AND (json_extract(card,'$.kind')='agent_workflow'
                   OR json_array_length(json_extract(body,'$.resources'))=0
                   OR coalesce(json_extract(?, '$."scene.observe"'),0)=1) AS available
            FROM bindings b JOIN skills s USING(name,version)
            WHERE b.workspace=? AND b.enabled=1 AND s.deleted=0 AND score>0
              AND (?=0 OR available=1) ORDER BY score DESC,s.rowid LIMIT ? OFFSET ?
        """,
            (
                args.query,
                canonical(provided),
                motion,
                canonical(provided),
                self.workspace,
                args.available_only,
                args.limit + 1,
                args.offset,
            ),
        ).fetchall()
        cards = []
        for row in rows[: args.limit]:
            data = json.loads(row["card"])
            data["resources"] = json.loads(row["resources"])
            card = {k: data[k] for k in ("name", "version", "kind", "description", "entrypoint")}
            card["available"] = bool(row["available"])
            if not card["available"]:
                card["missing_capabilities"] = self.missing(data, bindings=bindings)
            cards.append(card)
        result = {
            "skills": cards,
            "total": rows[0]["total"] if rows else 0,
            "next_offset": args.offset + args.limit if len(rows) > args.limit else None,
        }
        if len(self._queries) >= 256:
            self._queries.pop(next(iter(self._queries)))
        self._queries[key] = result
        return copy.deepcopy(result)

    def descriptors(self, *, bindings=None):
        results = []
        for row in self.db.execute(
            "SELECT card,json_extract(body,'$.resources') AS resources "
            "FROM bindings b JOIN skills s USING(name,version) "
            "WHERE b.workspace=? AND b.enabled=1 AND s.deleted=0",
            (self.workspace,),
        ):
            card = json.loads(row[0])
            card["resources"] = json.loads(row["resources"])
            missing = self.missing(card, bindings=bindings)
            results.append(
                {
                    **card,
                    "missing_capabilities": missing,
                    "available": not missing
                    and (
                        card["kind"] == "agent_workflow"
                        or not card["resources"]
                        or self.robot is None
                        or self.robot.supports_motion
                    ),
                }
            )
        return results

    def seed_fixtures(self, fixtures):
        """Management-only metadata fixture, explicitly not a robot capability."""
        self._queries.clear()
        base = self._row("workspace_context", self.resolve("workspace_context")["version"])
        with self.db:
            for item in fixtures:
                body = {**json.loads(base["body"]), **item}
                card = {
                    k: body[k]
                    for k in (
                        "name",
                        "version",
                        "kind",
                        "category",
                        "description",
                        "requires",
                        "keywords",
                        "entrypoint",
                    )
                }
                self.db.execute(
                    "INSERT OR IGNORE INTO skills VALUES(?,?,?,?,?,?,0)",
                    (
                        body["name"],
                        body["version"],
                        base["digest"],
                        canonical(card),
                        canonical(body),
                        base["directory"],
                    ),
                )
                self.db.execute(
                    "INSERT OR REPLACE INTO bindings VALUES(?,?,?,1)",
                    (self.workspace, body["name"], body["version"]),
                )
