from __future__ import annotations
from dataclasses import dataclass
from ..contracts import HarnessError
from ..feature_contracts import BindingRead, BindingSave, BindingValidate, CandidateQuery

@dataclass
class SceneTools:
    scenes: object
    task_memory: object

    definitions = {
        'binding_candidates': (CandidateQuery, "Find at most 3 relevant installed read/compute candidates; no connection"),
        'binding_read': (BindingRead, "Read saved scene binding and only selected dependency changes; no trial"),
        'binding_validate': (BindingValidate, "Check a proposed selection and explicitly try read/compute ports; host evidence"),
        'binding_save': (BindingSave, "Save the exact host-validated selection as an immutable version; optional first task attachment"),
    }

    native_tools = ('binding_candidates', 'binding_read', 'binding_validate', 'binding_save')
    motion_tools = set()
    operation_tools = set()

    async def dispatch(self, name, args, *, task_id=None):
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
        raise HarnessError("unknown_tool", "Tool not found")
