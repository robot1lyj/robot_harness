"""Immutable model/calibration records in the original Store, selected at task boundaries."""

from .capabilities import Capability
from .contracts import HarnessError
from .resources import RobotModel
from .storage import canonical


def identity(ref):
    # Collision-free even when IDs contain separators; Store IDs need not be public identifiers.
    return canonical(ref.model_dump())


class Assets:
    def __init__(self, store, config, models=(), calibrations=()):
        self.store = store
        for kind, records in [("robot_model", models), ("calibration", calibrations)]:
            for record in records:
                if record.artifact_id:
                    store.get("artifact", record.artifact_id)
                key = identity(record.reference)
                body = record.model_dump()
                try:
                    previous = store.get(kind, key)
                except HarnessError as exc:
                    if exc.code != "not_found":
                        raise
                    store.put(kind, key, body)
                else:
                    if canonical(previous) != canonical(body):
                        raise HarnessError(
                            "version_conflict", "Versioned data cannot change in place"
                        )
        self.selected = {
            "model": config.model_ref.model_dump() if config.model_ref else None,
            "calibrations": [r.model_dump() for r in config.calibration_refs],
        }
        if config.model_ref:
            self.model(config.model_ref)
        for ref in config.calibration_refs:
            store.get("calibration", identity(ref))
        self.capabilities = tuple(
            Capability(
                name=name,
                contract_version=1,
                provider_id="versioned_data",
                implementation_version="1",
                qualification="record_provenance",
            )
            for name, present in [
                ("model.read", models or store.all("robot_model")),
                ("calibration.read", calibrations or store.all("calibration")),
            ]
            if present
        )

    def model(self, ref):
        return RobotModel.model_validate(self.store.get("robot_model", identity(ref)))

    def guard_model(self, ref, versions):
        if versions is not None and versions.get("model") != ref.model_dump():
            raise HarnessError("task_version_mismatch", "Use the model fixed by this task")
        return self.model(ref)

    def get(self, query, versions=None):
        if versions is not None and query.reference.model_dump() not in versions.get(
            "calibrations", []
        ):
            raise HarnessError("task_version_mismatch", "Use a calibration fixed by this task")
        record = self.store.get("calibration", identity(query.reference))
        reasons = []
        for field in ("resource_id", "model_ref", "stream_id", "resolution"):
            expected = record.get(field)
            actual = getattr(query, field)
            if hasattr(actual, "model_dump"):
                actual = actual.model_dump()
            if isinstance(actual, tuple):
                actual = list(actual)
            if expected is not None and canonical(expected) != canonical(actual):
                reasons.append(field + "_mismatch_or_missing")
        if (
            (transform := record.get("transform"))
            or record.get("source_frame")
            or record.get("target_frame")
        ):
            transform = transform or record
            for field in ("source_frame", "target_frame"):
                if getattr(query, field) != transform[field]:
                    reasons.append(field + "_mismatch_or_missing")
        return {
            "applicable": not reasons,
            "reasons": reasons,
            "record": record,
            "source": "versioned_store; no sampling or recalibration",
        }
