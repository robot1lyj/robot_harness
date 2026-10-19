"""Built-in implementations of the common skill contracts.

All synthetic geometry and scene dictionaries stay here, outside reusable skills.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .capabilities import (
    Capability,
    Criterion,
    ObservationRef,
    PickPlaceGoal,
    PlanRef,
    SkillBindings,
    Stage,
    Verification,
)
from .contracts import HarnessError
from .storage import Store, uid


class RobotScene:
    def __init__(self, robot, observe):
        self.robot, self._observe = robot, observe
        self.capabilities = (
            Capability(
                name="scene.observe",
                contract_version=1,
                provider_id=robot.provider_id,
                implementation_version="0.1.0",
                qualification=robot.qualification,
            ),
        )

    async def observe(self):
        result = await self._observe()
        return ObservationRef(id=result["id"], qualification=result["qualification"])

    async def snapshot(self, camera):
        return await self._observe(camera)


@dataclass
class _MockPlan:
    reference: PlanRef
    goal: PickPlaceGoal
    source: list
    destination: list
    source_hover: list
    destination_hover: list
    radius: float
    epoch: int


class MockManipulation:
    def __init__(self, robot, store: Store):
        self.robot, self.store = robot, store
        self.plans: dict[str, _MockPlan] = {}
        self.capabilities = tuple(
            Capability(
                name=name,
                contract_version=1,
                provider_id="mock_manipulation",
                implementation_version="0.1.0",
                qualification="synthetic",
            )
            for name in (
                "manipulation.plan_pick_place",
                "manipulation.execute_stage",
                "outcome.verify",
            )
        )

    def validate_goal(self, goal: PickPlaceGoal):
        self.robot.validate_arm(goal.arm)

    async def plan(self, goal: PickPlaceGoal, observation: ObservationRef):
        # HOLD invalidates old plans. Their immutable evidence is already in Store.
        self.plans = {key: value for key, value in self.plans.items()
                      if value.epoch == self.robot.epoch}
        self.validate_goal(goal)
        record = self.store.get("observation", observation.id)
        self.robot.validate_observation(record)
        state = record["state"]
        if goal.object_id not in state["objects"] or goal.target_region not in state["regions"]:
            raise HarnessError("target_missing", "Object or region absent from observed scene")
        if any(state["holding"].values()):
            raise HarnessError("object_already_held", "Resolve the held object before a new pick")
        if not state["objects"][goal.object_id]["supported"]:
            raise HarnessError("verification_unknown", "Object support is unknown")
        target = state["regions"][goal.target_region]
        source = list(state["objects"][goal.object_id]["position_m"])
        destination = list(target["position_m"])
        hover_z = max(source[2], destination[2]) + 0.12
        source_hover = [source[0], source[1], hover_z]
        destination_hover = [destination[0], destination[1], hover_z]
        for pose in (source, destination, source_hover, destination_hover):
            self.robot.validate_position(pose)
        reference = PlanRef(
            id=uid("plan"),
            provider_id="mock_manipulation",
            observation_id=observation.id,
            frame_id=state["frame_id"],
            qualification="synthetic",
        )
        self.plans[reference.id] = _MockPlan(
            reference,
            goal,
            source,
            destination,
            source_hover,
            destination_hover,
            target["radius_m"],
            self.robot.epoch,
        )
        self.store.put(
            "plan",
            reference.id,
            {
                **reference.model_dump(),
                "goal": goal.model_dump(),
                "geometry_qualification": "synthetic_bounds_only",
                "source": source,
                "destination": destination,
                "source_hover": source_hover,
                "destination_hover": destination_hover,
            },
        )
        return reference

    def _resolve(self, reference: PlanRef):
        plan = self.plans.get(reference.id)
        if plan is None or plan.reference != reference:
            raise HarnessError("invalid_plan", "Plan is not owned by this provider instance")
        if plan.epoch != self.robot.epoch:
            raise HarnessError("stale_plan", "Plan was invalidated by hold or ownership change")
        return plan

    async def execute(self, reference: PlanRef, stage: Stage):
        plan = self._resolve(reference)
        arm = plan.goal.arm
        if stage == "approach":
            await self.robot.gripper(arm, 1)
            await self.robot.move(arm, plan.source_hover)
            await self.robot.move(arm, plan.source)
        elif stage == "grasp":
            await self.robot.gripper(arm, 0)
        elif stage == "lift":
            await self.robot.move(arm, plan.source_hover)
        elif stage in ("transport", "withdraw"):
            await self.robot.move(arm, plan.destination_hover)
        elif stage == "place":
            await self.robot.move(arm, plan.destination)
        elif stage == "release":
            await self.robot.gripper(arm, 1)
        else:
            raise HarnessError("unsupported_stage", "Unknown manipulation stage")

    async def verify(self, reference: PlanRef, criterion: Criterion, observation: ObservationRef):
        plan = self._resolve(reference)
        record = self.store.get("observation", observation.id)
        self.robot.validate_observation(record)
        state, goal = record["state"], plan.goal
        if criterion == "grasp":
            passed = state["holding"][goal.arm] == goal.object_id
            retryable = not passed and all(value is None for value in state["holding"].values())
        elif criterion == "placement":
            obj = state["objects"].get(goal.object_id)
            if obj is None:
                return Verification(
                    outcome="unknown",
                    observation_id=observation.id,
                    method="mock_scene_state",
                    qualification="synthetic",
                    reason="Object missing",
                )
            passed = bool(
                obj["supported"]
                and state["holding"][goal.arm] is None
                and np.linalg.norm(np.array(obj["position_m"]) - plan.destination) <= plan.radius
            )
            retryable = False
        else:
            raise HarnessError("unsupported_criterion", "Unknown verification criterion")
        return Verification(
            outcome="passed" if passed else "failed",
            observation_id=observation.id,
            method="mock_scene_state",
            qualification="synthetic",
            retryable=retryable,
            reason=None if passed else "Observed outcome did not meet criterion",
        )


def built_in_bindings(robot, store: Store, observe) -> SkillBindings:
    scene = RobotScene(robot, observe)
    if robot.provider_id == "mock_robot":
        manipulation = MockManipulation(robot, store)
        return SkillBindings(scene, manipulation, manipulation)
    return SkillBindings(scene)
