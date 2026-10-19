from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import numpy as np

from .contracts import HarnessError, Profile


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class Store:
    """One host-selected scope per process; no tool accepts a pool or filesystem path."""

    def __init__(self, profile: Profile):
        base = Path(profile.state_root).expanduser().resolve()
        self.scope = {
            "role": profile.role,
            "station": profile.station,
            "namespace": profile.namespace,
            "robot": profile.robot,
        }
        self.root = base / profile.role / profile.station / profile.namespace
        if self.root.resolve() != self.root:
            raise HarnessError("invalid_storage", "Scope directory must not traverse symlinks")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in ("owner.lock", "scope.json", "state.sqlite3", "artifacts", "task-memory"):
            if (self.root / name).is_symlink():
                raise HarnessError("invalid_storage", "Storage entries cannot be symlinks")
        self._lock = (self.root / "owner.lock").open("a")
        try:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock.close()
            raise HarnessError("scope_busy", "Another runtime owns this scope") from exc
        identity = self.root / "scope.json"
        if identity.exists() and json.loads(identity.read_text()) != self.scope:
            self._lock.close()
            raise HarnessError("scope_mismatch", "Storage identity does not match profile")
        identity.write_text(canonical(self.scope))
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir(exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.root / "state.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS records (
              id TEXT PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
              seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
              kind TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS requests (
              request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, operation_id TEXT NOT NULL);
        """)
        self.db.commit()

    @contextmanager
    def transaction(self):
        """Durably commit one logical state transition, including its evidence, once."""
        self.db.execute("BEGIN IMMEDIATE")
        self._batch = True
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        finally:
            self._batch = False

    def _commit(self):
        if not getattr(self, "_batch", False):
            self.db.commit()

    def close(self):
        self.db.close()
        self._lock.close()

    def put(self, kind: str, identity: str, body: dict):
        self.db.execute(
            "INSERT INTO records VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
            (identity, kind, canonical(body)),
        )
        self._commit()

    def get(self, kind: str, identity: str) -> dict:
        row = self.db.execute(
            "SELECT body FROM records WHERE id=? AND kind=?", (identity, kind)
        ).fetchone()
        if row is None:
            raise HarnessError("not_found", "No accessible record with this identity")
        return json.loads(row[0])

    def all(self, kind: str) -> list[dict]:
        return [
            json.loads(row[0])
            for row in self.db.execute(
                "SELECT body FROM records WHERE kind=? ORDER BY rowid", (kind,)
            )
        ]

    def event(self, kind: str, body: dict):
        self.db.execute(
            "INSERT INTO events(at, kind, body) VALUES (?, ?, ?)",
            (time.time(), kind, canonical(body)),
        )
        self._commit()

    def task_memory_path(self, task_id: str) -> Path:
        # IDs are host-scoped references; callers never provide filesystem paths.
        directory = self.root / "task-memory"
        if directory.is_symlink():
            raise HarnessError("invalid_storage", "Task memory directory cannot be a symlink")
        directory.mkdir(exist_ok=True, mode=0o700)
        name = hashlib.sha256(task_id.encode()).hexdigest() + ".md"
        path = directory / name
        if path.is_symlink():
            raise HarnessError("invalid_storage", "Task memory file cannot be a symlink")
        return path

    def write_task_memory(self, task_id: str, content: str):
        path = self.task_memory_path(task_id)
        temporary = path.with_name(uid("memory") + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                handle.write(content)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return path.relative_to(self.root).as_posix()

    def artifact(self, content: bytes, mime: str, metadata: dict | None = None) -> str:
        identity = uid("artifact")
        path = self.artifacts / identity
        with path.open("xb") as f:
            f.write(content)
        self.put(
            "artifact",
            identity,
            {
                "id": identity,
                "mime": mime,
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "metadata": metadata or {},
            },
        )
        return identity

    def artifact_path(self, identity: str) -> Path:
        self.get("artifact", identity)
        path = self.artifacts / identity
        if path.is_symlink() or path.resolve().parent != self.artifacts.resolve():
            raise HarnessError("invalid_artifact", "Artifact is outside its scope")
        return path

    def read_artifact(self, identity: str) -> bytes:
        meta = self.get("artifact", identity)
        content = self.artifact_path(identity).read_bytes()
        if hashlib.sha256(content).hexdigest() != meta["sha256"]:
            raise HarnessError("artifact_changed", "Artifact failed integrity check")
        return content

    def array(self, array: np.ndarray, metadata: dict) -> str:
        stream = io.BytesIO()
        np.save(stream, array, allow_pickle=False)
        return self.artifact(stream.getvalue(), "application/x-npy", metadata)

    def load_array(self, identity: str) -> np.ndarray:
        return np.load(io.BytesIO(self.read_artifact(identity)), allow_pickle=False)

    def deduplicate(self, request_id: str, fingerprint: str) -> str | None:
        row = self.db.execute(
            "SELECT fingerprint, operation_id FROM requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if not row:
            return None
        if row["fingerprint"] != fingerprint:
            raise HarnessError("request_conflict", "Request ID already has different arguments")
        return row["operation_id"]

    def bind_request(self, request_id: str, fingerprint: str, operation_id: str):
        self.db.execute(
            "INSERT INTO requests VALUES (?, ?, ?)", (request_id, fingerprint, operation_id)
        )
        self._commit()
