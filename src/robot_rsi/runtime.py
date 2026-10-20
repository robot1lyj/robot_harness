from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace

from pydantic import ValidationError

from .adapters import build_adapter
from .assets import Assets
from .builtin_tasks.pick_place import PICK_PLACE_VERSION, pick_place
from .capabilities import PICK_PLACE_REQUIREMENTS, PickPlaceGoal, SkillContext
from .catalog import public_descriptor
from .contracts import (
    HarnessError,
    Profile,
    TaskCheckpointGet,
    TaskRun,
)
from .device_validation import confirm_command, validate_command, validate_state
from .environment import environment_summary
from .feedback import Feedback
from .guides import read_guide
from .host import resolve_host
from .memory import Memory
from .operations import Operations, operation_resources
from .project import Project
from .registry import Registry
from .resources import (
    CommandFeedback,
    CommandReceipt,
    FKResult,
    IKResult,
    RobotState,
)
from .scene_bindings import (
    BindingRead,
    SceneBindings,
)
from .storage import Store, uid
from .task_context import build_task_context
from .task_memory import TaskMemory
from .tool_specs import MOTION_TOOLS, OPERATION_TOOLS, TOOLS
from .workspace import Workspace

__all__ = ["Runtime", "TOOLS", "MOTION_TOOLS", "OPERATION_TOOLS"]


