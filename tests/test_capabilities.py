"""Portability is exercised against opaque references, without robot scene dictionaries."""

import ast
from pathlib import Path

import pytest

from robot_rsi.capabilities import (
    PICK_PLACE_REQUIREMENTS,
    Capability,
    ObservationRef,
    PlanRef,
    SkillBindings,
    SkillContext,
    Verification,
)
from robot_rsi.contracts import HarnessError
from robot_rsi.runtime import Runtime
from robot_rsi.skill_adapters import RobotScene
from robot_rsi.skill_models import PickPlaceParameters
from robot_rsi.skills import PICK_PLACE_VERSION, pick_place


class OpaqueBench:
    """A contract test double, NOT a second supported physical robot.

    This backend has no poses, gripper opening, camera role or mock object dictionary.
    """

    def __init__(
        self,
        *,
        grasp_outcomes=("passed",),
        retryable=True,
        version=1,
        wrong_plan_observation=False,
        wrong_verification_observation=False,
    ):
        self.capabilities = tuple(
            Capability(
                name=name,
                contract_version=version if name == "outcome.verify" else 1,
                provider_id="opaque_bench",
                implementation_version="fixture_1",
                qualification="contract_test_double",
            )
            for name in PICK_PLACE_REQUIREMENTS
        )
        self.observations = 0
        self.plans = []
        self.executed = []
        self.grasp_outcomes = iter(grasp_outcomes)
        self.retryable = retryable
        self.wrong_plan_observation = wrong_plan_observation
        self.wrong_verification_observation = wrong_verification_observation

    async def observe(self):
        self.observations += 1
        return ObservationRef(
            id=f"capture_{self.observations}", qualification="contract_test_double"
        )

    def validate_goal(self, goal):
        if goal.arm != "toolhead_7":
            raise HarnessError("unsupported_arm", "This fixture has one named effector")

    async def plan(self, goal, observation):
        self.validate_goal(goal)
        plan = PlanRef(
            id=f"opaque_plan_{len(self.plans)}",
            provider_id="opaque_bench",
            observation_id="old_capture" if self.wrong_plan_observation else observation.id,
            frame_id="bench_fixture_frame",
            qualification="contract_test_double",
        )
        self.plans.append(plan)
        return plan

    async def execute(self, plan, stage):
        assert plan in self.plans
        self.executed.append((plan.id, stage))

    async def verify(self, plan, criterion, observation):
        outcome = next(self.grasp_outcomes) if criterion == "grasp" else "passed"
        return Verification(
            outcome=outcome,
            observation_id="old_capture" if self.wrong_verification_observation else observation.id,
            method="opaque_fixture_sensor",
            qualification="contract_test_double",
            retryable=self.retryable if outcome != "passed" else False,
        )


def request():
    return PickPlaceParameters(
        arm="toolhead_7",
        object_id="workpiece",
        target_region="output_tray",
    )


def context(bench):
    return SkillContext(SkillBindings(bench, bench, bench), lambda *_: None)


async def test_same_skill_runs_on_non_yam_shaped_backend():
    bench = OpaqueBench()
    result = await pick_place(context(bench), request())
    assert result["verified"]
    assert result["qualification"] == "contract_test_double"
    assert result["verification_method"] == "opaque_fixture_sensor"
    assert result["skill_version"] == PICK_PLACE_VERSION
    assert [stage for _, stage in bench.executed] == [
        "approach",
        "grasp",
        "lift",
        "transport",
        "place",
        "release",
        "withdraw",
    ]


async def test_retry_reobserves_and_replans_on_backend_that_allows_recovery():
    bench = OpaqueBench(grasp_outcomes=("failed", "passed"))
    result = await pick_place(context(bench), request())
    assert result["attempts"] == 2
    assert len(bench.plans) == 2
    assert bench.plans[0].observation_id != bench.plans[1].observation_id
    assert bench.executed[-1][0] == bench.plans[1].id


@pytest.mark.parametrize(
    "outcome,retryable,error",
    [
        ("unknown", True, "verification_unknown"),
        ("failed", False, "grasp_failed"),
    ],
)
async def test_unknown_or_nonrecoverable_grasp_does_not_retry_or_release(outcome, retryable, error):
    bench = OpaqueBench(grasp_outcomes=(outcome,), retryable=retryable)
    with pytest.raises(HarnessError) as caught:
        await pick_place(context(bench), request())
    assert caught.value.code == error
    assert len(bench.plans) == 1
    assert [stage for _, stage in bench.executed] == ["approach", "grasp", "lift"]


