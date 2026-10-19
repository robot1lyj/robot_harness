"""One Runtime owner and a temporary authenticated MCP endpoint for model/control clients."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import time
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path

import httpx
import uvicorn
from starlette.responses import JSONResponse

from .agent_client import AGENT_TOOLS, CodexClient
from .contracts import HarnessError, SkillQuery, TaskBegin, TaskContext
from .mcp_server import create_mcp
from .operations import TERMINAL
from .runtime import TOOLS, Runtime
from .storage import canonical
from .task_context import build_task_context


class AgentHost:
    def __init__(self, runtime):
        self.runtime = runtime
        self.admin = secrets.token_urlsafe(32)
        self.grants = {self.admin: {"actor": "operator"}}
        self.url = None
        self.client = CodexClient()
        self.clients = []

    def grant(self, request):
        token = request.headers.get("authorization", "").removeprefix("Bearer ") if request else ""
        return self.grants.get(token, {})

    @asynccontextmanager
    async def serve(self):
        server = create_mcp(self.runtime.profile, runtime=self.runtime, grant=self.grant)
        app = server.streamable_http_app()

        async def authenticated(scope, receive, send):
            if scope["type"] == "http":
                token = (
                    dict(scope["headers"])
                    .get(b"authorization", b"")
                    .decode()
                    .removeprefix("Bearer ")
                )
                if token not in self.grants:
                    await JSONResponse({"error": "unauthorized"}, status_code=401)(
                        scope, receive, send
                    )
                    return
            await app(scope, receive, send)

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)
        self.url = f"http://127.0.0.1:{sock.getsockname()[1]}/mcp"
        config = uvicorn.Config(
            authenticated, log_level="error", lifespan="on", timeout_graceful_shutdown=2
        )
        instance = uvicorn.Server(config)
        instance.capture_signals = nullcontext
        job = asyncio.create_task(instance.serve(sockets=[sock]))
        path = self.runtime.store.root / "agent-control.json"
        try:
            async with asyncio.timeout(5):
                while not instance.started:
                    if job.done():
                        await job
                        raise HarnessError("client_unavailable", "MCP endpoint failed to start")
                    await asyncio.sleep(0.01)
            # Only a local host control command reads this file, never the model.
            if path.is_symlink():
                raise HarnessError("invalid_storage", "Control file cannot be a symlink")
            if path.exists():
                path.chmod(0o600)
            with open(path, "w", opener=lambda p, flags: os.open(p, flags, 0o600)) as handle:
                json.dump({"url": self.url, "token": self.admin}, handle)
            yield self
        finally:
            for client in self.clients:
                if close := getattr(client, "close", None):
                    await close()
            path.unlink(missing_ok=True)
            instance.should_exit = True
            await job
            sock.close()

    async def run(self, task_id, *, client=None):
        client = client or self.client
        if client not in self.clients:
            self.clients.append(client)
        while True:
            # In-flight work is owned by Operations; elapsed waiting is not a decision.
            record = self.runtime.task_memory._load(task_id)
            for identity in record["operation_ids"]:
                if self.runtime.store.get("operation", identity)["status"] not in TERMINAL:
                    await self.wait_operation(task_id, identity, record["execution"]["epoch"])
            request = self.runtime.agent_state.request(task_id)
            if request["role"] == "complete":
                return {"status": "complete", "task_id": task_id, "request": request}
            if request["role"] == "closed":
                return {
                    "status": request["task_outcome"]["outcome"],
                    "task_id": task_id,
                    "request": request,
                }
            if request["role"] == "operator" or not request["configured"]:
                return {"status": "handoff", "task_id": task_id, "request": request}
            config = getattr(self.runtime.profile.agent, request["role"])
            try:
                binding = client.binding(request["role"], config)
            except HarnessError as exc:
                return {
                    "status": "handoff",
                    "task_id": task_id,
                    "request": request,
                    "client_error": exc.payload(),
                }
            session = self.runtime.agent_state.start(task_id, request["role"], binding)
            token = secrets.token_urlsafe(32)
            self.grants[token] = {"agent_session_id": session["id"]}
            started = time.monotonic()
            preparing = True
            try:
                record = self.runtime.task_memory._load(task_id)
                turn = {
                    "id": session["id"],
                    "task_id": task_id,
                    "task_epoch": session["task_epoch"],
                    "binding": binding,
                    "url": self.url,
                    "token": token,
                    "timeout_s": max(0.01, record["execution"]["deadline_at"] - time.time()),
                    "scope": self.runtime.store.scope,
                    "client_root": str(self.runtime.store.root / "native-client"),
                    "history_bytes": self.runtime.profile.task_limits.context_bytes * 4,
                    "tools": [{"type": "function", "name": name, "description": TOOLS[name][1],
                               "inputSchema": TOOLS[name][0].model_json_schema(),
                               "deferLoading": False} for name in AGENT_TOOLS],
                }
                cards = self.runtime.registry.cards(SkillQuery(query=record["goal"][:200], limit=3),
                                                    bindings=self.runtime.task_bindings(task_id))
                selected = next(iter(record["skill_versions"]), None)
                code_cards = [card for card in cards["skills"] if card["kind"] == "code"]
                if not selected and len(code_cards) == 1:
                    selected = code_cards[0]["name"]
                cursor = client.context_cursor(turn) if hasattr(client, "context_cursor") else None
                context_args = TaskContext(task_id=task_id, selected_skill=selected,
                                           blocks=True, delivered=cursor)
                view = build_task_context(self.runtime, context_args)
                turn["view"] = view
                turn["prompt"] = (f"角色 {request['role']}，接续任务。以下为必要全量或变化整块；"
                                  "已有块继续有效，不用重复读取。工具参数直接传入。\n" + canonical(view))
                self.runtime.store.event("agent.context_delivered", {
                    "task_id": task_id, "session_id": session["id"], "full": view["full"],
                    "changed": list(view["blocks"]), **view["metrics"],
                })

                async def invoke(name, arguments):
                    if name == "task_context":
                        arguments = {**arguments, "blocks": True,
                                     "delivered": turn["view"]["delivered"]}
                        arguments.setdefault("selected_skill", selected)
                    response = await self.runtime.invoke(name, arguments,
                                                         agent_session_id=session["id"])
                    if response["ok"] and name == "task_context":
                        turn["view"] = response["result"]
                    if response["ok"] and name in ("task_run", "task_recover", "operation_get",
                                                   "operation_cancel") and "id" in response["result"]:
                        operation = response["result"]
                        if operation["status"] not in TERMINAL:
                            lease = self.runtime.store.get("agent_session", session["id"])
                            operation = await self.wait_operation(task_id, operation["id"],
                                                                  lease["task_epoch"])
                        response["result"] = self.operation_result(task_id, operation)
                    elif response["ok"] and name == "task_finish":
                        response["result"] = {"task_id": task_id,
                                              "task_result": response["result"]["task_result"]}
                    return response

                turn["invoke"] = invoke
                preparing = False
                pending = asyncio.create_task(
                    client.run(turn, lambda e: self.runtime.agent_state.client_event(session, e))
                )
                try:
                    while not pending.done():
                        await asyncio.wait({pending}, timeout=0.05)
                        current = self.runtime.task_memory._load(task_id)["execution"]
                        lease = self.runtime.store.get("agent_session", session["id"])
                        if current["epoch"] != lease["task_epoch"]:
                            pending.cancel()
                            try:
                                await pending
                            except asyncio.CancelledError:
                                pass
                            result = {
                                "completed": False,
                                "message": "decision superseded by control change",
                            }
                            break
                    else:
                        result = await pending
                finally:
                    if not pending.done():
                        pending.cancel()
                        try:
                            await pending
                        except asyncio.CancelledError:
                            pass
            except asyncio.CancelledError:
                self.runtime.agent_state.finish(
                    session,
                    {"completed": False, "message": "host cancelled"},
                    elapsed_s=time.monotonic() - started,
                )
                await self.runtime.invoke("task_cancel", {"task_id": task_id})
                raise
            except Exception as exc:
                result = {
                    "completed": False,
                    "message": ("decision preparation failed: " if preparing else "client disconnected: ")
                    + type(exc).__name__,
                }
                if preparing:
                    result["preparation_error"] = exc.payload() if isinstance(exc, HarnessError) else {
                        "code": "preparation_failed", "cause": type(exc).__name__}
            finally:
                # Revoke only this model credential; the control endpoint remains available.
                self.grants.pop(token, None)
            finished = self.runtime.agent_state.finish(
                session, result, elapsed_s=time.monotonic() - started
            )
            if finished["status"] != "completed":
                return {
                    "status": "preparation_failed" if "preparation_error" in result else finished["status"],
                    **({"preparation_error": result["preparation_error"]}
                       if "preparation_error" in result else {}),
                    "task_id": task_id,
                    "session": finished,
                    "request": self.runtime.agent_state.request(task_id),
                }
            next_request = self.runtime.agent_state.request(task_id)
            if (
                next_request["role"] == request["role"]
                and next_request["fingerprint"] == request["fingerprint"]
            ):
                return {"status": "handoff", "task_id": task_id, "request": next_request}

    async def wait_operation(self, task_id, identity, epoch):
        started = time.monotonic()
        while True:
            operation = await self.runtime.operations.get(identity, 0.05)
            record = self.runtime.task_memory._load(task_id)
            if (operation["status"] in TERMINAL or record["execution"]["epoch"] != epoch
                or time.time() >= record["execution"]["deadline_at"]):
                break
        self.runtime.store.event("agent.operation_wait", {
            "task_id": task_id, "operation_id": identity,
            "elapsed_s": time.monotonic() - started, "status": operation["status"],
        })
        return operation

    def operation_result(self, task_id, operation):
        record = self.runtime.task_memory._load(task_id)
        return {**{key: operation.get(key) for key in
                   ("id", "status", "stage", "result", "error", "hold", "qualification",
                    "skill_name", "skill_version", "recovery_of")},
                "task_id": task_id, "task_epoch": record["execution"]["epoch"],
                "terminal": operation["status"] in TERMINAL,
                "allowed_recovery": self.runtime.task_memory.recovery_options(record),
                "details": {"tool": "operation_get", "operation_id": operation["id"]}}


def control_path(profile):
    return (
        Path(profile.state_root).expanduser().resolve()
        / profile.role
        / profile.station
        / profile.namespace
        / "agent-control.json"
    )


@asynccontextmanager
async def connect_mcp(url, token):
    from mcp.client.streamable_http import streamable_http_client

    async with httpx.AsyncClient(
        headers={"Authorization": "Bearer " + token}, timeout=30, trust_env=False
    ) as client:
        async with streamable_http_client(url, http_client=client) as streams:
            yield streams


async def control_call(profile, name, arguments):
    from urllib.parse import urlparse

    from mcp import ClientSession

    path = control_path(profile)
    if path.is_symlink():
        raise HarnessError("invalid_storage", "Control file cannot be a symlink")
    data = json.loads(path.read_text())
    endpoint = urlparse(data["url"])
    if endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" or endpoint.path != "/mcp":
        raise HarnessError("invalid_storage", "Control endpoint must be local MCP")
    try:
        async with connect_mcp(data["url"], data["token"]) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                reply = await session.call_tool(name, {"arguments": arguments})
    except Exception as exc:
        raise HarnessError(
            "agent_endpoint_unavailable",
            "Session endpoint is unavailable; inspect the stored task before reconnecting",
        ) from exc
    if reply.isError:
        raise HarnessError("control_rejected", reply.content[0].text)
    return reply.structuredContent or json.loads(reply.content[0].text)


async def run_agent(profile, task_id, goal=None):
    # This process is the sole Runtime/Store owner. Codex receives only its HTTP MCP URL.
    profile = profile.model_copy(update={"active_task_id": task_id})
    async with Runtime(profile) as runtime:
        try:
            record = runtime.task_memory._load(task_id)
        except HarnessError as exc:
            if exc.code != "not_found" or not goal:
                raise
            runtime.task_memory.begin(
                TaskBegin(task_id=task_id, goal=goal, constraints=profile.workspace.constraints),
                profile.task_limits,
            )
        else:
            if goal and goal != record["goal"]:
                raise HarnessError("task_conflict", "Existing task goal differs; resume by task ID")
            if "execution" not in record and not record["archived"]:
                runtime.task_memory.begin(
                    TaskBegin(
                        task_id=task_id, goal=record["goal"], constraints=record["constraints"]
                    ),
                    profile.task_limits,
                )
        async with AgentHost(runtime).serve() as host:
            return await host.run(task_id)
