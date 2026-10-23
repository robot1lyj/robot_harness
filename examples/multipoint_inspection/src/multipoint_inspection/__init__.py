"""A complete callable inspection method; no model inside the ordinary path."""
import asyncio
import io
import json
from importlib.resources import files
from typing import Literal
from PIL import Image, ImageStat
from pydantic import Field, model_validator
from robot_rsi.contracts import Contract, HarnessError, Identifier, Finite, VersionRef
from robot_rsi.device_validation import confirm_command, validate_state, validate_command
from robot_rsi.resources import RobotCommand, Waypoint
from robot_rsi.storage import uid


def packages():
    return [files(__package__).joinpath("skill")]


class Point(Contract):
    id: Identifier
    joints: dict[Identifier, Finite]


class Parameters(Contract):
    points: list[Point] = Field(min_length=1, max_length=32)
    resource_id: Identifier
    arm: Identifier
    model_ref: VersionRef
    camera: Identifier = "inspection"
    # A report goal may succeed with anomalies; normality is a separate criterion.
    mode: Literal["report", "confirm_normal"] = "confirm_normal"
    resamples: int = Field(default=1, ge=0, le=3)

    @model_validator(mode="after")
    def distinct(self):
        if len({p.id for p in self.points}) != len(self.points):
            raise ValueError("Point IDs must be unique")
        return self

    def validate_context(self, context):
        bound = context.resource_versions.get("model")
        if bound != self.model_ref.model_dump():
            raise HarnessError("model_not_bound", "Use this task's fixed robot model")
        for point in self.points:
            context.bindings.robot.model.validate_joints(self.arm, point.joints)


def judge(data):
    """Only pixels determine the observation, never command receipts or desired state."""
    with Image.open(io.BytesIO(data)) as image:
        red, green, blue = ImageStat.Stat(image.convert("RGB")).mean
    if max(red, green, blue) < 40:
        return "unknown"
    return "passed" if green > red * 1.5 and green > blue * 1.5 else "failed"


def plan(points, start):
    # Deterministic nearest-next order; bounded input, no generic planner claim.
    remaining = list(points)
    ordered = []
    current = start
    while remaining:
        point = min(remaining, key=lambda p: (sum((v-current[k])**2 for k, v in p.joints.items()), p.id))
        ordered.append(point)
        remaining.remove(point)
        current = point.joints
    return ordered


async def run(context, args):
    robot, camera = context.bindings.robot, context.bindings.camera
    state = await robot.state()
    ordered = plan(args.points, state.joints)
    results, moves, samples = [], 0, 0
    for index, point in enumerate(ordered):
        remaining = [p.id for p in ordered[index:]]
        progress = {"confirmed": [r["point"] for r in results], "remaining": remaining}
        context.stage("moving", progress)
        state = await robot.state()
        command = RobotCommand(request_id=uid("inspect"), resource_id=args.resource_id,
            model_ref=args.model_ref, arm=args.arm, observation_id=state.id,
            trajectory=[Waypoint(time_from_start_s=0.001, positions=point.joints)])
        validate_command(robot.model, command, state)
        receipt = await robot.submit(command, context.operation_id)
        if (receipt.operation_id != context.operation_id or receipt.request_id != command.request_id
                or receipt.resource_id != args.resource_id or receipt.boot_id != state.boot_id or receipt.epoch != state.epoch):
            raise HarnessError("verification_unknown", "Command ownership is unknown", details=progress)
        feedback = await robot.wait(receipt)
        if (feedback.status == "unknown" or feedback.command_id != receipt.command_id
                or feedback.operation_id != context.operation_id):
            raise HarnessError("verification_unknown", "Physical command effect unknown", details=progress)
        if feedback.status != "completed":
            raise HarnessError("command_failed", feedback.reason or "Motion failed", details=progress)
        validate_state(robot.model, feedback.state)
        if feedback.state.boot_id != state.boot_id or feedback.state.epoch != state.epoch:
            raise HarnessError("verification_unknown", "Feedback epoch/boot changed", details=progress)
        robot.validate_observation(feedback.state.model_dump())
        confirm_command(robot.model, command, feedback.state)
        moves += 1
        frames = []
        # Recovery repeats only read-only capture, never the completed movement.
        for attempt in range(args.resamples + 1):
            context.stage("sampling", {**progress, "attempt": attempt})
            observation = await camera.capture(camera=args.camera, require="preview")
            samples += 1
            frame = observation.frames[0]
            status = judge(context.artifacts.read(frame.artifact_id))
            frames.append({"observation_id": observation.id, "artifact_id": frame.artifact_id,
                           "status": status, "qualification": observation.qualification})
            if status != "unknown":
                break
            await asyncio.sleep(0)
        results.append({"point": point.id, "status": status, "frames": frames,
            "motion": {"command": command.model_dump(), "receipt": receipt.model_dump(),
                       "feedback": feedback.model_dump()}})
        context.stage("point_confirmed", {"confirmed": [r["point"] for r in results],
            "remaining": [p.id for p in ordered[index+1:]]})
    normal = "failed" if any(r["status"] == "failed" for r in results) else "unknown" if any(r["status"] == "unknown" for r in results) else "passed"
    report = {"qualification": "synthetic" if all(f["qualification"] == "synthetic" for r in results for f in r["frames"]) else "provider_dependent",
        "source": "pixel_green_dominance_v1; not field validated", "mode": args.mode,
        "order": [p.id for p in ordered], "points": results,
        "normality": normal, "moves": moves, "samples": samples,
        "remaining": [], "last_confirmed": results[-1]["point"]}
    identity = context.artifacts.write(json.dumps(report).encode(), "application/json", {"kind": "inspection_report"})
    ref = [{"kind": "artifact", "id": identity}]
    return {"completed": True, "verified": True, "qualification": report["qualification"],
        "report_id": identity, "moves": moves, "samples": samples,
        "criterion_results": [
            {"criterion_id": "report", "status": "passed", "source": report["source"], "evidence_refs": ref, "remaining": []},
            {"criterion_id": "normal", "status": normal, "source": report["source"], "evidence_refs": ref,
             "remaining": [] if normal == "passed" else ["Resolve anomalous or unreadable observations before confirming normality"]}]}
