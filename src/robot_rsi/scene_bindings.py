"""Task-bound selections over installed typed ports, persisted in the existing Store."""

from __future__ import annotations
from .feature_contracts import CandidateInfo, CandidateQuery, BindingSelection, BindingRead, BindingTrial, BindingValidate, BindingSave, Role, Conditions

import hashlib
from dataclasses import dataclass, replace
from typing import Literal

from pydantic import Field

from .assets import identity
from .capabilities import SkillBindings
from .contracts import Contract, Finite, HarnessError, Identifier, VersionRef
from .installation import installation_identity
from .storage import canonical, uid

PROBES = {"camera_capture": "camera", "kinematics_fk": "kinematics", "kinematics_ik": "kinematics"}




@dataclass(frozen=True)
class Candidate:
    info: CandidateInfo
    bindings: SkillBindings
    # Trusted installed code declares which read/compute calls may be tried, never motion.
    probe_tools: tuple[str, ...] = ()

    @property
    def roles(self):
        return [role for role in ("camera", "kinematics") if getattr(self.bindings, role)]














def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class SceneBindings:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.base = runtime.bindings
        self.candidates = {}
        self._prepared = {}
        self._records = {}
        for candidate in runtime.adapter.candidates:
            if any(
                getattr(candidate.bindings, port) is not None
                for port in (
                    "scene",
                    "manipulation",
                    "verification",
                    "robot",
                    "depth",
                    "calibration",
                )
            ):
                raise HarnessError(
                    "invalid_candidate", "C03 selection changes only camera/kinematics ports"
                )
            name = candidate.info.reference.id
            if name in self.candidates or not candidate.roles:
                raise HarnessError(
                    "ambiguous_candidate", "Candidate IDs must be unique and have ports"
                )
            if any(
                tool not in PROBES or PROBES[tool] not in candidate.roles
                for tool in candidate.probe_tools
            ):
                raise HarnessError(
                    "invalid_candidate", "Only matching read/compute probes are allowed"
                )
            candidate.bindings.catalog()
            self.candidates[name] = candidate

    def find(self, query):
        found = []
        for candidate in self.candidates.values():
            if candidate.bindings.missing(query.requirements):
                continue
            summary = {
                **candidate.info.model_dump(),
                "roles": candidate.roles,
                "capabilities": candidate.bindings.catalog(),
                "probe_tools": candidate.probe_tools,
            }
            score = sum(word.lower() in canonical(summary).lower() for word in query.query.split())
            found.append((score, candidate.info.reference.id, summary))
        found.sort(key=lambda item: (-item[0], item[1]))
        return {
            "candidates": [item[2] for item in found[: query.limit]],
            "total": len(found),
            "missing": []
            if found
            else [
                {
                    "requirements": query.requirements,
                    "next": "clarify the goal or integrate the missing installed capability",
                }
            ],
            "source": "selected installed factory; no connection or source analysis",
        }

    def prepare(self, selection):
        allowed = {"camera.describe", "camera.capture", "kinematics.fk", "kinematics.ik"}
        if set(selection.requirements) - allowed:
            raise HarnessError(
                "unsupported_binding_contract",
                "C03 binding trials cover camera and kinematics only",
                details={"unsupported": sorted(set(selection.requirements) - allowed)},
            )
        key = digest(selection.model_dump())
        if key in self._prepared:
            return self._prepared[key]
        ports, dependencies = {}, {}
        install = installation_identity()
        for role, name in selection.roles.items():
            candidate = self.candidates.get(name)
            if candidate is None or role not in candidate.roles:
                raise HarnessError(
                    "candidate_unavailable",
                    "Select an installed candidate for this role",
                    details={"role": role, "candidate": name},
                )
            info = candidate.info
            if any(selection.conditions.get(k) != v for k, v in info.conditions.items()):
                raise HarnessError(
                    "binding_conditions",
                    "Candidate applicability conditions differ",
                    details={"role": role, "required": info.conditions},
                )
            ports[role] = getattr(candidate.bindings, role)
            resources = [
                r.model_dump()
                for r in self.runtime.adapter.resources
                if r.resource_id in info.resource_ids
            ]
            if {r["resource_id"] for r in resources} != set(info.resource_ids):
                raise HarnessError(
                    "candidate_unavailable", "Candidate resources are not configured"
                )
            assets = []
            if role == "kinematics":
                if selection.model_ref not in info.model_refs:
                    raise HarnessError(
                        "binding_asset_mismatch",
                        "Choose a model supported by this provider",
                        details={
                            "role": role,
                            "available": [r.model_dump() for r in info.model_refs],
                        },
                    )
                assets.append(self.runtime.assets.model(selection.model_ref).model_dump())
            if role == "camera":
                if selection.calibration_refs != info.calibration_refs:
                    raise HarnessError(
                        "binding_asset_mismatch",
                        "Choose this camera's calibration references",
                        details={
                            "role": role,
                            "available": [r.model_dump() for r in info.calibration_refs],
                        },
                    )
                for ref in info.calibration_refs:
                    asset = self.store.get("calibration", identity(ref))
                    if asset["resource_id"] not in info.resource_ids:
                        raise HarnessError("binding_asset_mismatch", "Calibration resource differs")
                    assets.append(asset)
            distribution = next(
                (
                    d
                    for d in install["dependencies"]
                    if d["name"].lower().replace("_", "-")
                    == info.distribution.lower().replace("_", "-")
                ),
                None,
            )
            if distribution is None:
                raise HarnessError(
                    "candidate_unavailable", "Candidate distribution is not installed"
                )
            dependencies[role] = digest(
                {
                    "reference": info.reference.model_dump(),
                    "capabilities": [c.model_dump() for c in ports[role].capabilities],
                    "resources": resources,
                    "assets": assets,
                    "conditions": info.conditions,
                    "distribution": distribution,
                    "runtime": install["runtime_sha256"],
                }
            )
        for capability in selection.requirements:
            role = capability.split(".")[0]
            if role in ("camera", "kinematics") and role not in ports:
                raise HarnessError(
                    "missing_selection",
                    "Select a candidate for the required role",
                    details={"role": role},
                )
        bound = replace(self.base, **ports)
        bound.require(selection.requirements)
        versions = {
            "model": selection.model_ref.model_dump() if selection.model_ref else None,
            "calibrations": [ref.model_dump() for ref in selection.calibration_refs],
        }
        self._prepared[key] = (bound, versions, dependencies)
        return self._prepared[key]

    def load(self, ref):
        key = identity(ref)
        if key not in self._records:
            self._records[key] = self.store.get("scene_binding", "scene_binding:" + key)
        return self._records[key]

    def changes(self, record, conditions=None):
        selection = BindingSelection.model_validate(record["selection"])
        changes = []
        try:
            _, _, current = self.prepare(selection)
        except HarnessError as exc:
            return [{"role": exc.details.get("role"), "reason": exc.payload()}]
        for role, prior in record["dependencies"].items():
            if current.get(role) != prior:
                changes.append({"role": role, "reason": "selected_dependency_changed"})
        if conditions is not None and any(
            conditions.get(k) != v for k, v in selection.conditions.items()
        ):
            changes.append(
                {
                    "role": "conditions",
                    "reason": "task_conditions_mismatch",
                    "required": selection.conditions,
                }
            )
        return changes

    def summary(self, record, conditions=None):
        selection = record["selection"]
        changes = self.changes(record, conditions)
        return {
            "reference": record["reference"],
            "scene_id": selection["scene_id"],
            "purpose": selection["purpose"],
            "roles": selection["roles"],
            "model_ref": selection["model_ref"],
            "calibration_refs": selection["calibration_refs"],
            "conditions": selection["conditions"],
            "skills": selection["skills"],
            "valid": not changes,
            "changes": changes,
            "validation_id": record["validation_id"],
            "qualification": "saved read/compute trial; not live readiness or motion validation",
        }

    def read(self, query, task_id=None):
        if task_id:
            ref = self.runtime.resource_versions(task_id).get("scene_binding")
            if ref:
                return self.summary(self.load(VersionRef.model_validate(ref)), query.conditions)
        if query.reference:
            return self.summary(self.load(query.reference), query.conditions)
        records = self.store.all("scene_binding")
        if query.scene_id:
            records = [r for r in records if r["selection"]["scene_id"] == query.scene_id]
            if not records:
                raise HarnessError("binding_missing", "No saved binding for this scene")
            return self.summary(records[-1], query.conditions)
        latest = {r["selection"]["scene_id"]: r for r in records}
        return {
            "bindings": [self.summary(r) for r in list(latest.values())[-3:]],
            "total": len(latest),
            "next": "binding_candidates only for missing or changed requirements",
        }

    def resolve(self, ref, conditions=None, skill=None):
        record = self.load(ref)
        if changes := self.changes(record, conditions):
            raise HarnessError(
                "binding_changed",
                "Revalidate only the affected selection",
                details={"changes": changes},
            )
        selection = BindingSelection.model_validate(record["selection"])
        if skill and selection.skills and skill not in selection.skills:
            raise HarnessError("binding_skill_mismatch", "Skill is outside this binding's scope")
        bound, versions, _ = self.prepare(selection)
        return bound, {**versions, "scene_binding": ref.model_dump()}

    async def validate(self, query):
        selection = query.selection
        try:
            bound, versions, deps = self.prepare(selection)
        except HarnessError as exc:
            return {
                "valid": False,
                "static": {"valid": False, "issue": exc.payload()},
                "trials": [],
            }
        previous = self.load(query.previous) if query.previous else None
        previous_validation = (
            self.store.get("binding_validation", previous["validation_id"]) if previous else None
        )
        if any(trial.role not in selection.roles for trial in query.trials):
            raise HarnessError("missing_selection", "Trial role must be selected")
        results = {}
        missing = []
        for role, name in selection.roles.items():
            candidate = self.candidates[name]
            trials = [t for t in query.trials if t.role == role]
            prior = (previous_validation or {}).get("results", {}).get(role)
            if (
                not trials
                and previous
                and previous["dependencies"].get(role) == deps[role]
                and previous["selection"]["conditions"] == selection.conditions
                and prior
                and prior["valid"]
            ):
                results[role] = {**prior, "reused_validation_id": previous["validation_id"]}
                continue
            outputs = []
            tested = set()
            for trial in trials:
                if PROBES[trial.tool] != role or trial.tool not in candidate.probe_tools:
                    raise HarnessError(
                        "unsafe_binding_trial", "Trial must match the installed read/compute port"
                    )
                from .tool_specs import TOOLS

                arguments = TOOLS[trial.tool][0].model_validate(trial.arguments)
                try:
                    with self.runtime.binding_scope(bound, versions):
                        output = await self.runtime._dispatch(trial.tool, arguments)
                    if trial.tool == "camera_capture":
                        from .perception import CameraObservation
                        from .resources import CalibrationQuery

                        observation = CameraObservation.model_validate(output)
                        for frame in observation.frames:
                            self.store.read_artifact(frame.artifact_id)
                        for ref in selection.calibration_refs:
                            record = self.store.get("calibration", identity(ref))
                            frame = next(
                                (
                                    f
                                    for f in observation.frames
                                    if f.stream_id == record["stream_id"]
                                ),
                                None,
                            )
                            if frame is None:
                                raise HarnessError(
                                    "trial_calibration_mismatch", "Calibration stream is absent"
                                )
                            checked = self.runtime.assets.get(
                                CalibrationQuery(
                                    reference=ref,
                                    resource_id=record["resource_id"],
                                    stream_id=frame.stream_id,
                                    resolution=(frame.width, frame.height),
                                )
                            )
                            if not checked["applicable"] or (
                                record.get("intrinsics")
                                and (
                                    frame.intrinsics is None
                                    or frame.intrinsics.model_dump() != record["intrinsics"]
                                )
                            ):
                                raise HarnessError(
                                    "trial_calibration_mismatch",
                                    "Observed frame and calibration differ",
                                )
                    if trial.tool == "kinematics_ik" and output["status"] != "solved":
                        raise HarnessError(
                            "trial_not_solved", "No solved IK evidence for this input"
                        )
                    outputs.append(
                        {"tool": trial.tool, "arguments": arguments.model_dump(), "result": output}
                    )
                    tested.add(
                        {
                            "camera_capture": "camera.capture",
                            "kinematics_fk": "kinematics.fk",
                            "kinematics_ik": "kinematics.ik",
                        }[trial.tool]
                    )
                except Exception as exc:
                    outputs.append(
                        {
                            "tool": trial.tool,
                            "issue": exc.payload()
                            if isinstance(exc, HarnessError)
                            else {"code": type(exc).__name__, "message": str(exc)},
                        }
                    )
            required = {
                cap
                for cap in selection.requirements
                if cap.startswith(role + ".") and not cap.endswith(".describe")
            }
            valid = (
                bool(outputs)
                and not any("issue" in item for item in outputs)
                and required <= tested
            )
            results[role] = {
                "valid": valid,
                "tested": sorted(tested),
                "outputs": outputs,
                "candidate": candidate.info.reference.model_dump(),
            }
            if not valid:
                missing.append({"role": role, "required_trials": sorted(required) or [role]})
        # Reused evidence must also cover newly requested contracts.
        for role, result in results.items():
            required = {
                cap
                for cap in selection.requirements
                if cap.startswith(role + ".") and not cap.endswith(".describe")
            }
            if not required <= set(result["tested"]):
                missing.append(
                    {"role": role, "required_trials": sorted(required - set(result["tested"]))}
                )
        validation_id = uid("binding_validation")
        body = {
            "id": validation_id,
            "selection": selection.model_dump(),
            "dependencies": deps,
            "selection_digest": digest(selection.model_dump()),
            "valid": not missing,
            "static": {"valid": True},
            "results": results,
            "missing": missing,
        }
        self.store.put("binding_validation", validation_id, body)
        self.store.event(
            "binding.validated",
            {
                "id": validation_id,
                "valid": body["valid"],
                "roles_tested": [
                    r for r, value in results.items() if not value.get("reused_validation_id")
                ],
            },
        )
        return body

    def save(self, query):
        validation = self.store.get("binding_validation", query.validation_id)
        selection = BindingSelection.model_validate(validation["selection"])
        if not validation["valid"] or validation["selection_digest"] != digest(
            selection.model_dump()
        ):
            raise HarnessError("binding_not_validated", "Save requires matching host validation")
        _, _, deps = self.prepare(selection)
        if deps != validation["dependencies"]:
            raise HarnessError("binding_changed", "Selected dependencies changed since validation")
        if query.reference.id != selection.scene_id:
            raise HarnessError(
                "binding_identity_mismatch", "Reference ID must match the validated scene"
            )
        key = "scene_binding:" + identity(query.reference)
        body = {
            "reference": query.reference.model_dump(),
            "selection": selection.model_dump(),
            "dependencies": deps,
            "validation_id": query.validation_id,
        }
        try:
            previous = self.store.get("scene_binding", key)
        except HarnessError as exc:
            if exc.code != "not_found":
                raise
            self.store.put("scene_binding", key, body)
        else:
            if previous != body:
                raise HarnessError("version_conflict", "Saved binding versions are immutable")
        self._records[identity(query.reference)] = body
        return self.summary(body)
