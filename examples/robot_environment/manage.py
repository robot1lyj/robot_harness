"""Host setup and explicit protocol exercise, not an autonomous Agent or device deployment."""

import argparse
import asyncio
import json
from pathlib import Path

from robot_rsi.contracts import Profile
from robot_rsi.runtime import Runtime


async def call(runtime, name, arguments=None, task_id=None):
    reply = await runtime.invoke(name, arguments or {}, task_id=task_id)
    if not reply["ok"]:
        raise RuntimeError(json.dumps(reply, ensure_ascii=False))
    return reply["result"]


async def prepare(root):
    results = []
    for environment, view in (("a", "survey"), ("b", "detail")):
        profile = Profile(
            station="offline_slider", namespace="environment_" + environment,
            state_root=str(root / "state"), robot=None,
            host={"adapter": {"factory": "legacy_lab", "options": {"default_ports": True}},
                  "cameras": None, "depth": None,
                  "model_ref": {"id": "survey_slider", "version": "1"},
                  "calibration_refs": [{"id": "survey_intrinsics", "version": "1"}]},
            project={"id": "slider_project", "root": str(root), "purpose": "run"},
            workspace={"title": "Offline slider " + environment,
                       "purpose": "Inspect a recorded slider using installed computation",
                       "constraints": ["Authored recordings only; no device connection or motion"]},
            task_limits={"duration_s": 600, "tool_calls": 80},
        )
        (root / (environment + ".json")).write_text(profile.model_dump_json(indent=2) + "\n")
        async with Runtime(profile) as runtime:
            discovered = runtime.registry.discover()
            runtime.registry.enable("lab_inspect_height", "1.0.0")
            selected = {
                "scene_id": "height_inspection", "purpose": "Offline height interval inspection",
                "reason": "Use the installed recorded view and its named geometry/assets",
                "roles": {"camera": view + "_camera", "kinematics": view + "_kinematics"},
                "requirements": {"camera.capture": 1, "kinematics.fk": 1},
                "model_ref": {"id": view + "_slider", "version": "1"},
                "calibration_refs": [{"id": view + "_intrinsics", "version": "1"}],
                "conditions": {"dataset": "authored_lab"}, "skills": ["lab_inspect_height"],
            }
            checked = await call(runtime, "binding_validate", {"selection": selected, "trials": [
                {"role": "camera", "tool": "camera_capture", "arguments": {"require": "preview"}},
                {"role": "kinematics", "tool": "kinematics_fk", "arguments": {
                    "model_ref": selected["model_ref"], "arm": "slider", "joints": {"slide": 0.3},
                    "base_frame": "lab", "target_frame": "tip"}},
            ]})
            assert checked["valid"], checked
            saved = await call(runtime, "binding_save", {
                "reference": {"id": "height_inspection", "version": "1"}, "validation_id": checked["id"]})
            await call(runtime, "workspace_note", {
                "key": "recording_context", "title": "Installed offline source",
                "text": "Authored legacy_lab recordings and a bounded slider formula. "
                        "The saved scene selects " + view + "; physical readiness is untested.",
                "source": "installed lab_adapter factory / saved height_inspection binding"})
            results.append({"environment": environment, "discovered": discovered,
                            "binding": saved, "workspace": runtime.workspace_brief()})
    return results


async def run(root, args):
    profile = Profile.model_validate_json((root / (args.environment + ".json")).read_text())
    async with Runtime(profile) as runtime:
        note = await call(runtime, "workspace_read", {"key": "recording_context"})
        saved = await call(runtime, "binding_read", {"scene_id": "height_inspection"})
        started = await call(runtime, "task_begin", {
            "task_id": args.task, "goal": "Inspect offline slider height against the requested interval",
            "binding_ref": saved["reference"], "binding_conditions": {"dataset": "authored_lab"}})
        attempt = await runtime.invoke("task_run", {
            "task_id": args.task, "task_epoch": 0, "skill": "lab_inspect_height",
            "request_id": args.task + "_operation",
            "parameters": {"position_m": args.position, "lower_m": args.lower, "upper_m": args.upper}})
        operation = None
        if attempt["ok"]:
            operation = await call(runtime, "operation_get", {"operation_id": attempt["result"]["id"], "wait_s": 2})
            succeeded = operation["status"] == "succeeded"
            summary = "Recorded height inspection: " + json.dumps(operation.get("result"), ensure_ascii=False)
        else:
            succeeded = False
            summary = "Inspection could not execute: " + json.dumps(attempt["error"], ensure_ascii=False)
        task = await call(runtime, "task_checkpoint_get", {"task_id": args.task})
        finished = await call(runtime, "task_finish", {
            "task_id": args.task, "task_epoch": task["execution"]["epoch"],
            "outcome": "succeeded" if succeeded else "failed", "summary": summary[:1000],
            "operation_ids": [operation["id"]] if operation else []})
        archived = await call(runtime, "task_archive", {"task_id": args.task})
        return {"qualification": "explicit protocol exercise; no model decision", "started": started,
                "reused_note": note,
                "attempt": attempt, "operation": operation, "task_result": finished["task_result"],
                "archived": archived["archived"], "validations": len(runtime.store.all("binding_validation")),
                "active_pins": runtime.registry.db.execute("SELECT count(*) FROM pins WHERE workspace=?", (runtime.registry.workspace,)).fetchone()[0],
                "workspace": runtime.workspace_brief()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("prepare")
    execute = actions.add_parser("run")
    execute.add_argument("--environment", choices=("a", "b"), default="a")
    execute.add_argument("--task", required=True)
    execute.add_argument("--position", type=float, default=0.3)
    execute.add_argument("--lower", type=float, default=0.19)
    execute.add_argument("--upper", type=float, default=0.21)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    result = asyncio.run(prepare(root) if args.action == "prepare" else run(root, args))
    print(json.dumps(result, ensure_ascii=False, indent=2))
