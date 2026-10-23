"""Skill admission, fixed versions and operation lifecycle; independent of application/client/host."""
from .contracts import HarnessError
from .operations import operation_resources

class Execution:
    def __init__(self, store, registry, operations, task_memory, *, bindings, task_bindings, skill_context):
        self.store, self.registry, self.operations, self.task_memory = store, registry, operations, task_memory
        self._bindings, self.task_bindings, self.skill_context = bindings, task_bindings, skill_context

    @property
    def bindings(self):
        return self._bindings()

    def restore(self):
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
