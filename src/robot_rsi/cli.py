from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from .contracts import HarnessError, Profile, ProjectConfig, StereoCalibration
from .depth import import_stereo
from .memory import Memory
from .operations import TERMINAL
from .runtime import OPERATION_TOOLS, Runtime
from .storage import Store, uid


def output(value):
    print(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False))


async def run_command(args, profile):
    async with Runtime(profile) as runtime:
        if args.command == "tools":
            output(runtime.tools())
            return 0
        if args.command == "project":
            from .project import ProjectRead

            if profile.project is None:
                raise HarnessError("project_required", "Host has not selected a project")
            result = {"ok": True, "result": runtime.project.read(
                ProjectRead(key=args.key, query=args.query)
            ) if args.key or args.query else runtime.workspace_brief().get("project")}
        elif args.command == "feedback":
            from .feedback import IssueGet

            if args.action == "export":
                result = runtime.feedback.get(IssueGet(issue_id=args.issue_id))
                Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            elif args.action == "import-reply":
                result = runtime.feedback.import_reply(json.loads(Path(args.file).read_text()))
            elif args.action == "attest":
                result = runtime.feedback.attest(args.reply_id, args.task_id, args.operation_id, args.note)
            else:
                result = runtime.feedback.get(IssueGet(issue_id=args.issue_id, kind=args.kind,
                                                      limit=args.limit, cursor=args.cursor))
            output(result)
            return 0
        elif args.command == "call":
            result = await runtime.invoke(
                args.name, json.loads(args.arguments), task_id=args.task_id
            )
            if args.name in OPERATION_TOOLS and result["ok"] and "id" in result["result"]:
                operation_id = result["result"]["id"]
                while result["result"]["status"] not in TERMINAL:
                    result = await runtime.invoke(
                        "operation_get", {"operation_id": operation_id, "wait_s": 30}
                    )
                    if not result["ok"]:
                        break
        elif args.command == "demo":
            if profile.robot != "mock":
                raise HarnessError("mock_required", "demo only runs with the mock profile")
            accepted = await runtime.invoke("task_run", {"request_id": uid("demo")})
            if not accepted["ok"]:
                output(accepted)
                return 1
            result = await runtime.invoke(
                "operation_get", {"operation_id": accepted["result"]["id"], "wait_s": 30}
            )
            if result["ok"] and result["result"]["status"] == "succeeded":
                result["candidate_memory"] = await runtime.invoke(
                    "memory_remember",
                    {
                        "claim": "Synthetic pick/place completed with post-release scene verification; not real-robot evidence.",
                        "operation_id": result["result"]["id"],
                        "tags": ["pick_place", "synthetic"],
                    },
                )
        elif args.command in ("import-stereo", "depth"):
            calibration = StereoCalibration.model_validate_json(Path(args.calibration).read_text())
            observation = import_stereo(
                runtime.store,
                Path(args.left).read_bytes(),
                Path(args.right).read_bytes(),
                calibration,
            )
            result = {"ok": True, "result": observation}
            if args.command == "depth":
                result = await runtime.invoke(
                    "depth_estimate",
                    {"observation_id": observation["id"], "max_depth_m": args.max_depth_m},
                )
        else:
            raise ValueError("Unknown command")
        output(result)
        # A one-shot CLI cannot leave an asynchronous operation running after process exit.
        return (
            0
            if result["ok"]
            and result.get("result", {}).get("status")
            not in ("failed", "unknown", "timed_out", "needs_assistance", "cancelled")
            else 1
        )


