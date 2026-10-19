"""Reproducible protocol replay of an explicit input choice; not autonomous Agent selection."""

import argparse
import asyncio
import json
from pathlib import Path

from robot_rsi.contracts import Profile
from robot_rsi.runtime import Runtime


async def run(args):
    base = Path(__file__).parent
    profile = Profile.model_validate_json((base / "profile.json").read_text())
    profile = profile.model_copy(update={"state_root": args.state_root})
    async with Runtime(profile) as runtime:
        runtime.registry.register(base / "skills/measure")
        runtime.registry.enable("lab_measure", "1.0.0")

        async def call(name, arguments, task_id=None):
            result = await runtime.invoke(name, arguments, task_id=task_id)
            assert result["ok"], result
            return result["result"]

        if args.selection:
            choice = json.loads(Path(args.selection).read_text())
            checked = await call(
                "binding_validate",
                {
                    "selection": choice,
                    "trials": [
                        {
                            "role": "camera",
                            "tool": "camera_capture",
                            "arguments": {"require": "preview"},
                        },
                        {
                            "role": "kinematics",
                            "tool": "kinematics_fk",
                            "arguments": {
                                "model_ref": choice["model_ref"],
                                "arm": "slider",
                                "joints": {"slide": 0.3},
                                "base_frame": "lab",
                                "target_frame": "tip",
                            },
                        },
                    ],
                },
            )
            assert checked["valid"], checked
            saved = await call(
                "binding_save",
                {
                    "reference": {"id": choice["scene_id"], "version": "1"},
                    "validation_id": checked["id"],
                },
            )
        else:
            saved = await call("binding_read", {"scene_id": "slider_recording"})
        validations = len(runtime.store.all("binding_validation"))
        task = await call(
            "task_begin",
            {
                "task_id": args.task,
                "goal": "read recording and calculate slider",
                "binding_ref": saved["reference"],
                "binding_conditions": {"dataset": "authored_lab"},
            },
        )
        listed = await call("skills_list", {"query": "lab_measure"}, args.task)
        described = await call("skill_describe", {"name": "lab_measure"}, args.task)
        context = await call(
            "task_context", {"task_id": args.task, "selected_skill": "lab_measure"}
        )
        assert listed["skills"] and described["available"] and not context.get("capability_gap")
        operation = await call(
            "task_run",
            {
                "task_id": args.task,
                "task_epoch": 0,
                "skill": "lab_measure",
                "request_id": args.task + "-1",
                "parameters": {"position_m": 0.3},
            },
        )
        operation = await call("operation_get", {"operation_id": operation["id"], "wait_s": 2})
        assert operation["status"] == "succeeded", operation
        task = await call("task_checkpoint_get", {"task_id": args.task})
        task = await call(
            "task_finish",
            {
                "task_id": args.task,
                "task_epoch": task["execution"]["epoch"],
                "outcome": "succeeded",
                "summary": "Read the selected authored recording and calculated slider=0.3 m: "
                + str(operation["result"]["tip_m"])
                + ". Offline calculation only; no motion.",
                "operation_ids": [operation["id"]],
                "evidence_refs": [{"kind": "artifact", "id": operation["result"]["artifact_id"]}],
            },
        )
        archived = await call("task_archive", {"task_id": args.task})
        assert archived["archived"] and task["task_result"]["outcome"] == "succeeded"
        assert len(runtime.store.all("binding_validation")) == validations
        print(
            json.dumps(
                {
                    "mode": "explicit-choice protocol replay"
                    if args.selection
                    else "saved binding reuse; no selection or validation",
                    "binding": saved,
                    "default_camera_selected": runtime.bindings.camera is not None,
                    "task_skills": listed,
                    "task_skill_available": described["available"],
                    "task_workspace": context.get("workspace"),
                    "operation": operation,
                    "task_result": task["task_result"],
                    "task_status": task["execution"]["status"],
                    "archived": archived["archived"],
                    "validations": validations,
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--selection")
    asyncio.run(run(parser.parse_args()))
