"""Generic installed factory and optional resource contracts; all effects are synthetic."""

import asyncio
import json
import math
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from robot_rsi.adapters import config_check, factories
from robot_rsi.contracts import Profile
from robot_rsi.resources import CommandFeedback, RobotCommand, Transform
from robot_rsi.runtime import Runtime

fixture = pytest.importorskip("synthetic_adapter")

REF = {"id": "fixture_model", "version": "v1"}
Q = {"carriage_x": 0.2, "carriage_y": 0.3, "turntable": math.pi / 2}
FK = {
    "model_ref": REF,
    "arm": "bridge_3",
    "joints": Q,
    "base_frame": "world",
    "target_frame": "tool_tip",
}


def configured(profile, **options):
    return profile.model_copy(
        update={
            "robot": None,
            "host": Profile(
                host={
                    "adapter": {"factory": "synthetic_fixture", "options": options},
                    "model_ref": REF,
                    "calibration_refs": [{"id": "fixture_extrinsics", "version": "v1"}],
                }
            ).host,
        }
    )


async def call(runtime, name, **args):
    result = await runtime.invoke(name, args)
    assert result["ok"], result
    return result["result"]


def evidence(name, **body):
    if root := os.environ.get("C02_EVIDENCE_ROOT"):
        path = Path(root)
        path.mkdir(parents=True, exist_ok=True)
        (path / (name + ".json")).write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n")


async def test_factory_discovery_config_and_shared_lifecycle_do_not_connect(profile, monkeypatch):
    profile = configured(profile)
    initial = fixture.FACTORY_CALLS
    assert "synthetic_fixture" in factories()
    assert config_check(profile)["connected"] is False
    assert fixture.FACTORY_CALLS == initial
    async with Runtime(profile) as runtime:
        owner = runtime.robot
        assert fixture.FACTORY_CALLS == initial + 1 and owner.connect_count == 0
        for _ in range(5):
            await call(runtime, "capabilities_list")
            await call(runtime, "station_describe")
        assert fixture.FACTORY_CALLS == initial + 1 and owner.connect_count == 0
        before = await call(runtime, "resources_list")
        await call(runtime, "resource_connect", resource_id="bench")
        await call(runtime, "resource_connect", resource_id="sensor")
        assert owner.connect_count == 1
        after = await call(runtime, "resources_list")
        assert runtime.cameras.connection is owner
    assert owner.close_count == 1
    evidence(
        "factory",
        before=before,
        after=after,
        connects=owner.connect_count,
        closes=owner.close_count,
        configured_factory_calls=fixture.FACTORY_CALLS - initial,
        model_calls=0,
    )


@pytest.mark.parametrize(
    "mode,options,ports",
    [
        ("robot_only", {"camera": False}, (True, False, True)),
        (
            "camera_only",
            {"robot": False, "kinematics": False, "shared_owner": False},
            (False, True, False),
        ),
        (
            "compute_only",
            {"robot": False, "camera": False, "shared_owner": False},
            (False, False, True),
        ),
    ],
)
async def test_independent_compositions_and_missing_skill_capabilities(
    profile, mode, options, ports
):
    async with Runtime(configured(profile, **options)) as runtime:
        assert (
            runtime.robot is not None,
            runtime.cameras is not None,
            runtime.bindings.kinematics is not None,
        ) == ports
        metadata = await call(runtime, "capabilities_list")
        assert not runtime.store.all("observation")
        missing = await runtime.invoke("task_run", {"request_id": "grasp-requires-scene"})
        assert missing["error"]["code"] == "unsupported"
        assert not runtime.store.all("operation")
        result = {}
        if ports[0]:
            await call(runtime, "resource_connect", resource_id="bench")
            result["state"] = await call(runtime, "robot_state", resource_id="bench")
            assert set(result["state"]["joints"]) == set(Q) and set(result["state"]["tools"]) == {
                "vacuum"
            }
        if ports[1]:
            await call(runtime, "resource_connect", resource_id="sensor")
            result["camera"] = await call(
                runtime, "camera_capture", camera="inspection", require="preview"
            )
            assert (
                result["camera"]["frames"][0]["capture_time"]["value_s"]
                <= result["camera"]["received_at"]
            )
        if ports[2]:
            result["fk"] = await call(runtime, "kinematics_fk", **FK)
        result["workspace"] = await call(runtime, "workspace_read")
        evidence(
            mode,
            ports=ports,
            metadata=metadata,
            results=result,
            robot_is_none=runtime.robot is None,
        )