def main():
    parser = argparse.ArgumentParser(description="Robot RSI Harness：机器人使用项目的能力、技能与任务入口")
    parser.add_argument("--profile", default=os.environ.get("ROBOT_RSI_PROFILE"))
    parser.add_argument("--task-id", help="Associate calls with an existing task budget")
    from importlib.metadata import version

    parser.add_argument("--version", action="version", version=version("robot-rsi-harness"))
    sub = parser.add_subparsers(dest="command", required=True)
    adapter = sub.add_parser(
        "adapter", help="Read installed adapter factory metadata without opening a Runtime"
    )
    adapter.add_argument("action", choices=["list"])

    sub.add_parser("tools")
    initialize = sub.add_parser("init", help="在当前选定目录建立独立使用项目，不覆盖已有文件")
    initialize.add_argument("directory", nargs="?", default=".")
    initialize.add_argument("--core-wheel", required=True)
    initialize.add_argument("--source", help="只读旧来源目录")
    initialize.add_argument("--no-sync", action="store_true", help="只生成文件，安装由显式 uv 命令接续")
    guidance = sub.add_parser("guide", help="读取当前安装的同源 Agent 引导或中文公共指南")
    guidance.add_argument("section", choices=["agent", "base", "onboarding", "project", "integration", "interfaces"])
    project = sub.add_parser("project", help="Host purpose entry over an existing project and role")
    project.add_argument("--purpose", choices=["onboarding", "run", "improve", "evaluate"])
    project.add_argument("--key")
    project.add_argument("--query", default="")
    feedback = sub.add_parser("feedback", help="Host-only issue/reply file exchange; no network")
    actions = feedback.add_subparsers(dest="action", required=True)
    export_issue = actions.add_parser("export")
    export_issue.add_argument("issue_id")
    export_issue.add_argument("--output", required=True)
    import_reply = actions.add_parser("import-reply")
    import_reply.add_argument("file")
    status = actions.add_parser("status")
    status.add_argument("issue_id", nargs="?")
    status.add_argument("--kind", choices=["all", "updates", "pending"], default="all")
    status.add_argument("--limit", type=int, default=3)
    status.add_argument("--cursor")
    attest = actions.add_parser("attest")
    attest.add_argument("reply_id")
    attest.add_argument("--task-id", required=True)
    attest.add_argument("--operation-id", required=True)
    attest.add_argument("--note", required=True)
    sub.add_parser(
        "config-check",
        help="Validate host bindings without opening a runtime or connecting hardware",
    )
    sub.add_parser("demo")
    mcp = sub.add_parser("mcp", help="Run stdio MCP for one host-selected memory scope")
    mcp.add_argument("--project", help="Read-only project scope without an adapter profile")
    mcp.add_argument("--usage-root", help="Selected usage destination; no creation, inspection or added permissions")
    serve = sub.add_parser("serve", help="Run authenticated loopback HTTP")
    serve.add_argument("--port", type=int, default=8767)
    call = sub.add_parser("call", help="One-shot tool call; motion waits for its terminal outcome")
    call.add_argument("name")
    call.add_argument("arguments", nargs="?", default="{}")
    for command in ("import-stereo", "depth"):
        stereo = sub.add_parser(command)
        stereo.add_argument("--left", required=True)
        stereo.add_argument("--right", required=True)
        stereo.add_argument("--calibration", required=True)
        stereo.add_argument("--max-depth-m", type=float, default=5)
    review = sub.add_parser("memory-review", help="Host-only review; unavailable to runtime tools")
    review.add_argument("identity")
    review.add_argument("status", choices=["verified", "rejected", "stale"])
    review.add_argument("--note", required=True)
    experience = sub.add_parser("experience", help="Host-only selected evidence consolidation")
    actions = experience.add_subparsers(dest="action", required=True)
    export = actions.add_parser("export")
    export.add_argument("--task-id", required=True)
    export.add_argument("--output", required=True)
    consolidate = actions.add_parser("consolidate")
    consolidate.add_argument("selection")
    consolidate.add_argument("lesson")
    verify = actions.add_parser("verify")
    verify.add_argument("identity")
    verify.add_argument("--evaluation", required=True)
    evaluation = sub.add_parser("evaluation", help="Host-only independent report import")
    actions = evaluation.add_subparsers(dest="action", required=True)
    report = actions.add_parser("import")
    report.add_argument("report")
    report.add_argument("--experience-id", action="append", default=[], help="Selected id@revision")
    report.add_argument("--output", required=True)
    release = sub.add_parser("release", help="Host-only evaluated publication and rollback")
    actions = release.add_subparsers(dest="action", required=True)
    publish = actions.add_parser("publish")
    publish.add_argument("directory")
    publish.add_argument("--evaluation", required=True)
    rollback = actions.add_parser("rollback")
    rollback.add_argument("identity")
    actions.add_parser("list")
    reconcile = actions.add_parser("reconcile")
    reconcile.add_argument("identity", nargs="?")
    installation = sub.add_parser("installation", help="Installed runtime/dependency identity")
    actions = installation.add_subparsers(dest="action", required=True)
    actions.add_parser("identity")
    verify = actions.add_parser("verify")
    verify.add_argument("manifest")
    host = sub.add_parser("host", help="宿主从固定产物准备受控安装；不采纳已有可写安装")
    actions = host.add_subparsers(dest="action", required=True)
    prepare = actions.add_parser("prepare")
    prepare.add_argument("--directory", required=True)
    prepare.add_argument("--core-wheel", required=True)
    prepare.add_argument("--core-sha256", required=True)
    prepare.add_argument("--requirements", required=True)
    prepare.add_argument("--wheel", action="append", default=[])
    prepare.add_argument("--uv")
    prepare.add_argument("--codex")
    update = actions.add_parser("update")
    update.add_argument("--baseline", required=True)
    update.add_argument("--directory", required=True)
    update.add_argument("--wheel", action="append", required=True)
    evidence = sub.add_parser("evidence", help="Host-only scoped task/evidence export")
    evidence.add_argument("--task-id", required=True)
    evidence.add_argument("--output", required=True)
    skill = sub.add_parser("skill", help="Host-only skill package management; no hardware access")
    management = skill.add_subparsers(dest="action", required=True)
    create = management.add_parser("create")
    create.add_argument("directory")
    create.add_argument("name")
    create.add_argument("--version", default="1.0.0")
    create.add_argument("--kind", choices=["code", "agent_workflow"], required=True)
    register = management.add_parser("register")
    register.add_argument("directory")
    management.add_parser("discover")
    for action in ("inspect", "enable", "disable", "unbind", "delete"):
        command = management.add_parser(action)
        command.add_argument("name")
        if action in ("enable", "delete"):
            command.add_argument("version")
        if action == "inspect":
            command.add_argument("--version")
    agent = sub.add_parser(
        "agent", help="Natural-language task/session entry using the existing client"
    )
    actions = agent.add_subparsers(dest="action", required=True)
    run = actions.add_parser("run")
    run.add_argument("--task-id")
    run.add_argument("--project", help="Inspect this existing source root without an adapter profile")
    run.add_argument("--usage-root", help="Selected usage destination; no creation, inspection or added permissions")
    run.add_argument("goal", nargs="?")
    recipe = actions.add_parser(
        "recipe", help="Explicit complete pick_place_v1 code goal; no model"
    )
    recipe.add_argument("--task-id", required=True)
    recipe.add_argument("--parameters", default="{}", help="PickPlaceParameters JSON")
    for action in ("status", "cancel", "claim", "return"):
        item = actions.add_parser(action)
        item.add_argument("--task-id", required=True)
        if action in ("claim", "return"):
            item.add_argument("--request-id", required=True)
            item.add_argument("--epoch", type=int, required=True)
            item.add_argument("--operator", required=True)
        if action == "return":
            item.add_argument("--disposition", choices=["resume", "handoff"], required=True)
            item.add_argument("--note", required=True)
    args = parser.parse_args()
    try:
        if args.command == "host":
            from .host_install import prepare, update

            if args.action == "prepare":
                output(prepare(args.directory, args.core_wheel, args.core_sha256, args.requirements,
                               wheels=args.wheel, uv=args.uv, codex=args.codex))
            else:
                output(update(args.baseline, args.directory, args.wheel))
            return
        if args.command in ("init", "guide"):
            from .project_setup import guide, initialize

            if args.command == "guide":
                print(guide(args.section))
            else:
                output(initialize(args.directory, args.core_wheel, source=args.source, sync=not args.no_sync))
            return
        source_entry = (args.command == "mcp" or (args.command == "agent" and args.action == "run")) and args.project
        if args.profile:
            profile = Profile.model_validate_json(Path(args.profile).read_text())
        elif source_entry:
            profile = Profile()
        else:
            from .project_setup import default_project_profile

            profile = default_project_profile() or Profile()
        if args.command == "mcp" or (args.command == "agent" and args.action == "run"):
            if args.project:
                if args.profile:
                    raise HarnessError("project_profile_conflict", "Use --project or --profile")
                from .project import source_profile

                profile = source_profile(args.project, usage_root=args.usage_root)
            elif args.usage_root:
                if profile.project is None:
                    raise HarnessError("project_required", "Select a source project before a usage destination")
                project = ProjectConfig.model_validate({**profile.project.model_dump(), "usage_root": args.usage_root})
                profile = profile.model_copy(update={"project": project})
        if args.command == "project" and args.purpose:
            if profile.project is None:
                raise HarnessError("project_required", "Configure the existing project in the host profile")
            profile = profile.model_copy(update={"project": profile.project.model_copy(
                update={"purpose": args.purpose})})
        if args.command == "adapter":
            from .adapters import factories

            output(
                {
                    "adapters": [
                        {"name": name, "entry": entry.value}
                        for name, entry in sorted(factories().items())
                    ],
                    "connected": False,
                }
            )
            return 0
        if args.command == "installation":
            from .installation import installation_identity

            identity = installation_identity()
            if args.action == "verify":
                expected = json.loads(Path(args.manifest).read_text())["installation"]
                if identity["digest"] != expected["digest"]:
                    raise HarnessError("installation_mismatch", "Release installation changed")
                output({"verified": True, "installation_digest": identity["digest"]})
            else:
                output(identity)
            return
        if args.command in ("experience", "evaluation", "release", "evidence"):
            from .experience import Experience, Lesson, Releases, import_evaluation
            from .registry import Registry

            store = Store(profile)
            registry = None
            try:
                if args.command == "evidence":
                    from .evidence import export_task

                    registry = Registry(profile)
                    result = export_task(store, registry, args.task_id)
                    Path(args.output).write_text(
                        json.dumps(result, ensure_ascii=False, indent=2) + "\n"
                    )
                elif args.command == "experience":
                    manager = Experience(store)
                    if args.action == "export":
                        result = manager.export(args.task_id)
                        Path(args.output).write_text(
                            json.dumps(result, ensure_ascii=False, indent=2) + "\n"
                        )
                    elif args.action == "consolidate":
                        result = manager.consolidate(
                            json.loads(Path(args.selection).read_text()),
                            Lesson.model_validate_json(Path(args.lesson).read_text()),
                        )
                    else:
                        result = manager.verify(
                            args.identity, json.loads(Path(args.evaluation).read_text())
                        )
                elif args.command == "evaluation":
                    result = import_evaluation(
                        store, json.loads(Path(args.report).read_text()), args.experience_id
                    )
                    Path(args.output).write_text(
                        json.dumps(result, ensure_ascii=False, indent=2) + "\n"
                    )
                elif args.action == "list":
                    result = store.all("release")
                else:
                    registry = Registry(profile)
                    manager = Releases(store, registry)
                    if args.action == "publish":
                        result = manager.publish(
                            json.loads(Path(args.evaluation).read_text()), args.directory
                        )
                    elif args.action == "reconcile":
                        result = manager.reconcile(args.identity)
                    else:
                        result = manager.rollback(args.identity)
                output(result)
                if args.command == "release" and isinstance(result, dict):
                    sys.exit(0 if result["status"] in ("published", "rolled_back") else 1)
            finally:
                if registry:
                    registry.close()
                store.close()
            return
        if args.command == "agent":
            if args.action == "recipe":
                from .builtin_tasks.pick_place import recipe_entry

                output(
                    asyncio.run(recipe_entry(profile, args.task_id, json.loads(args.parameters)))
                )
                return 0
            from .agent_host import control_call, control_path, run_agent

            if args.action == "run":
                if not args.task_id and not args.goal:
                    raise HarnessError("goal_required", "Provide a request or an explicit task ID to resume")
                args.task_id = args.task_id or uid("task")
                result = asyncio.run(run_agent(profile, args.task_id, args.goal))
                output(result)
                sys.exit(0 if result["status"] == "complete" else 1)
            name = {
                "status": "agent_session_get",
                "cancel": "task_cancel",
                "claim": "assistance_claim",
                "return": "assistance_return",
            }[args.action]
            arguments = {"task_id": args.task_id}
            if args.action in ("claim", "return"):
                arguments.update(
                    request_id=args.request_id, task_epoch=args.epoch, operator=args.operator
                )
            if args.action == "return":
                arguments.update(disposition=args.disposition, note=args.note)
            if control_path(profile).exists():
                result = asyncio.run(control_call(profile, name, arguments))
            else:

                async def local():
                    async with Runtime(profile) as runtime:
                        return await runtime.invoke(name, arguments, actor="operator")

                result = asyncio.run(local())
            output(result)
            sys.exit(0 if result.get("ok", False) else 1)
        if args.command == "skill":
            from .registry import Registry
            from .skill_management import create

            if args.action == "create":
                output(create(args.directory, args.name, args.version, args.kind))
                return
            registry = Registry(profile)
            try:
                if args.action == "register":
                    result = registry.register(args.directory)
                elif args.action == "discover":
                    result = registry.discover()
                elif args.action == "inspect":
                    result = registry.describe(args.name, args.version, history=True)
                elif args.action in ("enable", "delete"):
                    result = getattr(registry, args.action)(args.name, args.version)
                else:
                    result = getattr(registry, args.action)(args.name)
                output(result)
            finally:
                registry.close()
            return
        if args.command == "config-check":
            from .adapters import config_check

            output(config_check(profile))
            return
        if args.command == "serve":
            import uvicorn

            from .server import create_app

            uvicorn.run(
                create_app(profile, os.environ.get("ROBOT_RSI_TOKEN", "")),
                host="127.0.0.1",
                port=args.port,
                workers=1,
            )
            return
        if args.command == "mcp":
            from .mcp_server import create_mcp

            create_mcp(profile).run(transport="stdio")
            return
        if args.command == "memory-review":
            store = Store(profile)
            try:
                output(Memory(store).review(args.identity, args.status, args.note))
            finally:
                store.close()
            return
        sys.exit(asyncio.run(run_command(args, profile)))
    except HarnessError as exc:
        output({"ok": False, "error": exc.payload()})
        sys.exit(1)
    except (ValueError, OSError, ValidationError) as exc:
        output({"ok": False, "error": {"code": "configuration_error", "message": str(exc)}})
        sys.exit(1)


if __name__ == "__main__":
    main()
