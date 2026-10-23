from __future__ import annotations
from dataclasses import dataclass
from ..contracts import ArtifactQuery, CameraCapture, DepthEstimate, DepthPoint, Empty, Gripper, HarnessError, Move, Observe, PickPlace

from ..resources import CalibrationQuery, FKRequest, IKRequest, ModelQuery, ResourceQuery, RobotCommand
from ..device_validation import confirm_command, validate_command, validate_state
from ..resources import CommandFeedback, CommandReceipt, FKResult, IKResult, RobotState
from ..operations import operation_resources

@dataclass
class ResourceTools:
    _capabilities: object
    adapter: object
    assets: object
    _bindings: object
    depth: object
    observe: object
    operations: object
    registry: object
    resource_versions: object
    robot: object
    skill_context: object
    store: object
    validate_pick_place: object

    definitions = {
        'camera_describe': (
        Empty,
        "Describe semantic camera roles and supported input types without capturing",
    ),
        'camera_capture': (
        CameraCapture,
        "Capture a configured semantic view with typed frames, calibration and clock provenance",
    ),
        'model_get': (
        ModelQuery,
        "Read one immutable model definition; task-bound requests keep their selected version",
    ),
        'resources_list': (Empty, "Describe configured named resources without connecting"),
        'resource_connect': (ResourceQuery, "Explicitly connect a configured resource owner"),
        'robot_state': (ResourceQuery, "Read sampled named joint/tool state after explicit connection"),
        'calibration_get': (
        CalibrationQuery,
        "Read versioned calibration and check applicability; no sampling",
    ),
        'kinematics_fk': (FKRequest, "Compute FK for the selected model; no device access"),
        'kinematics_ik': (IKRequest, "Compute IK for the selected model; no motion or collision claim"),
        'robot_command': (
        RobotCommand,
        "Submit a named device command through Operations; receipt is not measured completion or goal success",
    ),
        'robot_observe': (Observe, "Read fresh state and image references; no implicit connection"),
        'robot_move': (
        Move,
        "Move in the synthetic workspace; requires fresh observation and request ID",
    ),
        'robot_gripper': (
        Gripper,
        "Set synthetic gripper opening; requires fresh observation and request ID",
    ),
        'skill_pick_place': (
        PickPlace,
        "Run bounded pick/place code; poll operation for verified outcome",
    ),
        'robot_hold': (Empty, "Cancel active work and request hold; check confirmed in the result"),
        'artifact_info': (ArtifactQuery, "Read artifact metadata; fetch images separately when needed"),
        'depth_estimate': (
        DepthEstimate,
        "Run the host-selected depth backend on typed camera input; return metric depth references",
    ),
        'depth_point': (
        DepthPoint,
        "Project a pixel in the named source stream to its optical frame in metres",
    ),
    }

    native_tools = ('robot_observe', 'resources_list', 'resource_connect', 'robot_state', 'camera_describe', 'camera_capture', 'kinematics_fk', 'kinematics_ik', 'model_get', 'calibration_get')
    motion_tools = {'robot_gripper', 'skill_pick_place', 'robot_command', 'robot_move'}
    operation_tools = {'robot_gripper', 'skill_pick_place', 'robot_command', 'robot_move'}

    @property
    def bindings(self):
        return self._bindings()

    async def dispatch(self, name, args, *, task_id=None):
        if name == "camera_describe":
            self.bindings.require({"camera.describe": 1})
            return self.bindings.camera.describe()
        if name == "camera_capture":
            self.bindings.require({"camera.capture": 1})
            return (
                await self.bindings.camera.capture(args.camera, args.require, args.max_age_s)
            ).model_dump()
        if name == "model_get":
            self.bindings.require({"model.read": 1})
            return self.assets.guard_model(
                args.reference, self.resource_versions(task_id)
            ).model_dump()
        if name == "resources_list":
            return {
                "resources": self.adapter.describe(),
                "model_ref": (self.resource_versions(task_id) or self.assets.selected)["model"],
            }
        if name == "resource_connect":
            return await self.adapter.connect(args.resource_id)
        if name == "robot_state":
            self.bindings.require({"robot.state": 1})
            if args.resource_id != self.bindings.robot.model.resource_id:
                raise HarnessError("resource_mismatch", "State port belongs to another resource")
            state = RobotState.model_validate(await self.bindings.robot.state())
            if args.resource_id != state.resource_id:
                raise HarnessError("resource_mismatch", "State belongs to another resource")
            validate_state(self.bindings.robot.model, state)
            self.store.put("robot_state", state.id, state.model_dump())
            return state.model_dump()
        if name == "calibration_get":
            self.bindings.require({"calibration.read": 1})
            return self.assets.get(args, self.resource_versions(task_id))
        if name in ("kinematics_fk", "kinematics_ik"):
            capability = "kinematics.fk" if name.endswith("fk") else "kinematics.ik"
            self.bindings.require({capability: 1})
            model = self.assets.guard_model(args.model_ref, self.resource_versions(task_id))
            model.validate_joints(args.arm, args.joints if name.endswith("fk") else args.seed)
            if name.endswith("fk"):
                if args.base_frame not in model.frames or args.target_frame not in model.frames:
                    raise HarnessError("unsupported_frame", "Use model frame IDs")
                result = FKResult.model_validate(await self.bindings.kinematics.fk(args))
                if (
                    result.model_ref != args.model_ref
                    or result.pose.target_frame != args.base_frame
                    or result.pose.source_frame != args.target_frame
                ):
                    raise HarnessError("invalid_kinematics", "FK returned mismatched model/frames")
            else:
                if (
                    args.target.target_frame not in model.frames
                    or args.target.source_frame not in model.frames
                ):
                    raise HarnessError("unsupported_frame", "Use model frame IDs")
                for joint, limits in args.joint_constraints.items():
                    if joint not in args.seed or limits[0] > limits[1]:
                        raise HarnessError("joint_mapping", "Invalid IK joint constraint")
                result = IKResult.model_validate(await self.bindings.kinematics.ik(args))
                if result.model_ref != args.model_ref:
                    raise HarnessError("invalid_kinematics", "IK returned another model")
                if result.status == "solved":
                    model.validate_joints(args.arm, result.joints)
                    if any(
                        not lo <= result.joints[j] <= hi
                        for j, (lo, hi) in args.joint_constraints.items()
                    ):
                        raise HarnessError(
                            "invalid_kinematics", "IK violated requested constraints"
                        )
            return result.model_dump()
        if name == "robot_command":
            self.bindings.require({"robot.command": 1})
            if prior := self.operations.existing(name, args):
                return prior
            state = RobotState.model_validate(self.store.get("robot_state", args.observation_id))
            validate_command(self.bindings.robot.model, args, state)

            async def execute_command(identity):
                receipt = CommandReceipt.model_validate(
                    await self.bindings.robot.submit(args, identity)
                )
                if (
                    receipt.operation_id != identity
                    or receipt.request_id != args.request_id
                    or receipt.resource_id != args.resource_id
                    or receipt.boot_id != state.boot_id
                    or receipt.epoch != state.epoch
                ):
                    raise HarnessError(
                        "verification_unknown", "Command acknowledgement has wrong ownership"
                    )
                self.store.put("command", receipt.command_id, receipt.model_dump())
                self.operations.stage(identity, "submitted", receipt.model_dump())
                feedback = CommandFeedback.model_validate(await self.bindings.robot.wait(receipt))
                if (
                    feedback.command_id != receipt.command_id
                    or feedback.operation_id != identity
                    or feedback.status == "unknown"
                ):
                    raise HarnessError("verification_unknown", "Command effect is unknown")
                if feedback.status != "completed":
                    raise HarnessError("command_failed", feedback.reason or "Command failed")
                validate_state(self.bindings.robot.model, feedback.state)
                if (
                    feedback.state.boot_id != receipt.boot_id
                    or feedback.state.epoch != receipt.epoch
                ):
                    raise HarnessError(
                        "verification_unknown", "Feedback belongs to another owner epoch/boot"
                    )
                self.robot.validate_observation(feedback.state.model_dump())
                confirm_command(self.bindings.robot.model, args, feedback.state)
                self.store.put("robot_state", feedback.state.id, feedback.state.model_dump())
                return {
                    "receipt": receipt.model_dump(),
                    "feedback": feedback.model_dump(),
                    "verified": False,
                    "qualification": feedback.qualification,
                    "meaning": "measured device command completion; not whole-goal or grasp verification",
                }

            return self.operations.submit(
                name,
                args,
                execute_command,
                observation=state.model_dump(),
                capabilities=self._capabilities,
            )
        if name == "robot_observe":
            return await self.observe(args.camera)
        if name in {"robot_move", "robot_gripper", "skill_pick_place"}:
            if name in ("robot_move", "robot_gripper") and not callable(
                getattr(self.robot, "move" if name == "robot_move" else "gripper", None)
            ):
                raise HarnessError("unsupported", "Use the declared named device command port")
            if prior := self.operations.existing(name, args):
                return prior
            if name == "skill_pick_place":
                self.validate_pick_place(args)
            if name == "robot_move" and args.frame_id not in self.robot.frames:
                raise HarnessError("unsupported_frame", "Frame is not bound on this station")
            observation = self.store.get("observation", args.observation_id)

            async def run(identity):
                if name == "skill_pick_place":
                    from ..builtin_tasks.pick_place import pick_place
                    return await pick_place(self.skill_context(identity), args)
                if name == "robot_move":
                    await self.robot.move(args.arm, args.position_m)
                else:
                    await self.robot.gripper(args.arm, args.opening)
                self.operations.stage(identity, "executed")
                return {
                    "qualification": getattr(self.robot, "qualification", "no_robot"),
                    "observation_id": (await self.observe())["id"],
                }

            is_skill = name == "skill_pick_place"
            return self.operations.submit(
                name,
                args,
                run,
                observation=observation,
                skill_name="pick_place" if is_skill else None,
                skill_version=self.registry.describe("pick_place")["version"] if is_skill else None,
                capabilities=self.bindings.catalog() if is_skill else [],
            )
        if name == "robot_hold":
            stopped = await self.operations.hold()
            if stopped.get("confirmed"):
                for op in self.store.all("operation"):
                    if "manipulator" in operation_resources(op) and (
                        request := op.get("args", {}).get("request_id")
                    ):
                        self.registry.release("operation:" + request)
            return stopped
        if name == "artifact_info":
            return self.store.get("artifact", args.artifact_id)
        if name == "depth_estimate":
            self.bindings.require({"depth.estimate": 1})
            return await self.depth.estimate(args.observation_id, args.max_depth_m)
        if name == "depth_point":
            self.bindings.require({"depth.estimate": 1})
            return self.depth.point(args.depth_id, args.u, args.v, args.radius, args.stream_id)
        raise HarnessError("unknown_tool", "Tool not found")
