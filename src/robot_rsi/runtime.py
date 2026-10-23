from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace

from pydantic import ValidationError

from .adapters import build_adapter
from .assets import Assets
from .capabilities import PICK_PLACE_REQUIREMENTS, PickPlaceGoal, SkillContext, SkillArtifacts
from .contracts import HarnessError, Profile
from .environment import environment_summary
from .feedback import Feedback
from .host import resolve_host
from .memory import Memory
from .operations import Operations
from .project import Project
from .registry import Registry
from .scene_bindings import BindingRead, SceneBindings
from .storage import Store, uid
from .task_context import build_task_context
from .execution import Execution
from . import tool_catalog
from .task_memory import TaskMemory
from .tool_specs import MOTION_TOOLS, OPERATION_TOOLS, TOOLS
from .workspace import Workspace

__all__ = ["Runtime", "TOOLS", "MOTION_TOOLS", "OPERATION_TOOLS"]


class Runtime:
    def __init__(
        self, profile: Profile, *, depth_provider=None, robot_transport=None, bindings_factory=None
    ):
        self.profile = profile
        from .host_boundary import protection

        self.host_protection = protection()
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
        self._agent_state = None
        self.feedback = Feedback(self)
        self.execution = Execution(self.store, self.registry, self.operations, self.task_memory,
            bindings=lambda: self.bindings, task_bindings=self.task_bindings, skill_context=self.skill_context)
        self.operations.on_finished = self.operation_finished
        self.operations.on_stage = self.task_memory.stage
        self.execution.restore()
        features = (
            tool_catalog.SceneTools(
                scenes=self.scenes,
                task_memory=self.task_memory,
            ),
            tool_catalog.SessionTools(
                agent_state=lambda: self.agent_state,
            ),
            tool_catalog.ContextTools(
                context=lambda args: build_task_context(self, args),
                feedback=self.feedback,
                memory=self.memory,
                project=self.project,
                recall_memory=self.recall_memory,
                task_memory=self.task_memory,
                workspace=self.workspace,
                workspace_brief=self.workspace_brief,
            ),
            tool_catalog.TaskTools(
                _run_skill=self._run_skill,
                _bindings=lambda: self.bindings,
                environment=lambda task: environment_summary(self, task),
                operations=self.operations,
                profile=self.profile,
                registry=self.registry,
                scenes=self.scenes,
                select_skill=self.select_skill,
                store=self.store,
                task_memory=self.task_memory,
            ),
            tool_catalog.DiscoveryTools(
                adapter=self.adapter,
                _bindings=lambda: self.bindings,
                depth=self.depth,
                operations=self.operations,
                profile=self.profile,
                provider=self.provider,
                robot=self.robot,
                skill_descriptors=self.skill_descriptors,
                store=self.store,
                tools=self.tools,
            ),
            tool_catalog.ResourceTools(
                _capabilities=self._capabilities,
                adapter=self.adapter,
                assets=self.assets,
                _bindings=lambda: self.bindings,
                depth=self.depth,
                observe=self.observe,
                operations=self.operations,
                registry=self.registry,
                resource_versions=self.resource_versions,
                robot=self.robot,
                skill_context=self.skill_context,
                store=self.store,
                validate_pick_place=self.validate_pick_place,
            ),
        )
        self._tool_handlers = {name: feature.dispatch for feature in features for name in feature.definitions}

    @property
    def agent_state(self):
        if self._agent_state is None:
            from .agent_state import AgentState
            self._agent_state = AgentState(self)
        return self._agent_state

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
        return self.execution.operation_finished(record)

    def select_skill(self, task_id, name):
        return self.execution.select_skill(task_id, name)

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
            identity,
            SkillArtifacts(self.store.read_artifact, self.store.artifact),
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
                from .tool_catalog import AGENT_TOOLS

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
        return await self._tool_handlers[name](name, args, task_id=task_id)

    async def _run_skill(self, args, *, tool="task_run", recovery_of=None, dedup_args=None):
        return await self.execution._run_skill(args, tool=tool, recovery_of=recovery_of, dedup_args=dedup_args)
