from __future__ import annotations

import json
from contextlib import asynccontextmanager

from mcp.server.fastmcp import Context, FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError

from .contracts import ArtifactQuery, HarnessError, Identifier, Profile
from .prompts import base_prompt
from .runtime import TOOLS, Runtime


class RuntimeMCP(FastMCP):
    async def call_tool(self, name, arguments):
        # Keep advertised typed schemas, but route validation through Runtime so rejected
        # argument attempts also consume the bound task's allowance and enter its trace.
        if name not in TOOLS:
            return await super().call_tool(name, arguments)
        context = self.get_context().request_context
        runtime = context.lifespan_context
        grant = self.grant(context.request) if getattr(self, 'grant', None) else {}
        result = await runtime.invoke(
            name, arguments.get("arguments", {}), task_id=arguments.get("task_id"), **grant
        )
        if not result["ok"]:
            raise ToolError(json.dumps(result["error"], ensure_ascii=False))
        return result


def create_mcp(profile: Profile, *, runtime=None, grant=None) -> FastMCP:
    @asynccontextmanager
    async def lifespan(_server):
        if runtime is not None:
            yield runtime
        else:
            async with Runtime(profile) as owned:
                yield owned

    server = RuntimeMCP(
        "Robot RSI Harness",
        lifespan=lifespan,
        instructions=base_prompt()["text"] + "\n\nMCP protocol: all tool inputs are nested inside arguments. "
        + (f"The host bound task is {profile.active_task_id}. " if profile.active_task_id else "Use task_begin for a new task. ")
        + "Fetch artifact images only when useful.",
    )

    server.grant = grant

    def register(name, schema, description):
        async def call(arguments, ctx, task_id=None):
            result = await ctx.request_context.lifespan_context.invoke(
                name, arguments.model_dump(), task_id=task_id
            )
            if not result["ok"]:
                raise ToolError(json.dumps(result["error"], ensure_ascii=False))
            return result

        call.__name__ = name
        call.__annotations__ = {
            "arguments": schema,
            "ctx": Context,
            "task_id": Identifier | None,
            "return": dict,
        }
        server.add_tool(call, name=name, description=description)

    for name, (schema, description) in TOOLS.items():
        register(name, schema, description)

    @server.tool(description="Fetch one image artifact by ID, without filesystem access")
    async def artifact_image(
        arguments: ArtifactQuery, ctx: Context, task_id: Identifier | None = None
    ) -> Image:
        runtime = ctx.request_context.lifespan_context
        store = runtime.store
        try:
            response = await runtime.invoke(
                "artifact_info", arguments.model_dump(), task_id=task_id
            )
            if not response["ok"]:
                raise ToolError(json.dumps(response["error"], ensure_ascii=False))
            meta = response["result"]
            if meta["mime"] not in ("image/png", "image/jpeg"):
                raise HarnessError("not_an_image", "Use artifact_info for non-image artifacts")
            return Image(
                data=store.read_artifact(arguments.artifact_id),
                format="png" if meta["mime"] == "image/png" else "jpeg",
            )
        except HarnessError as exc:
            raise ToolError(json.dumps(exc.payload())) from exc

    return server