async def test_explicit_connection_and_command_ack_measured_feedback_dedup(profile):
    async with Runtime(configured(profile, delay_s=0.03)) as runtime:
        not_connected = await runtime.invoke("robot_state", {"resource_id": "bench"})
        assert not_connected["error"]["code"] == "resource_not_connected"
        await call(runtime, "resource_connect", resource_id="bench")
        state = await call(runtime, "robot_state", resource_id="bench")
        args = {
            "resource_id": "bench",
            "request_id": "named-command",
            "observation_id": state["id"],
            "model_ref": REF,
            "arm": "bridge_3",
            "trajectory": [{"time_from_start_s": 0.01, "positions": Q}],
        }
        accepted = await call(runtime, "robot_command", **args)
        await asyncio.sleep(0.005)
        running = await call(runtime, "operation_get", operation_id=accepted["id"])
        assert running["status"] == "running" and running["stage"] == "submitted"
        receipt = runtime.store.all("command")[0]
        assert receipt["status"] == "accepted" and running["result"] is None
        done = await call(runtime, "operation_get", operation_id=accepted["id"], wait_s=2)
        assert done["status"] == "succeeded" and done["result"]["feedback"]["state"]["joints"] == Q
        assert done["result"]["verified"] is False and done["hold"]["confirmed"]
        repeated = await call(runtime, "robot_command", **args)
        assert repeated["id"] == accepted["id"] and len(runtime.robot.commands) == 1
        fresh = await call(runtime, "robot_state", resource_id="bench")
        tool = await call(
            runtime,
            "robot_command",
            resource_id="bench",
            request_id="pressure",
            observation_id=fresh["id"],
            model_ref=REF,
            arm="bridge_3",
            effector={"tool_id": "vacuum", "command": "pressure", "value": 80000, "unit": "Pa"},
        )
        tool_done = await call(runtime, "operation_get", operation_id=tool["id"], wait_s=2)
        assert tool_done["result"]["feedback"]["state"]["tools"]["vacuum"] == 80000
        evidence(
            "commands",
            state=state,
            accepted=accepted,
            running=running,
            receipt=receipt,
            done=done,
            tool=tool_done,
        )


@pytest.mark.parametrize(
    "change,code",
    [
        ({"arm": "left"}, "unsupported_arm"),
        (
            {"trajectory": [{"time_from_start_s": 0.01, "positions": {"wrong": 0.2}}]},
            "joint_mapping",
        ),
        (
            {"trajectory": [{"time_from_start_s": 0.01, "positions": {**Q, "carriage_x": 2}}]},
            "joint_limit",
        ),
        ({"model_ref": {"id": "fixture_model", "version": "v2"}}, "model_mismatch"),
        (
            {
                "trajectory": [],
                "effector": {
                    "tool_id": "vacuum",
                    "command": "pressure",
                    "value": 0.5,
                    "unit": "fraction",
                },
            },
            "invalid_tool_command",
        ),
    ],
)
async def test_invalid_named_command_never_submits(profile, change, code):
    async with Runtime(configured(profile)) as runtime:
        await call(runtime, "resource_connect", resource_id="bench")
        state = await call(runtime, "robot_state", resource_id="bench")
        args = {
            "resource_id": "bench",
            "request_id": "invalid",
            "observation_id": state["id"],
            "model_ref": REF,
            "arm": "bridge_3",
            "trajectory": [{"time_from_start_s": 0.01, "positions": Q}],
            **change,
        }
        result = await runtime.invoke("robot_command", args)
        assert result["error"]["code"] == code and not runtime.robot.commands
        assert not runtime.store.all("operation")


