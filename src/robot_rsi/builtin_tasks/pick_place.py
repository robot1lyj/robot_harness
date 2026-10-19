"""Pick/place reference task: parameters, execution and complete-goal recipe."""
from __future__ import annotations

import json
from importlib.resources import files

from pydantic import Field

from ..capabilities import PICK_PLACE_REQUIREMENTS, PickPlaceGoal, SkillContext
from ..contracts import Contract, HarnessError, Identifier, PickPlace, TaskBegin
from ..operations import TERMINAL
from ..storage import uid


class PickPlaceParameters(Contract):
    arm: Identifier = "left"
    object_id: Identifier = "red_block"
    target_region: Identifier = "blue_box"
    grasp_retries: int = Field(default=1, ge=0, le=1)

    def validate_context(self, context):
        context.bindings.manipulation.validate_goal(
            PickPlaceGoal(arm=self.arm, object_id=self.object_id, target_region=self.target_region)
        )


PICK_PLACE_VERSION = json.loads(
    files("robot_rsi").joinpath("resources/pick_place/skill.json").read_text()
)["version"]


async def pick_place(context: SkillContext, args: PickPlace | PickPlaceParameters):
    bindings = context.bindings
    bindings.require(PICK_PLACE_REQUIREMENTS)
    scene, manipulation, verification = bindings.scene, bindings.manipulation, bindings.verification
    goal = PickPlaceGoal(arm=args.arm, object_id=args.object_id, target_region=args.target_region)
    manipulation.validate_goal(goal)

    async def observe():
        context.stage("observe", None)
        return await scene.observe()

    async def execute(plan, stages):
        for stage in stages:
            context.stage(stage, {"plan_id": plan.id})
            await manipulation.execute(plan, stage)

    async def verify(plan, criterion):
        observation = await observe()
        result = await verification.verify(plan, criterion, observation)
        if result.observation_id != observation.id:
            raise HarnessError(
                "verification_unknown", "Verification references another observation"
            )
        context.stage("verify_" + criterion, result.model_dump())
        return result

    attempts = 0
    while attempts <= args.grasp_retries:
        attempts += 1
        before = await observe()
        plan = await manipulation.plan(goal, before)
        if plan.observation_id != before.id:
            raise HarnessError("stale_plan", "Plan references another observation")
        context.stage("plan", {**plan.model_dump(), "attempt": attempts})
        await execute(plan, ("approach", "grasp", "lift"))
        grasp = await verify(plan, "grasp")
        if grasp.outcome == "passed":
            break
        if grasp.outcome == "unknown":
            raise HarnessError(
                "verification_unknown", "Grasp outcome is unknown", details=grasp.model_dump()
            )
        if not grasp.retryable or attempts > args.grasp_retries:
            raise HarnessError(
                "grasp_failed",
                "Grasp failed; bounded recovery unavailable",
                details={
                    **grasp.model_dump(),
                    "attempts": attempts,
                    "allowed_next": ["robot_observe", "robot_hold"],
                    "assistance": "external_agent_or_operator",
                },
            )
        context.stage(
            "recover",
            {
                "observation_id": grasp.observation_id,
                "reason": grasp.reason,
                "next": "observe_and_replan",
            },
        )

    await execute(plan, ("transport", "place", "release", "withdraw"))
    placement = await verify(plan, "placement")
    if placement.outcome != "passed":
        raise HarnessError(
            "verification_unknown",
            "Placement success was not established",
            details=placement.model_dump(),
        )
    context.stage("verified", placement.model_dump())
    return {
        "verified": True,
        "qualification": placement.qualification,
        "attempts": attempts,
        "object_id": args.object_id,
        "target_region": args.target_region,
        "observation_id": placement.observation_id,
        "verification_method": placement.method,
        "plan_id": plan.id,
        "skill_version": PICK_PLACE_VERSION,
    }


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
    from ..runtime import Runtime

    profile = profile.model_copy(update={"active_task_id": task_id})
    async with Runtime(profile) as runtime:
        return await run_recipe(runtime, task_id, parameters)
