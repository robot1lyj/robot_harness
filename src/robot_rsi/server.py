from __future__ import annotations

import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .contracts import Contract, HarnessError, Identifier, Profile
from .runtime import Runtime


class ToolRequest(Contract):
    arguments: dict
    task_id: Identifier | None = None


def create_app(profile: Profile, token: str) -> FastAPI:
    if len(token) < 24:
        raise ValueError("HTTP requires ROBOT_RSI_TOKEN with at least 24 characters")

    @asynccontextmanager
    async def lifespan(app):
        async with Runtime(profile) as runtime:
            app.state.runtime = runtime
            yield

    app = FastAPI(
        title="Robot RSI Harness",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    async def authenticate(request: Request):
        value = request.headers.get("authorization", "")
        if not secrets.compare_digest(value, "Bearer " + token):
            raise HTTPException(status_code=401, detail="Bearer token required")

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": "0.1.0"}

    @app.get("/tools", dependencies=[Depends(authenticate)])
    async def tool_list(request: Request):
        return request.app.state.runtime.tools()

    @app.post("/tools/{name}", dependencies=[Depends(authenticate)])
    async def call(name: str, body: ToolRequest, request: Request):
        result = await request.app.state.runtime.invoke(name, body.arguments, task_id=body.task_id)
        return JSONResponse(result, status_code=200 if result["ok"] else 422)

    @app.get("/artifacts/{identity}", dependencies=[Depends(authenticate)])
    async def artifact(identity: str, request: Request):
        store = request.app.state.runtime.store
        try:
            metadata = store.get("artifact", identity)
            content = store.read_artifact(identity)
        except HarnessError as exc:
            return JSONResponse({"ok": False, "error": exc.payload()}, status_code=404)
        return Response(
            content,
            media_type=metadata["mime"],
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    return app