async def test_capability_version_mismatch_fails_before_observation_or_motion():
    bench = OpaqueBench(version=2)
    with pytest.raises(HarnessError) as caught:
        await pick_place(context(bench), request())
    assert caught.value.code == "unsupported"
    assert caught.value.details["missing_capabilities"] == [
        {"name": "outcome.verify", "required_version": 1, "provided_version": 2}
    ]
    assert bench.observations == 0 and not bench.executed


@pytest.mark.parametrize("method", ["observe", "plan", "execute", "verify"])
async def test_manifest_without_callable_port_is_not_an_implemented_capability(method):
    bench = OpaqueBench()
    setattr(bench, method, None)
    with pytest.raises(HarnessError) as caught:
        await pick_place(context(bench), request())
    assert caught.value.code == "unsupported"
    assert bench.observations == 0 and not bench.executed


async def test_missing_port_cannot_be_satisfied_by_unrelated_manifest():
    bench = OpaqueBench()
    bindings = SkillBindings(bench)  # scene advertises all names but execution port is absent
    with pytest.raises(HarnessError, match="missing"):
        await pick_place(SkillContext(bindings, lambda *_: None), request())
    assert bench.observations == 0 and not bench.executed


@pytest.mark.parametrize(
    "field,error",
    [
        ("wrong_plan_observation", "stale_plan"),
        ("wrong_verification_observation", "verification_unknown"),
    ],
)
async def test_stale_provider_results_cannot_become_success(field, error):
    bench = OpaqueBench(**{field: True})
    with pytest.raises(HarnessError) as caught:
        await pick_place(context(bench), request())
    assert caught.value.code == error
    assert all(stage != "release" for _, stage in bench.executed)


async def test_runtime_discovery_and_admission_share_requirements(profile):
    def scene_only(robot, store, observe):
        return SkillBindings(RobotScene(robot, observe))

    async with Runtime(profile, bindings_factory=scene_only) as runtime:
        station = await runtime.invoke("station_describe")
        assert not station["result"]["supported_tasks"]
        catalog = await runtime.invoke("capabilities_list")
        assert catalog["result"]["skills"][0]["missing_capabilities"]
        result = await runtime.invoke("task_run", {"request_id": "missing-capability"})
        assert result["error"]["code"] == "unsupported"
        assert not runtime.robot.commands
        assert not runtime.store.all("observation")
        assert not runtime.store.all("operation")


async def test_bad_host_binding_does_not_leave_scope_locked(profile):
    def broken_binding(*_):
        raise ValueError("Invalid host binding")

    with pytest.raises(ValueError) as caught:
        Runtime(profile, bindings_factory=broken_binding)
    assert caught.value.args == ("Invalid host binding",)
    async with Runtime(profile) as runtime:
        assert runtime.bindings.catalog()


async def test_mock_plan_is_invalid_after_hold(profile):
    async with Runtime(profile) as runtime:
        scene = await runtime.bindings.scene.observe()
        from robot_rsi.capabilities import PickPlaceGoal

        plan = await runtime.bindings.manipulation.plan(
            PickPlaceGoal(arm="left", object_id="red_block", target_region="blue_box"), scene
        )
        await runtime.operations.hold()
        with pytest.raises(HarnessError) as caught:
            await runtime.bindings.manipulation.execute(plan, "approach")
        assert caught.value.code == "stale_plan"
        assert not runtime.robot.commands


async def test_roles_frames_and_binding_evidence(profile):
    async with Runtime(profile) as runtime:
        unsupported = await runtime.invoke(
            "task_run", {"request_id": "bad-role", "parameters": {"arm": "other_effector"}}
        )
        assert unsupported["error"]["code"] == "unsupported_arm"
        assert not runtime.store.all("observation")
        obs = await runtime.observe("top")
        frame = await runtime.invoke(
            "robot_move",
            {
                "request_id": "bad-frame",
                "observation_id": obs["id"],
                "position_m": [0.3, 0, 0.1],
                "frame_id": "unregistered_base",
            },
        )
        assert frame["error"]["code"] == "unsupported_frame"
        accepted = await runtime.invoke("task_run", {"request_id": "good-role"})
        result = await runtime.operations.get(accepted["result"]["id"], 2)
        assert (
            result["skill_name"] == "pick_place" and result["skill_version"] == PICK_PLACE_VERSION
        )
        assert result["capabilities"] == runtime.bindings.catalog()
        assert (
            runtime.store.get("plan", result["result"]["plan_id"])["provider_id"]
            == "mock_manipulation"
        )


def test_reusable_skill_imports_only_contracts():
    # Architectural boundary, so a future device shortcut fails independently of happy-path tests.
    path = Path(__file__).parents[1] / "src/robot_rsi/skills.py"
    modules = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    assert set(modules) <= {
        "__future__",
        "capabilities",
        "contracts",
        "skill_models",
        "json",
        "importlib.resources",
    }