class Runtime:
    def __init__(
        self, profile: Profile, *, depth_provider=None, robot_transport=None, bindings_factory=None
    ):
        self.profile = profile
        self.project = Project(profile)
        self._binding_scope = ContextVar("runtime_binding", default=None)
        self.store = Store(profile)
        try:
            self.host_config = resolve_host(profile)
            self.adapter = build_adapter(
                profile,
                self.store,
                depth_predictor=depth_provider,
                robot_transport=robot_transport,
                bindings_factory=bindings_factory,
            )
            self.robot = self.adapter.owner
            self.cameras = self.adapter.bindings.camera
            self.provider = self.adapter.depth_provider
            self.depth = self.adapter.bindings.depth
            config = self.host_config
            if config.model_ref is None and self.adapter.bindings.robot is not None:
                config = config.model_copy(
                    update={"model_ref": self.adapter.bindings.robot.model.reference}
                )
            self.assets = Assets(self.store, config, self.adapter.models, self.adapter.calibrations)
            self._base_bindings = replace(
                self.adapter.bindings, calibration=self.adapter.bindings.calibration or self.assets
            )
            self._capabilities = self.bindings.catalog()
            self.scenes = SceneBindings(self)
            self.operations = Operations(self.store, self.robot)
        except BaseException:
            self.store.close()
            raise
        self.registry = Registry(profile, self.bindings, self.robot)
        self.memory = Memory(self.store, capabilities=self.bindings.catalog(),
                             skill_versions=self.registry.versions())
        self.workspace = Workspace(profile, self.store)
        self.task_memory = TaskMemory(
            self.store,
            skill_versions=self.registry.versions(),
            capabilities=self.bindings.catalog(),
            maintenance=profile.memory_maintenance,
        )
        from .installation import installation_identity

        self.task_memory.installation_digest = installation_identity()["digest"]
        self.task_memory.resource_versions = self.assets.selected
        from .agent_state import AgentState

        self.agent_state = AgentState(self)
        self.feedback = Feedback(self)
        self.operations.on_finished = self.operation_finished
        self.operations.on_stage = self.task_memory.stage
        # Store is the task owner; repair registry references after an interrupted publish.
        for checkpoint in self.store.all("task_checkpoint"):
            if (
                not checkpoint["archived"]
                and checkpoint.get("execution", {}).get("status", "active") == "active"
            ):
                for name, version in checkpoint["skill_versions"].items():
                    self.registry.pin(checkpoint["task_id"], name, version, restore=True)
            else:
                self.registry.release(checkpoint["task_id"])

        # Transient operation pins survive a crash, but never authorize replay.
        operations = {
            op.get("args", {}).get("request_id"): op for op in self.store.all("operation")
        }
        for pin in self.registry.db.execute(
            "SELECT task FROM pins WHERE workspace=?", (self.registry.workspace,)
        ).fetchall():
            if pin[0].startswith("operation:"):
                op = operations.get(pin[0].removeprefix("operation:"))
                if (
                    not op
                    or op.get("hold_reconciled")
                    or (op.get("hold") or {}).get("confirmed")
                    or (not operation_resources(op) and op["status"] != "unknown")
                ):
                    self.registry.release(pin[0])

    async def __aenter__(self):
        return self

    @property
    def bindings(self):
        scoped = self._binding_scope.get()
        return scoped[0] if scoped else self._base_bindings

    @bindings.setter
    def bindings(self, value):
        # Retain the existing host/test override; model tools only select installed ports.
        self._base_bindings = value
        if hasattr(self, "scenes"):
            self.scenes.base = value
            self.scenes._prepared.clear()

    @contextmanager
    def binding_scope(self, bound, versions):
        token = self._binding_scope.set((bound, versions))
        try:
            yield
        finally:
            self._binding_scope.reset(token)

    async def __aexit__(self, *_):
        await self.close()

    async def close(self):
        try:
            await self.operations.close()
            await self.adapter.close()
        finally:
            self.registry.close()
            self.store.close()

    def tools(self):
        entries = []
        for name, (_, description) in TOOLS.items():
            available, reason = True, None
            if name == "project_files":
                available = self.project.can_read_sources
                reason = "Host-selected onboarding/improve project only"
            if name in MOTION_TOOLS and not getattr(self.robot, "supports_motion", False):
                available, reason = False, "Legacy YAM motion bridge not implemented"
            if name in ("task_recover", "skill_pick_place") and self.bindings.missing(
                PICK_PLACE_REQUIREMENTS
            ):
                available, reason = False, "Required skill capabilities are missing or incompatible"
            if name == "robot_hold":
                available = getattr(self.robot, "supports_hold", False)
                reason = "Check confirmed in result; availability is not stop confirmation"
            if name == "resource_connect":
                available = bool(self.adapter.resources)
                reason = "Connect a host-configured owner; discovery does not connect"
            if name == "depth_estimate":
                readiness = (
                    self.provider.readiness()
                    if self.provider
                    else {
                        "configured": self.depth is not None,
                        "reason": "No depth port" if self.depth is None else None,
                    }
                )
                available = readiness.get("configured", False) or readiness.get("ready", False)
                reason = readiness.get("reason")
            required = {
                "robot_state": "robot.state",
                "robot_command": "robot.command",
                "robot_observe": "scene.observe",
                "camera_capture": "camera.capture",
                "camera_describe": "camera.describe",
                "kinematics_fk": "kinematics.fk",
                "kinematics_ik": "kinematics.ik",
                "calibration_get": "calibration.read",
                "model_get": "model.read",
            }
            if name in required and self.bindings.missing({required[name]: 1}):
                available, reason = False, "Optional capability is not bound"
            if name in ("robot_move", "robot_gripper") and not callable(
                getattr(self.robot, "move" if name == "robot_move" else "gripper", None)
            ):
                available, reason = (
                    False,
                    "Legacy diagnostic operation is not provided by this adapter",
                )
            entries.append(
                {"name": name, "description": description, "available": available, "reason": reason}
            )
        return {
            "tools": entries,
            "robot": self.profile.robot,
            "scope": self.store.scope,
            "qualification": getattr(self.robot, "qualification", "no_robot"),
            "model_routing": "external_agent; no automatic model calls",
            "availability_meaning": "bound capability; current execution still requires admission",
        }

    def skill_descriptors(self, bindings=None):
        return self.registry.descriptors(bindings=self.bindings if bindings is None else bindings)

    def workspace_brief(self, task_id=None, bindings=None):
        bound = self.bindings if bindings is None else bindings
        brief = self.workspace.brief(self.robot, bound.camera, self.skill_descriptors(bound))
        brief["environment"].update(environment_summary(self, task_id, bound))
        versions = self.registry.versions()
        if task_id:
            versions = {**versions, **self.task_memory._load(task_id)["skill_versions"]}
        if project := self.project.brief(self.task_memory.installation_digest, versions):
            brief["project"] = project
            project["installed_versions"] = self.feedback.versions
            project["bindings"] = self.scenes.read(BindingRead(), task_id)
            feedback = self.feedback.brief(task_id)
            brief["framework_updates"] = feedback.pop("issues")
            brief["framework_update_index"] = feedback
        return brief

    def task_bindings(self, task_id=None, *, skill=None):
        if task_id and (reference := self.resource_versions(task_id).get("scene_binding")):
            from .contracts import VersionRef
            return self.scenes.resolve(VersionRef.model_validate(reference), skill=skill)[0]
        return self.bindings

    def recall_memory(self, args, task_id=None):
        if not task_id:
            return self.memory.recall(args)
        record = self.task_memory._load(task_id)
        return self.memory.recall(
            args,
            capabilities=self.task_bindings(task_id).catalog(),
            skill_versions={**self.registry.versions(), **record["skill_versions"]},
        )

    def operation_finished(self, record):
        self.task_memory.finished(record)
        if (record.get("hold") or {}).get("confirmed") or (
            not operation_resources(record) and record["status"] != "unknown"
        ):
            self.registry.release("operation:" + record["args"]["request_id"])

    def select_skill(self, task_id, name):
        record = self.task_memory._load(task_id)
        if record["archived"] or record.get("execution", {}).get("status", "active") != "active":
            raise HarnessError("task_closed", "Closed tasks cannot select new skill bindings")
        bound = self.task_bindings(task_id, skill=name)
        selected = self.registry.resolve(name, task_id, bindings=bound)
        self.registry.pin(task_id, name, selected["version"])
        try:
            self.task_memory.select_skill(task_id, name, selected["version"], bound.catalog())
        except BaseException:
            # Remove only a newly introduced reference; retain existing owner pins.
            record = self.task_memory._load(task_id)
            if name not in record["skill_versions"]:
                with self.registry.db:
                    self.registry.db.execute(
                        "DELETE FROM pins WHERE workspace=? AND task=? AND name=?",
                        (self.registry.workspace, task_id, name),
                    )
            raise
        return selected

    def skill_context(self, identity):
        task_id = self.store.get("operation", identity).get("task_id") if identity else None
        versions = self.resource_versions(task_id) if task_id else (self.resource_versions(None) or self.assets.selected)
        bound = self.bindings
        if versions and versions.get("scene_binding"):
            from .contracts import VersionRef
            bound, versions = self.scenes.resolve(VersionRef.model_validate(versions["scene_binding"]))
        return SkillContext(
            bound,
            lambda stage, data: self.operations.stage(identity, stage, data),
            versions,
        )

    def validate_pick_place(self, args):
        self.bindings.require(PICK_PLACE_REQUIREMENTS)
        self.bindings.manipulation.validate_goal(
            PickPlaceGoal(arm=args.arm, object_id=args.object_id, target_region=args.target_region)
        )

    async def observe(self, camera: str | None = None):
        self.bindings.require({"scene.observe": 1})
        # Built-in scenes accept a semantic view for the old diagnostic entry.
        if camera is not None:
            snapshot = getattr(self.bindings.scene, "snapshot", None)
            if not callable(snapshot):
                raise HarnessError(
                    "unsupported_camera", "Use camera_capture for adapter camera views"
                )
            return await snapshot(camera)
        reference = await self.bindings.scene.observe()
        return self.store.get("observation", reference.id)

    def resource_versions(self, task_id):
        if task_id:
            return self.task_memory._load(task_id).get("resource_versions", {})
        scoped = self._binding_scope.get()
        return scoped[1] if scoped else None

    async def invoke(
        self,
        name: str,
        arguments: dict | None = None,
        *,
        task_id: str | None = None,
        agent_session_id: str | None = None,
        actor: str = "runtime",
    ) -> dict:
        """Same validation, errors and event trail for CLI, HTTP and MCP."""
        call_id, started = uid("call"), time.monotonic()
        binding_token = None
        try:
            bound_task = self.profile.active_task_id or task_id
            arguments = dict(arguments or {})
            session_task = (
                self.store.get("agent_session", agent_session_id)["task_id"]
                if agent_session_id
                else None
            )
            identities = {
                identity
                for identity in (
                    self.profile.active_task_id,
                    task_id,
                    arguments.get("task_id"),
                    session_task,
                )
                if identity
            }
            if name in ("operation_get", "operation_cancel") and arguments.get("operation_id"):
                operation = self.store.get("operation", arguments["operation_id"])
                if operation.get("task_id"):
                    identities.add(operation["task_id"])
            if len(identities) > 1:
                raise HarnessError("task_mismatch", "Call and host task identities differ")
            task_id = next(iter(identities), None)
            if task_id:
                try:
                    self.task_memory.charge(task_id, name)
                except HarnessError as exc:
                    if exc.code != "not_found" or not (
                        name == "task_begin" or (name == "task_checkpoint_save" and not bound_task)
                    ):
                        raise
                if name in (
                    "task_run",
                    "task_begin",
                    "task_context",
                    "task_recover",
                    "task_cancel",
                    "task_checkpoint_save",
                    "task_checkpoint_get",
                    "task_archive",
                    "task_finish",
                ):
                    arguments["task_id"] = task_id
            if agent_session_id:
                self.agent_state.guard(agent_session_id, task_id, name)
            if name in ("assistance_claim", "assistance_return") and actor != "operator":
                raise HarnessError("operator_required", "Operator control is host-only")
            if name not in TOOLS:
                raise HarnessError("unknown_tool", "Use tools_list to discover available tools")
            args = TOOLS[name][0].model_validate(arguments)
            if task_id and name in {
                "camera_describe", "camera_capture", "kinematics_fk", "kinematics_ik",
                "calibration_get", "model_get", "task_run", "capabilities_list",
                "skills_list", "skill_describe", "task_context", "workspace_read",
                "station_describe", "tools_list", "task_recover",
                "project_read", "issue_get",
            } and (name != "workspace_read" or (args.query is None and args.key is None)):
                from .contracts import VersionRef
                versions = self.resource_versions(task_id)
                if reference := versions.get("scene_binding"):
                    checkpoint = self.task_memory._load(task_id)
                    if name in {"camera_capture", "kinematics_fk", "kinematics_ik", "task_run"} and (checkpoint["archived"] or checkpoint["execution"]["status"] != "active"):
                        raise HarnessError("task_closed", "Task was cancelled or archived")
                    if name in {"camera_capture", "kinematics_fk", "kinematics_ik", "task_run"} and checkpoint["execution"].get("installation_digest") not in (None, self.task_memory.installation_digest):
                        raise HarnessError("installation_changed", "End the old task before using changed installed code")
                    bound, versions = self.scenes.resolve(VersionRef.model_validate(reference), skill=getattr(args, "skill", None))
                    binding_token = self._binding_scope.set((bound, versions))
            self.store.event(
                "tool.started",
                {
                    "id": call_id,
                    "name": name,
                    "task_id": task_id,
                    "arguments": args.model_dump(),
                    "agent_session_id": agent_session_id,
                },
            )
            if task_id and name in MOTION_TOOLS - {"task_run", "task_recover"}:
                raise HarnessError(
                    "task_skill_required", "Managed tasks submit through task_run or task_recover"
                )
            if task_id and name == "robot_hold":
                self.task_memory.cancel(task_id)
            result = await self._dispatch(name, args, task_id=task_id)
            if agent_session_id and name in ("tools_list", "tool_describe"):
                from .agent_client import AGENT_TOOLS

                if name == "tools_list":
                    entries = result["tools"]
                else:
                    availability = next(t for t in self.tools()["tools"] if t["name"] == result["name"])
                    result.update({key: availability[key] for key in ("available", "reason")})
                    entries = [result]
                for entry in entries:
                    entry["callable"] = entry["name"] in AGENT_TOOLS
                result["tool_scope"] = "native_agent"
                result["callable_meaning"] = "exposed by this native client; separate from bound capability and execution admission"
            if agent_session_id and name in ("task_run", "task_recover"):
                self.agent_state.advance(agent_session_id)
            response = {"ok": True, "result": result}
        except ValidationError as exc:
            response = {
                "ok": False,
                "error": {
                    "code": "invalid_arguments",
                    "details": [
                        {"path": list(e["loc"]), "message": e["msg"]} for e in exc.errors()
                    ],
                },
            }
        except HarnessError as exc:
            response = {"ok": False, "error": exc.payload()}
        except Exception as exc:
            response = {
                "ok": False,
                "error": {"code": "internal_error", "type": type(exc).__name__},
            }
        finally:
            if binding_token is not None:
                self._binding_scope.reset(binding_token)
        elapsed = round((time.monotonic() - started) * 1000, 2)
        self.store.event(
            "tool.finished",
            {
                "id": call_id,
                "name": name,
                "task_id": task_id,
                "ok": response["ok"],
                "duration_ms": elapsed,
                "agent_session_id": agent_session_id,
                "error": response.get("error"),
            },
        )
        return {**response, "call_id": call_id}

    async def _dispatch(self, name, args, *, task_id=None):
        if name == "binding_candidates":
            return self.scenes.find(args)
        if name == "binding_read":
            return self.scenes.read(args, task_id)
        if name == "binding_validate":
            return await self.scenes.validate(args)
        if name == "binding_save":
            if args.attach_to_task:
                if not task_id:
                    raise HarnessError("task_not_started", "Attachment requires the current task")
                record = self.task_memory._load(task_id)
                if record["operation_ids"] or record.get("resource_versions", {}).get("scene_binding") or record.get("execution", {}).get("status") != "active":
                    raise HarnessError("new_task_required", "Bindings are fixed before first execution; use a new task")
            result = self.scenes.save(args)
            if args.attach_to_task:
                bound, versions = self.scenes.resolve(args.reference)
                record["resource_versions"] = versions
                record["capabilities"] = bound.catalog()
                self.task_memory._write(record)
            return result
        if name == "agent_session_get":
            return self.agent_state.status(args.task_id)
        if name == "assistance_get":
            return self.agent_state.request(args.task_id)
        if name == "assistance_claim":
            return await self.agent_state.claim(args)
        if name == "assistance_return":
            return self.agent_state.give_back(args)
        if name == "task_begin":
            versions = None
            capabilities = None
            if args.binding_ref:
                bound, versions = self.scenes.resolve(args.binding_ref, args.binding_conditions)
                capabilities = bound.catalog()
            try:
                previous = self.task_memory._load(args.task_id)
            except HarnessError as exc:
                if exc.code != "not_found":
                    raise
            else:
                if args.binding_ref and previous.get("execution") and previous.get("resource_versions", {}).get("scene_binding") != args.binding_ref.model_dump():
                    raise HarnessError("task_conflict", "Task binding is already fixed")
            result = self.task_memory.begin(args, self.profile.task_limits, versions, capabilities)
            return {**result, "environment": environment_summary(self, args.task_id)}
        if name == "task_context":
            return build_task_context(self, args)
        if name == "task_finish":
            result = self.task_memory.finish(args)
            self.registry.release(args.task_id)
            return result
        if name == "task_cancel":
            record = self.task_memory.cancel(args.task_id)
            self.registry.release(args.task_id)
            for identity in record["operation_ids"]:
                await self.operations.cancel(identity)
            return self.task_memory.get(
                TaskCheckpointGet(task_id=args.task_id, include_archived=True)
            )
        if name == "task_recover":
            if prior := self.operations.existing(name, args):
                return prior
            original = self.store.get("operation", args.operation_id)
            if original.get("task_id") != args.task_id:
                raise HarnessError("task_mismatch", "Recovery evidence belongs to another task")
            stored_args = dict(original["args"])
            if "parameters" not in stored_args:
                stored_args["parameters"] = {
                    key: stored_args.pop(key)
                    for key in ("arm", "object_id", "target_region", "grasp_retries")
                    if key in stored_args
                }
            goal = TaskRun.model_validate(
                {
                    **stored_args,
                    "request_id": args.request_id,
                    "task_id": args.task_id,
                    "task_epoch": args.task_epoch,
                }
            )
            return await self._run_skill(
                goal, tool=name, recovery_of=args.operation_id, dedup_args=args
            )
        if name == "workspace_read":
            if args.query is None and args.key is None:
                return self.workspace_brief(task_id)
            return self.workspace.read(args)
        if name == "project_read":
            return self.project.read(args)
        if name == "project_files":
            return self.project.files(args)
        if name == "harness_read":
            return read_guide(args, self.task_memory.installation_digest)
        if name == "issue_create":
            return self.feedback.create(args)
        if name == "issue_get":
            return self.feedback.get(args, task_id)
        if name == "workspace_note":
            return self.workspace.note(args)
        if name == "task_checkpoint_save":
            self.task_memory.skill_versions = self.registry.versions()
            result = self.task_memory.save(args)
            for skill, version in result["skill_versions"].items():
                self.registry.pin(args.task_id, skill, version)
            return result
        if name == "task_checkpoint_get":
            return self.task_memory.get(args)
        if name == "task_archive":
            result = self.task_memory.archive(args.task_id)
            self.registry.release(args.task_id)
            return result
        if name == "camera_describe":
            self.bindings.require({"camera.describe": 1})
            return self.bindings.camera.describe()
        if name == "camera_capture":
            self.bindings.require({"camera.capture": 1})
            return (
                await self.bindings.camera.capture(args.camera, args.require, args.max_age_s)
            ).model_dump()
        if name == "capabilities_list":
            return {
                "capabilities": self.bindings.catalog(),
                "resources": self.adapter.describe(),
                "skills": [public_descriptor(skill) for skill in self.skill_descriptors()],
            }
        if name == "station_describe":
            return {
                "station": self.profile.station,
                "backend": self.profile.robot,
                "qualification": getattr(self.robot, "qualification", "no_robot"),
                "supported_tasks": [
                    skill["name"]
                    for skill in self.skill_descriptors()
                    if skill["available"] and skill["kind"] == "code"
                ],
                "arms": list(getattr(self.robot, "arms", ())),
                "cameras": [item["role"] for item in self.bindings.camera.describe()["cameras"]]
                if self.bindings.camera
                else [],
                "camera_inputs": self.bindings.camera.describe() if self.bindings.camera else {"cameras": []},
                "frames": list(getattr(self.robot, "frames", ())),
                "units": {
                    "translation": "m",
                    "rotation": "rad",
                    "joint_and_tool": "declared by model_get",
                },
                "entrypoint": "task_run",
                "followup": "operation_get",
                "blockers": self.bindings.missing(PICK_PLACE_REQUIREMENTS),
                "capabilities": self.bindings.catalog(),
                "depth_provider": self.provider.readiness()
                if self.provider
                else {"configured": self.depth is not None},
                "physical_availability": "not_checked",
                "execution_blocked": self.operations.blocked,
                "execution_busy": any(not task.done() for task in self.operations.tasks.values()),
                "memory_scope": self.store.scope,
            }
        if name == "task_run":
            return await self._run_skill(args)
        if name == "tools_list":
            return self.tools()
        if name == "tool_describe":
            if args.name not in TOOLS:
                raise HarnessError("unknown_tool", "Tool not found")
            schema, description = TOOLS[args.name]
            return {
                "name": args.name,
                "description": description,
                "input_schema": schema.model_json_schema(),
            }
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
        if name in MOTION_TOOLS:
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
                skill_version=PICK_PLACE_VERSION if is_skill else None,
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
        if name == "operation_get":
            return await self.operations.get(args.operation_id, args.wait_s)
        if name == "operation_cancel":
            operation = self.store.get("operation", args.operation_id)
            if operation.get("task_id"):
                self.task_memory.cancel(operation["task_id"])
                self.registry.release(operation["task_id"])
            return await self.operations.cancel(args.operation_id)
        if name == "skills_list":
            return self.registry.cards(args, bindings=self.bindings)
        if name == "skill_describe":
            return (
                self.select_skill(task_id, args.name)
                if task_id
                else self.registry.resolve(args.name, bindings=self.bindings)
            )
        if name == "artifact_info":
            return self.store.get("artifact", args.artifact_id)
        if name == "depth_estimate":
            self.bindings.require({"depth.estimate": 1})
            return await self.depth.estimate(args.observation_id, args.max_depth_m)
        if name == "depth_point":
            self.bindings.require({"depth.estimate": 1})
            return self.depth.point(args.depth_id, args.u, args.v, args.radius, args.stream_id)
        if name == "memory_recall":
            return self.recall_memory(args, task_id)
        if name == "memory_remember":
            return self.memory.remember(args)
        raise HarnessError("unknown_tool", "Tool not found")

    async def _run_skill(self, args, *, tool="task_run", recovery_of=None, dedup_args=None):
        if prior := self.operations.existing(tool, dedup_args or args):
            return prior
        task = None
        if args.task_id:
            task = self.task_memory.admit(
                args.task_id,
                args.task_epoch,
                recovery_of=recovery_of,
                after_operation_id=args.after_operation_id,
            )
            selected = self.select_skill(args.task_id, args.skill)
        else:
            if args.after_operation_id:
                raise HarnessError("task_not_started", "Continuation must belong to a managed task")
            selected = self.registry.resolve(args.skill, bindings=self.bindings)
        model, output_model, entry = self.registry.load(args.skill, selected["version"], bindings=self.bindings)
        parameters = model.model_validate(args.parameters)
        if selected["kind"] != "code":
            # Instruction preparation is explicitly not an operation or a success claim.
            return {
                "prepared": True,
                "completed": False,
                "task_id": args.task_id,
                "skill": selected,
            }
        self.bindings.require(selected["requires"])
        if validate := getattr(parameters, "validate_context", None):
            validate(self.skill_context(None))
        temporary_pin = "operation:" + args.request_id
        self.registry.pin(
            temporary_pin, args.skill, selected["version"], inherited_task=args.task_id
        )
        try:
            observation = None
            if "manipulator" in selected["resources"]:
                if selected["requires"].get("scene.observe") != 1:
                    self.bindings.require({"scene.observe": 1})
                reference = await self.bindings.scene.observe()
                observation = self.store.get("observation", reference.id)
            if task:
                task = self.task_memory.admit(
                    args.task_id,
                    args.task_epoch,
                    recovery_of=recovery_of,
                    after_operation_id=args.after_operation_id,
                )

            async def run(identity):
                result = await entry(self.skill_context(identity), parameters)
                return output_model.model_validate(result).model_dump()

            operation = self.operations.submit(
                tool,
                args,
                run,
                observation=observation,
                resources=selected["resources"],
                skill_name=args.skill,
                skill_version=selected["version"],
                capabilities=self.bindings.catalog(),
                deadline_at=task["execution"]["deadline_at"] if task else None,
                recovery_of=recovery_of,
                dedup_args=dedup_args,
            )
            if task:
                self.task_memory.attach(args.task_id, operation, recovery_of=recovery_of)
            return operation
        except BaseException:
            self.registry.release(temporary_pin)
            raise
