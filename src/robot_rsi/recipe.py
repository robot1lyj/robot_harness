"""One complete, explicitly bound code recipe; the installed image versions its code."""
from __future__ import annotations

from .contracts import HarnessError, TaskBegin
from .operations import TERMINAL
from .skill_models import PickPlaceParameters
from .storage import uid

RECIPE = "pick_place_v1"


def goal_for(parameters):
    return (f"配方 {RECIPE}：用 {parameters.arm} 将 {parameters.object_id} 放到 "
            f"{parameters.target_region}（最多 {parameters.grasp_retries} 次技能内重试）；独立放置验证通过且 HOLD 确认，记录整个目标并归档。")


def completed(parameters, operation):
    result = operation.get("result") or {}
    actual = operation.get("args", {}).get("parameters", {})
    return (operation["status"] == "succeeded"
            and operation.get("skill_name") == "pick_place"
            and operation.get("hold", {}).get("confirmed") is True
            and result.get("verified") is True
            and result.get("object_id") == parameters.object_id
            and result.get("target_region") == parameters.target_region
            and PickPlaceParameters.model_validate(actual) == parameters)


async def run_recipe(runtime, task_id, parameters):
    parameters = PickPlaceParameters.model_validate(parameters)
    goal = goal_for(parameters)
    try:
        record = runtime.task_memory._load(task_id)
    except HarnessError as exc:
        if exc.code != "not_found":
            raise
        record = runtime.task_memory.begin(TaskBegin(
            task_id=task_id, goal=goal, recipe=RECIPE,
            constraints=runtime.profile.workspace.constraints), runtime.profile.task_limits)
    if record.get("recipe") != RECIPE or record["goal"] != goal:
        raise HarnessError("recipe_goal_mismatch", "Recipe does not cover this entire task goal")
    if record.get("task_result"):
        if record["task_result"]["outcome"] == "succeeded" and not record["archived"]:
            await runtime.invoke("task_archive", {"task_id": task_id})
        return {"path": "recipe-code", "recipe": RECIPE,
                "task_outcome": runtime.task_memory.outcome(runtime.task_memory._load(task_id))}
    if record["operation_ids"]:
        operation = runtime.store.get("operation", record["operation_ids"][-1])
    else:
        response = await runtime.invoke("task_run", {
            "task_id": task_id, "task_epoch": record["execution"]["epoch"],
            "request_id": uid("recipe"), "skill": "pick_place", "parameters": parameters.model_dump(),
        })
        if not response["ok"]:
            return response
        operation = response["result"]
    while operation["status"] not in TERMINAL:
        operation = await runtime.operations.get(operation["id"], 30)
    if completed(parameters, operation):
        record = runtime.task_memory._load(task_id)
        result = await runtime.invoke("task_finish", {
            "task_id": task_id, "task_epoch": record["execution"]["epoch"],
            "outcome": "succeeded", "summary": goal, "operation_ids": [operation["id"]],
        })
        if not result["ok"]:
            return result
        result = await runtime.invoke("task_archive", {"task_id": task_id})
        if not result["ok"]:
            return result
    return {"path": "recipe-code", "recipe": RECIPE, "operation_id": operation["id"],
            "status": operation["status"],
            "task_outcome": runtime.task_memory.outcome(runtime.task_memory._load(task_id)),
            "next": None if completed(parameters, operation) else "assistance_get"}


async def recipe_entry(profile, task_id, parameters):
    from .runtime import Runtime

    profile = profile.model_copy(update={"active_task_id": task_id})
    async with Runtime(profile) as runtime:
        return await run_recipe(runtime, task_id, parameters)
