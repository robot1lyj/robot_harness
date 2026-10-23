"""Installed entry exercise; reads ordinary point inputs, writes original run reports."""
import argparse
import asyncio
import json
from pathlib import Path
from robot_rsi.contracts import Profile
from robot_rsi.runtime import Runtime


def delivery(goal, parameters, kind="one_off"):
    return {"original_goal": goal, "kind": kind, "criteria": [
        {"id": "report", "description": "Visit every point and produce sourced image results", "required": True},
        {"id": "normal", "description": "All point images confirm normality", "required": parameters.get("mode", "confirm_normal") == "confirm_normal"}],
        "entry": {"skill": "multipoint_inspection", "version": "0.1.0", "parameters": parameters}, "remaining": []}


async def exercise(profile, task_id, parameters, goal="巡检全部输入点并确认正常"):
    async with Runtime(profile) as runtime:
        async def call(tool_name, **arguments):
            response = await runtime.invoke(tool_name, arguments)
            if not response["ok"]:
                raise RuntimeError(response)
            return response["result"]
        runtime.registry.discover()
        runtime.registry.enable("multipoint_inspection", "0.1.0")
        registration = await call("skill_describe", name="multipoint_inspection")
        await call("resource_connect", resource_id=parameters["resource_id"])
        await call("task_begin", task_id=task_id, goal=goal, delivery=delivery(goal, parameters))
        op = await call("task_run", task_id=task_id, task_epoch=0, request_id=task_id + "-run",
                        skill="multipoint_inspection", parameters=parameters)
        operation = await runtime.operations.get(op["id"], 10)
        record = runtime.task_memory._load(task_id)
        finish = await runtime.invoke("task_finish", {"task_id": task_id, "task_epoch": record["execution"]["epoch"],
            "outcome": "succeeded", "summary": goal, "operation_ids": [op["id"]]})
        report = json.loads(runtime.store.read_artifact(operation["result"]["report_id"])) if operation.get("result") else None
        return {"registration": registration, "source": "installed Runtime task_run; synthetic offline",
                "operation": operation, "finish": finish, "report": report,
                "commands": runtime.robot.commands,
                "samples": runtime.cameras.samples,
                "checkpoint": runtime.task_memory.get(__import__('robot_rsi.contracts', fromlist=['TaskCheckpointGet']).TaskCheckpointGet(task_id=task_id))}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    profile = Profile.model_validate_json(Path(args.profile).read_text())
    result = asyncio.run(exercise(profile, args.task, json.loads(Path(args.input).read_text())))
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"operation": result["operation"]["status"], "finish": result["finish"]["ok"], "output": args.output}))