async def test_cancel_unknown_and_restart_do_not_replay(profile):
    profile = configured(profile, delay_s=0.2)
    async with Runtime(profile) as runtime:
        await call(runtime, "resource_connect", resource_id="bench")
        state = await call(runtime, "robot_state", resource_id="bench")
        args = {
            "resource_id": "bench",
            "request_id": "cancel",
            "observation_id": state["id"],
            "model_ref": REF,
            "arm": "bridge_3",
            "trajectory": [{"time_from_start_s": 0.01, "positions": Q}],
        }
        accepted = await call(runtime, "robot_command", **args)
        await asyncio.sleep(0.01)
        cancelled = await call(runtime, "operation_cancel", operation_id=accepted["id"])
        assert cancelled["status"] == "cancelled" and cancelled["hold"]["confirmed"]
        assert (await call(runtime, "robot_command", **args))["id"] == accepted["id"]
        assert len(runtime.robot.commands) == 1
    broken = configured(profile, disconnect_after_submit=True)
    async with Runtime(broken) as runtime:
        await call(runtime, "resource_connect", resource_id="bench")
        state = await call(runtime, "robot_state", resource_id="bench")
        args.update(request_id="lost", observation_id=state["id"])
        accepted = await call(runtime, "robot_command", **args)
        unknown = await call(runtime, "operation_get", operation_id=accepted["id"], wait_s=2)
        assert unknown["status"] == "unknown" and runtime.operations.blocked
        evidence("unknown", cancelled=cancelled, unknown=unknown)
    async with Runtime(broken) as runtime:
        assert runtime.store.get("operation", accepted["id"])["status"] == "unknown"
        assert runtime.robot.commands == [] and runtime.robot.connect_count == 0
        assert (await call(runtime, "robot_command", **args))["id"] == accepted["id"]
        assert runtime.robot.commands == []


async def test_fk_ik_known_solutions_constraints_and_optional_port(profile):
    async with Runtime(
        configured(profile, robot=False, camera=False, shared_owner=False)
    ) as runtime:
        pose = await call(runtime, "kinematics_fk", **FK)
        assert (
            pose["pose"]["target_frame"] == "world" and pose["pose"]["source_frame"] == "tool_tip"
        )
        assert abs(pose["pose"]["matrix"][1][0] - 1) < 1e-10
        request = {"model_ref": REF, "arm": "bridge_3", "target": pose["pose"], "seed": Q}
        solution = await call(runtime, "kinematics_ik", **request)
        assert solution["status"] == "solved" and solution["joints"] == Q
        impossible = {
            **pose["pose"],
            "matrix": [[1, 0, 0, 2], [0, 1, 0, 0.2], [0, 0, 1, 0.25], [0, 0, 0, 1]],
        }
        no_solution = await call(runtime, "kinematics_ik", **{**request, "target": impossible})
        assert no_solution["status"] == "no_solution" and no_solution["joints"] is None
        constrained = await call(
            runtime, "kinematics_ik", **request, joint_constraints={"carriage_x": (0.6, 0.7)}
        )
        assert constrained["status"] == "no_solution"
        rotated = fixture.transform("world", "tool_tip", 0.2, 0.3, 0.25).model_dump()
        a = 0.3
        c, s = math.cos(a), math.sin(a)
        rotated["matrix"] = ((1, 0, 0, 0.2), (0, c, -s, 0.3), (0, s, c, 0.25), (0, 0, 0, 1))
        not_converged = await call(runtime, "kinematics_ik", **{**request, "target": rotated})
        assert not_converged["status"] == "not_converged"
        assert runtime.robot is None and not runtime.store.all("operation")
        evidence(
            "kinematics", fk=pose, ik=solution, no_solution=no_solution, not_converged=not_converged
        )
    async with Runtime(
        configured(profile, robot=False, camera=False, kinematics=False, shared_owner=False)
    ) as runtime:
        result = await runtime.invoke("kinematics_fk", FK)
        assert result["error"]["code"] == "unsupported"


