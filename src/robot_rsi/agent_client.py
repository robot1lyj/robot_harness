"""Task-local Codex App Server stdio adapter; Runtime owns every tool and task fact."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

from .contracts import HarnessError
from .storage import canonical

AGENT_TOOLS = [
    "task_context", "task_checkpoint_get", "task_run", "task_recover", "task_cancel",
    "task_archive", "task_finish", "operation_get", "operation_cancel", "robot_observe",
    "workspace_read", "workspace_note", "skills_list", "skill_describe", "memory_recall",
    "project_read", "project_files", "issue_create", "issue_get",
    "assistance_get",
    "binding_candidates", "binding_read", "binding_validate", "binding_save",
    "camera_describe", "camera_capture", "kinematics_fk", "kinematics_ik", "model_get", "calibration_get",
]


class CodexClient:
    def __init__(self):
        self.process = None
        self.threads = {}
        self.pending = {}
        self.jobs = set()
        self.sequence = 0
        self.active = None
        self.auth_link = None
        self.process_starts = 0
        self.handshakes = 0

    def binding(self, role, config):
        if not shutil.which("codex"):
            raise HarnessError("client_unavailable", "Existing Codex CLI is not installed")
        path = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
        data = tomllib.loads(path.read_text()) if path.exists() else {}
        if data.get("model_provider"):
            raise HarnessError("client_unavailable", "Custom providers are not replaced here")
        return {
            "client": "codex", "transport": "app-server-stdio",
            "client_version": subprocess.check_output(["codex", "--version"], text=True).strip(),
            "role": role, "model": config.model or data.get("model"),
            "reasoning_effort": config.reasoning_effort or data.get("model_reasoning_effort"),
        }

    @staticmethod
    def key(turn):
        return canonical([turn["scope"], turn["task_id"], turn["binding"], turn.get("base_prompt", {}).get("identity")])

    def context_cursor(self, turn):
        entry = self.threads.get(self.key(turn))
        if entry and not self._rebuild(entry, turn):
            return entry.get("delivered")
        return None

    @staticmethod
    def _rebuild(entry, turn):
        # A decision-boundary reconstruction, never an execution/token quota or N-turn summary.
        window = entry.get("context_window")
        occupancy = (entry.get("last_usage") or {}).get("inputTokens", 0)
        return (window and occupancy > window * 0.6) or entry["visible_bytes"] > turn["history_bytes"]

    async def _write(self, message):
        self.process.stdin.write((canonical(message) + "\n").encode())
        await self.process.stdin.drain()

    async def rpc(self, method, params):
        self.sequence += 1
        identity = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[identity] = future
        try:
            await self._write({"id": identity, "method": method, "params": params})
            return await future
        finally:
            self.pending.pop(identity, None)

    async def _connect(self, turn):
        if self.process is not None:
            if self.process.returncode is not None:
                raise HarnessError("client_disconnected", "App Server exited; restore from checkpoint")
            return
        root = Path(turn["client_root"])
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        home = root / "codex"
        home.mkdir(mode=0o700, exist_ok=True)
        auth = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
        if not auth.is_file():
            raise HarnessError("client_unavailable", "Existing file-based Codex login is missing")
        self.auth_link = home / "auth.json"
        if self.auth_link.exists() or self.auth_link.is_symlink():
            self.auth_link.unlink()
        self.auth_link.symlink_to(auth.resolve())
        self.process = await asyncio.create_subprocess_exec(
            "codex", "app-server", "--listen", "stdio://",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env={**os.environ, "CODEX_HOME": str(home)},
            cwd=root, limit=4 * 1024 * 1024,
        )
        self.process_starts += 1
        self.reader = asyncio.create_task(self._read())
        self.stderr = asyncio.create_task(self._drain())
        await asyncio.wait_for(self.rpc("initialize", {
            "clientInfo": {"name": "robot_rsi", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True},
        }), 20)
        await self._write({"method": "initialized"})
        self.handshakes += 1

    async def _drain(self):
        while await self.process.stderr.read(65536):
            pass

    async def _read(self):
        try:
            async for line in self.process.stdout:
                message = json.loads(line)
                if "method" in message:
                    if "id" in message:
                        job = asyncio.create_task(self._tool(message))
                        self.jobs.add(job)
                        job.add_done_callback(self.jobs.discard)
                    else:
                        self._event(message)
                elif message.get("id") in self.pending:
                    future = self.pending[message["id"]]
                    if not future.done():
                        if "error" in message:
                            future.set_exception(HarnessError("client_protocol", str(message["error"])))
                        else:
                            future.set_result(message.get("result"))
        except (ValueError, OSError) as exc:
            self.read_error = type(exc).__name__
        finally:
            error = HarnessError("client_disconnected", "App Server stdio closed")
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(error)
            if self.active and not self.active["done"].done():
                self.active["done"].set_exception(error)

    async def _tool(self, message):
        active = self.active
        params = message.get("params", {})
        try:
            if (message["method"] != "item/tool/call" or not active
                or params.get("threadId") != active["entry"]["id"]
                or params.get("turnId") != active.get("turn_id")
                or params.get("tool") not in AGENT_TOOLS):
                response = {"ok": False, "error": {"code": "unbound_client_request"}}
            else:
                response = await active["turn"]["invoke"](params["tool"], params["arguments"])
                active["emit"]({"type": "item.completed", "item": {
                    "id": params["callId"], "type": "mcp_tool_call", "tool": params["tool"],
                    "status": "completed" if response["ok"] else "failed"}})
            text = canonical(response)
            if active:
                active["entry"]["visible_bytes"] += len(text.encode())
            await self._write({"id": message["id"], "result": {
                "contentItems": [{"type": "inputText", "text": text}], "success": response["ok"]}})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._write({"id": message["id"], "error": {
                "code": -32603, "message": type(exc).__name__}})

    def _event(self, message):
        active = self.active
        params = message.get("params", {})
        if not active or params.get("threadId") != active["entry"]["id"]:
            return
        method = message["method"]
        if method == "item/completed" and params["item"]["type"] != "reasoning":
            active["emit"]({"type": "client.item", "item_type": params["item"]["type"]})
        if method == "turn/started":
            active["turn_id"] = params["turn"]["id"]
            active["emit"]({"type": "turn.started", "turn_id": active["turn_id"]})
        if params.get("turnId") and params["turnId"] != active.get("turn_id"):
            return
        if method == "thread/tokenUsage/updated":
            active["usage"] = params["tokenUsage"]["total"]
            active["entry"]["last_usage"] = params["tokenUsage"]["last"]
            active["entry"]["context_window"] = params["tokenUsage"].get("modelContextWindow")
        elif method == "item/completed" and params["item"]["type"] == "agentMessage":
            active["message"] = params["item"]["text"]
            active["entry"]["visible_bytes"] += len(params["item"]["text"].encode())
        elif method == "error":
            active["emit"]({"type": "error", "message": str(params.get("error", {}))[:2000]})
        elif method == "turn/completed":
            native = params["turn"]
            if native["id"] == active.get("turn_id") and not active["done"].done():
                active["done"].set_result(native)

    async def run(self, turn, emit):
        if self.active:
            raise HarnessError("agent_busy", "One native turn at a time")
        await self._connect(turn)
        key = self.key(turn)
        entry = self.threads.get(key)
        rebuilt = bool(entry and self._rebuild(entry, turn))
        if rebuilt:
            await self.rpc("thread/unsubscribe", {"threadId": entry["id"]})
            entry = None
        if entry is None:
            from .prompts import base_prompt

            prompt = turn.get("base_prompt") or base_prompt()
            params = {
                "model": turn["binding"]["model"], "cwd": turn["client_root"],
                "approvalPolicy": "never", "sandbox": "read-only", "ephemeral": True,
                "baseInstructions": prompt["text"],
                "dynamicTools": turn["tools"], "experimentalRawEvents": False,
                "config": {"features": {"shell_tool": False, "unified_exec": False,
                           "multi_agent": False, "skip_host_skill_discovery": True,
                           "skill_search": False, "skill_mcp_dependency_install": False,
                           "apps": False, "plugins": False, "memories": False},
                           "web_search": "disabled"},
            }
            emit({"type": "client.initialization", "params": params, "prompt_identity": prompt["identity"]})
            result = await self.rpc("thread/start", params)
            emit({"type": "client.thread_created", "thread_id": result["thread"]["id"],
                  "model": result.get("model"), "reasoning_effort": result.get("reasoningEffort"),
                  "cwd": result["thread"].get("cwd")})
            entry = {"id": result["thread"]["id"], "visible_bytes": 0, "usage": {}}
            self.threads[key] = entry
        emit({"type": "thread.started", "thread_id": entry["id"]})
        emit({"type": "client.transport", "process_starts": self.process_starts,
              "handshakes": self.handshakes, "rebuilt": rebuilt, "thread_id": entry["id"]})
        active = {"entry": entry, "turn": turn, "emit": emit, "message": "", "usage": None,
                  "done": asyncio.get_running_loop().create_future()}
        self.active = active
        try:
            async with asyncio.timeout(turn["timeout_s"]):
                emit({"type": "client.turn_input", "thread_id": entry["id"],
                      "prompt": turn["prompt"], "effort": turn["binding"]["reasoning_effort"]})
                result = await self.rpc("turn/start", {
                    "threadId": entry["id"], "effort": turn["binding"]["reasoning_effort"],
                    "input": [{"type": "text", "text": turn["prompt"], "text_elements": []}],
                })
                active["turn_id"] = result["turn"]["id"]
                entry["delivered"] = turn["view"]["delivered"]
                entry["visible_bytes"] += len(turn["prompt"].encode())
                native = await active["done"]
            usage = None
            if active["usage"] is not None:
                mapping = {"inputTokens": "input_tokens", "outputTokens": "output_tokens",
                           "cachedInputTokens": "cached_input_tokens",
                           "cacheWriteInputTokens": "cache_write_input_tokens",
                           "reasoningOutputTokens": "reasoning_output_tokens"}
                values = {output: active["usage"].get(source, 0) - entry["usage"].get(source, 0)
                          for source, output in mapping.items()}
                if all(value >= 0 for value in values.values()):
                    usage = values
                entry["usage"] = active["usage"]
            completed = native["status"] == "completed"
            entry["delivered"] = turn["view"]["delivered"]
            emit({"type": "turn.completed" if completed else "turn.failed", "usage": usage,
                  "turn_id": native["id"], "error": native.get("error")})
            return {"completed": completed, "returncode": 0 if completed else 1,
                    "message": active["message"], "error": native.get("error")}
        except (asyncio.CancelledError, TimeoutError):
            if active.get("turn_id"):
                try:
                    await asyncio.wait_for(self.rpc("turn/interrupt", {
                        "threadId": entry["id"], "turnId": active["turn_id"]}), 3)
                except Exception:
                    pass
            # A cancelled turn is not reused: no late native result can acquire a new lease.
            self.threads.pop(key, None)
            try:
                await asyncio.wait_for(self.rpc("thread/unsubscribe", {"threadId": entry["id"]}), 3)
            except Exception:
                pass
            raise
        finally:
            for job in list(self.jobs):
                job.cancel()
            if self.jobs:
                await asyncio.gather(*self.jobs, return_exceptions=True)
            self.active = None

    async def close(self):
        if self.process:
            if self.process.returncode is None:
                self.process.stdin.close()
                try:
                    await asyncio.wait_for(self.process.wait(), 3)
                except TimeoutError:
                    self.process.terminate()
                    await self.process.wait()
            await self.reader
            await self.stderr
            self.process = None
        if self.auth_link:
            self.auth_link.unlink(missing_ok=True)
        self.threads.clear()
