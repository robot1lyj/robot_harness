from __future__ import annotations
from dataclasses import dataclass
from ..contracts import Describe, Empty, HarnessError

from ..capabilities import PICK_PLACE_REQUIREMENTS
from ..catalog import public_descriptor

@dataclass
class DiscoveryTools:
    adapter: object
    _bindings: object
    depth: object
    operations: object
    profile: object
    provider: object
    robot: object
    skill_descriptors: object
    store: object
    tools: object

    definitions = {
        'capabilities_list': (
        Empty,
        "List bound capability contracts, versions, providers and skill compatibility",
    ),
        'station_describe': (
        Empty,
        "Discover station, supported tasks, frames and integration blockers; no hardware access",
    ),
        'tools_list': (Empty, "List capabilities, availability and compact descriptions"),
        'tool_describe': (Describe, "Get one tool's complete input schema"),
    }

    native_tools = ('tool_describe', 'tools_list', 'capabilities_list')
    motion_tools = set()
    operation_tools = set()

    @property
    def bindings(self):
        return self._bindings()

    async def dispatch(self, name, args, *, task_id=None):
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
        if name == "tools_list":
            return self.tools()
        if name == "tool_describe":
            from ..tool_catalog import TOOLS
            if args.name not in TOOLS:
                raise HarnessError("unknown_tool", "Tool not found")
            schema, description = TOOLS[args.name]
            return {
                "name": args.name,
                "description": description,
                "input_schema": schema.model_json_schema(),
            }
        raise HarnessError("unknown_tool", "Tool not found")