async def test_calibration_applicability_and_task_version_pins(profile):
    profile = configured(profile, robot=False, camera=False, shared_owner=False)
    query = {
        "reference": {"id": "fixture_extrinsics", "version": "v1"},
        "resource_id": "sensor",
        "model_ref": REF,
        "stream_id": "rectified_preview",
        "resolution": (16, 12),
        "target_frame": "world",
        "source_frame": "fixture_optical",
    }
    async with Runtime(profile) as runtime:
        await call(runtime, "task_begin", task_id="old", goal="检查模型标定")
        valid = await call(runtime, "calibration_get", **query)
        assert valid["applicable"] and valid["record"]["transform"]["matrix"][2][3] == 0.1
        mismatches = []
        for field, value in [
            ("model_ref", {"id": "fixture_model", "version": "v2"}),
            ("stream_id", "raw"),
            ("resolution", (20, 20)),
            ("source_frame", "wrong"),
        ]:
            result = await call(runtime, "calibration_get", **{**query, field: value})
            assert not result["applicable"] and result["reasons"]
            mismatches.append(result)
        missing = await runtime.invoke(
            "calibration_get",
            {**query, "reference": {"id": "fixture_extrinsics", "version": "absent"}},
        )
        assert missing["error"]["code"] == "not_found"
    updated = profile.model_copy(deep=True)
    updated.host.model_ref.version = "v2"
    updated.host.calibration_refs[0].version = "v2"
    async with Runtime(updated) as runtime:
        await call(runtime, "task_begin", task_id="new", goal="检查新模型标定")
        old = await runtime.invoke("calibration_get", query, task_id="old")
        assert old["ok"] and old["result"]["applicable"]
        new_query = {
            **query,
            "reference": {"id": "fixture_extrinsics", "version": "v2"},
            "model_ref": {"id": "fixture_model", "version": "v2"},
        }
        wrong = await runtime.invoke("calibration_get", new_query, task_id="old")
        assert wrong["error"]["code"] == "task_version_mismatch"
        new = await runtime.invoke("calibration_get", new_query, task_id="new")
        assert new["ok"] and new["result"]["record"]["transform"]["matrix"][2][3] == 0.2
        old_fk = await runtime.invoke("kinematics_fk", FK, task_id="old")
        new_fk = await runtime.invoke(
            "kinematics_fk",
            {**FK, "model_ref": {"id": "fixture_model", "version": "v2"}},
            task_id="new",
        )
        assert (
            old_fk["result"]["pose"]["matrix"][2][3] == 0.25
            and new_fk["result"]["pose"]["matrix"][2][3] == 0.30
        )
        context = await call(runtime, "task_context", task_id="old")
        assert context["checkpoint"]["resource_versions"]["model"] == REF
        assert "matrix" not in json.dumps(context)
        evidence(
            "calibrations",
            valid=valid,
            mismatches=mismatches,
            old=old,
            new=new,
            old_fk=old_fk,
            new_fk=new_fk,
            context=context,
        )


async def test_compute_skill_uses_pinned_model_without_robot(profile):
    async with Runtime(
        configured(profile, robot=False, camera=False, shared_owner=False)
    ) as runtime:
        runtime.registry.register(
            Path(__file__).parents[1] / "examples/synthetic_adapter/skills/check_fk"
        )
        runtime.registry.enable("fixture_fk", "1.0.0")
        await call(runtime, "task_begin", task_id="compute", goal="计算 FK")
        op = await call(
            runtime,
            "task_run",
            request_id="compute",
            task_id="compute",
            task_epoch=0,
            skill="fixture_fk",
        )
        done = await call(runtime, "operation_get", operation_id=op["id"], wait_s=2)
        assert done["status"] == "succeeded" and done["resources"] == [] and done["hold"] is None
        assert runtime.robot is None and not runtime.store.all("observation")
        assert done["result"]["model_ref"] == REF
        evidence("compute-skill", operation=done)


def test_receipt_cannot_be_completed_and_invalid_transforms_fail():
    with pytest.raises(ValidationError):
        CommandFeedback(
            command_id="c",
            operation_id="o",
            status="completed",
            source="ack",
            qualification="synthetic",
        )
    with pytest.raises(ValidationError):
        Transform(
            target_frame="a",
            source_frame="b",
            matrix=((2, 0, 0, 0), (0, 1, 0, 0), (0, 0, 1, 0), (0, 0, 0, 1)),
        )
    with pytest.raises(ValidationError):
        RobotCommand(
            resource_id="bench",
            request_id="x",
            observation_id="s",
            model_ref=REF,
            arm="bridge_3",
            trajectory=[
                {"time_from_start_s": 2, "positions": Q},
                {"time_from_start_s": 1, "positions": Q},
            ],
        )


async def test_builtin_mock_without_camera_and_injected_camera_preserved(profile):
    p = profile.model_copy(deep=True)
    p.host.cameras = None
    async with Runtime(p) as runtime:
        accepted = await call(runtime, "task_run", request_id="no-camera")
        done = await call(runtime, "operation_get", operation_id=accepted["id"], wait_s=2)
        assert done["status"] == "succeeded" and done["result"]["verified"]
        assert runtime.cameras is None and not runtime.store.all("camera_observation")
        evidence("mock-no-camera", operation=done)
    from robot_rsi.capabilities import SkillBindings
    from robot_rsi.skill_adapters import RobotScene

    external = {}

    def inject(robot, store, observe):
        camera = fixture.Camera(store, fixture.Connection())
        external["camera"] = camera
        return SkillBindings(RobotScene(robot, observe), camera=camera)

    p.namespace = "injected"
    async with Runtime(p, bindings_factory=inject) as runtime:
        assert (
            runtime.cameras is external["camera"] and runtime.bindings.camera is external["camera"]
        )
        assert (await call(runtime, "camera_describe"))["default_camera"] == "inspection"


