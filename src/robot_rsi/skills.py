"""Reusable task logic. Concrete geometry, devices and deployment stay in providers."""

from __future__ import annotations

import json
from importlib.resources import files

from .capabilities import PICK_PLACE_REQUIREMENTS, PickPlaceGoal, SkillContext
from .contracts import HarnessError, PickPlace
from .skill_models import PickPlaceParameters

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
