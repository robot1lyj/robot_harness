"""Validation shared by resource adapters and the single Runtime command path."""

import time

from .contracts import HarnessError
from .resources import RobotState


def validate_state_age(body, boot_id, epoch, max_age_s):
    state = RobotState.model_validate(body)
    stamp = state.capture_time
    if state.boot_id != boot_id or state.epoch != epoch:
        raise HarnessError("stale_observation", "State no longer belongs to this owner epoch/boot")
    if stamp.kind != "unix" or stamp.clock_domain != "utc" or stamp.uncertainty_s is None:
        raise HarnessError(
            "freshness_unknown", "Need acquisition time mapped to a bounded UTC clock"
        )
    age = time.time() - stamp.value_s
    if age < -stamp.uncertainty_s or age + stamp.uncertainty_s > max_age_s:
        raise HarnessError("stale_observation", "Sampled state is outside the admission age budget")


def validate_state(model, state):
    if state.resource_id != model.resource_id or state.model_ref != model.reference:
        raise HarnessError("invalid_state", "State resource/model differs from its bound port")
    if set(state.joints) != {j.id for j in model.joints} or not set(state.tools) <= {
        t.id for t in model.tools
    }:
        raise HarnessError("joint_mapping", "State joints/tools must use model names")


def validate_command(model, command, state):
    validate_state(model, state)
    if command.resource_id != model.resource_id or command.model_ref != model.reference:
        raise HarnessError("model_mismatch", "Command differs from the bound resource/model")
    if command.arm not in model.manipulators:
        raise HarnessError("unsupported_arm", "Manipulator is not declared by the model")
    if command.trajectory:
        for waypoint in command.trajectory:
            model.validate_joints(command.arm, waypoint.positions)
        if command.trajectory[-1].time_from_start_s > command.timeout_s:
            raise HarnessError("invalid_trajectory", "Trajectory exceeds the operation deadline")
    else:
        value = command.effector
        tool = next((t for t in model.tools if t.id == value.tool_id), None)
        if (
            tool is None
            or value.command not in tool.commands
            or value.unit != tool.unit
            or not tool.lower <= value.value <= tool.upper
            or tool.manipulator is not None
            and tool.manipulator != command.arm
        ):
            raise HarnessError(
                "invalid_tool_command", "Use the named tool commands, units and limits"
            )


def confirm_command(model, command, state):
    expected = (
        command.trajectory[-1].positions
        if command.trajectory
        else {command.effector.tool_id: command.effector.value}
    )
    measured = state.joints if command.trajectory else state.tools
    tolerances = {
        item.id: item.tolerance for item in (model.joints if command.trajectory else model.tools)
    }
    if any(
        name not in measured or abs(measured[name] - value) > tolerances[name]
        for name, value in expected.items()
    ):
        raise HarnessError(
            "verification_unknown", "Measured final state does not confirm the command target"
        )