async def test_state_clock_and_epoch_are_admission_facts(profile):
    async with Runtime(configured(profile)) as runtime:
        await call(runtime, "resource_connect", resource_id="bench")
        state = await call(runtime, "robot_state", resource_id="bench")
        args = {
            "resource_id": "bench",
            "request_id": "epoch",
            "observation_id": state["id"],
            "model_ref": REF,
            "arm": "bridge_3",
            "trajectory": [{"time_from_start_s": 0.01, "positions": Q}],
        }
        await runtime.operations.hold()
        stale = await runtime.invoke("robot_command", args)
        assert stale["error"]["code"] == "stale_observation" and not runtime.robot.commands
        state = await call(runtime, "robot_state", resource_id="bench")
        state["capture_time"].update(
            kind="device_monotonic", clock_domain="device_clock", uncertainty_s=None
        )
        runtime.store.put("robot_state", state["id"], state)
        args.update(request_id="clock", observation_id=state["id"])
        clock = await runtime.invoke("robot_command", args)
        assert clock["error"]["code"] == "freshness_unknown" and not runtime.robot.commands
        definition = await call(runtime, "model_get", reference=REF)
        assert [j["kind"] for j in definition["joints"]] == ["prismatic", "prismatic", "revolute"]
        assert definition["tools"][0]["unit"] == "Pa"
        evidence("state-admission", stale=stale, clock=clock, definition=definition)


async def test_immutable_asset_versions_and_failed_construction_release_scope(profile, monkeypatch):
    p = configured(profile, robot=False, camera=False, shared_owner=False)
    async with Runtime(p):
        pass
    original = fixture.model

    def changed(version):
        value = original(version)
        value.source = "changed without new version"
        return value

    monkeypatch.setattr(fixture, "model", changed)
    from robot_rsi.contracts import HarnessError

    with pytest.raises(HarnessError) as failure:
        Runtime(p)
    assert failure.value.code == "version_conflict"
    monkeypatch.setattr(fixture, "model", original)
    async with Runtime(p) as runtime:
        assert (await call(runtime, "model_get", reference=REF))["source"] == original("v1").source


class AsyncIOOwner:
    def __init__(self, fail_first=False):
        self.open = False
        self.connect_calls = self.close_calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.fail_first = fail_first

    async def connect(self):
        self.connect_calls += 1
        self.entered.set()
        await self.release.wait()
        if self.fail_first and self.connect_calls == 1:
            raise RuntimeError("fixture connection failed")
        self.open = True

    async def close(self):
        self.close_calls += 1
        await asyncio.sleep(0)
        self.open = False


def async_io_adapter(owner):
    from robot_rsi.adapters import Adapter
    from robot_rsi.capabilities import SkillBindings
    from robot_rsi.resources import Resource

    return Adapter(
        SkillBindings(None),
        tuple(
            Resource(
                resource_id=name,
                kind="camera",
                roles=[name],
                provider="io_fixture",
                implementation_version="1",
                capabilities=[],
                qualification="synthetic",
                owner_id=name,
            )
            for name in ("arm_view", "overview")
        ),
        lifecycles={"arm_view": owner, "overview": owner},
    )


async def test_async_shared_owner_connect_is_deduplicated_across_await():
    owner = AsyncIOOwner()
    adapter = async_io_adapter(owner)
    pending = [asyncio.create_task(adapter.connect(name)) for name in ("arm_view", "overview")]
    await owner.entered.wait()
    await asyncio.sleep(0)
    assert owner.connect_calls == 1
    assert not adapter.connected
    owner.release.set()
    results = await asyncio.gather(*pending)
    assert all(result["connected"] for result in results)
    assert owner.connect_calls == len(adapter.connected) == 1
    await asyncio.gather(adapter.close(), adapter.close())
    assert owner.close_calls == 1 and not owner.open and not adapter.connected
    evidence("async-shared-owner", connects=owner.connect_calls, closes=owner.close_calls)


async def test_async_close_waits_for_connect_and_rejects_later_connections():
    from robot_rsi.contracts import HarnessError

    owner = AsyncIOOwner()
    adapter = async_io_adapter(owner)
    connecting = asyncio.create_task(adapter.connect("arm_view"))
    await owner.entered.wait()
    closing = asyncio.create_task(adapter.close())
    await asyncio.sleep(0)
    again = asyncio.create_task(adapter.close())
    later = asyncio.create_task(adapter.connect("overview"))
    await asyncio.sleep(0)
    assert not closing.done() and not again.done() and owner.close_calls == 0
    owner.release.set()
    results = await asyncio.gather(connecting, closing, again, later, return_exceptions=True)
    assert results[0]["connected"] and results[1:3] == [None, None]
    assert isinstance(results[3], HarnessError) and results[3].code == "runtime_closed"
    assert adapter.closed and not adapter.connected and not owner.open
    assert owner.connect_calls == owner.close_calls == 1
    evidence(
        "async-shutdown",
        closed=adapter.closed,
        owner_open=owner.open,
        connected_entries=len(adapter.connected),
        connects=owner.connect_calls,
        closes=owner.close_calls,
        later_error=results[3].code,
    )


async def test_async_failed_connection_is_not_registered_and_can_retry():
    owner = AsyncIOOwner(fail_first=True)
    adapter = async_io_adapter(owner)
    first = asyncio.create_task(adapter.connect("arm_view"))
    await owner.entered.wait()
    owner.release.set()
    with pytest.raises(RuntimeError, match="connection failed"):
        await first
    assert not adapter.connected and not owner.open
    assert (await adapter.connect("overview"))["connected"]
    assert owner.connect_calls == 2 and len(adapter.connected) == 1
    await adapter.close()
    assert owner.close_calls == 1 and not owner.open
    evidence("async-retry", connects=owner.connect_calls, closes=owner.close_calls)


async def test_owner_aliases_close_once_even_when_another_close_fails():
    from robot_rsi.adapters import Adapter
    from robot_rsi.capabilities import SkillBindings

    class Owner:
        def __init__(self, fail=False):
            self.closed = 0
            self.fail = fail

        async def close(self):
            self.closed += 1
            await asyncio.sleep(0)
            if self.fail:
                raise RuntimeError("fixture close error")

    first, bad = Owner(), Owner(True)
    adapter = Adapter(
        SkillBindings(None), (), lifecycles={"first": first, "alias": first, "last": bad}
    )
    with pytest.raises(RuntimeError):
        await adapter.close()
    assert first.closed == bad.closed == 1
    await adapter.close()
    assert first.closed == bad.closed == 1


def test_calibration_reuses_camera_schemas_and_checks_declared_applicability():
    from robot_rsi.contracts import Intrinsics, StereoCalibration, VersionRef
    from robot_rsi.resources import CalibrationRecord

    intr = Intrinsics(width=16, height=12, fx=10, fy=10, cx=8, cy=6)
    common = {
        "reference": VersionRef(id="intrinsic", version="v1"),
        "resource_id": "sensor",
        "stream_id": "rectified_preview",
        "resolution": (16, 12),
        "method": "fixed_fixture",
        "sample_source": "synthetic data only",
        "qualification": "synthetic",
    }
    assert CalibrationRecord(kind="intrinsics", intrinsics=intr, **common).intrinsics == intr
    stereo = StereoCalibration(
        version="v1",
        frame_id="optical",
        left=intr,
        right=intr,
        baseline_m=0.05,
        rectified=True,
        source="synthetic",
    )
    assert CalibrationRecord(kind="stereo", stereo=stereo, **common).stereo == stereo
    with pytest.raises(ValidationError):
        CalibrationRecord(kind="intrinsics", intrinsics=intr, **{**common, "resolution": (20, 12)})
    with pytest.raises(ValidationError):
        CalibrationRecord(kind="hand_eye", artifact_id="transform_payload", **common)


@pytest.mark.parametrize(
    "kind,resource,source",
    [("hand_eye", "sensor", "fixture_optical"), ("tcp", "bench", "calibrated_tcp")],
)
async def test_fixed_hand_eye_and_tcp_artifacts_are_read_not_sampled(
    profile, kind, resource, source
):
    async with Runtime(configured(profile)) as runtime:
        result = await call(
            runtime,
            "calibration_get",
            reference={"id": "fixture_" + kind, "version": "v1"},
            resource_id=resource,
            model_ref=REF,
            target_frame="tool_tip",
            source_frame=source,
            stream_id="rectified_preview" if kind == "hand_eye" else None,
            resolution=(16, 12) if kind == "hand_eye" else None,
        )
        assert result["applicable"] and result["record"]["kind"] == kind
        assert runtime.robot.connect_count == 0 and runtime.cameras.capture_count == 0
        assert not runtime.robot.commands
        evidence(
            "calibration-" + kind,
            result=result,
            connects=runtime.robot.connect_count,
            samples=runtime.cameras.capture_count,
        )
