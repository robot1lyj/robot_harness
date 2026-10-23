"""Project wheel handoff to the existing local host updater; no plugin/action execution."""
import json
import uuid
import shutil
import tempfile
from pathlib import Path

from pydantic import Field

from .contracts import Contract, Identifier, Profile
from .host_boundary import sha256, verify
from .host_install import update
from .storage import Store


class CandidateNoProgress(ValueError):
    pass


class CandidateWheel(Contract):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CandidateTask(Contract):
    profile: str
    task_id: Identifier


class CandidateInstall(Contract):
    wheels: list[CandidateWheel] = Field(min_length=1, max_length=16)
    reason: str = Field(min_length=1, max_length=1000)
    tasks: list[CandidateTask] = Field(default_factory=list, max_length=16,
        description="Current affected tasks: finish/archive or hand off first; use new sourced task IDs after installation")


def boundary(project, request, local_file):
    """Read selected task owners without constructing Runtime or importing providers."""
    sources = []
    for reference in request.tasks:
        path = local_file(project, reference.profile, evidence=True)
        data = json.loads(path.read_text())
        for obj, fields in ((data, ("state_root",)), (data.get("project") or {}, ("root", "usage_root"))):
            for key in fields:
                if obj.get(key):
                    obj[key] = str((project / Path(obj[key]).expanduser()).resolve())
        profile = Profile.model_validate(data)
        if not Path(profile.state_root).resolve().is_relative_to(project):
            raise ValueError("Candidate task evidence must use this project's Store")
        store = Store(profile)
        try:
            task = store.get("task_checkpoint", "task-checkpoint_" + reference.task_id)
            operations = [store.get("operation", identity) for identity in task["operation_ids"]]
            if any(op["status"] in ("accepted", "running") for op in operations):
                raise ValueError("Wait/cancel the affected operation before installing a candidate")
            if not task.get("archived") and not task.get("task_result") and task.get("execution", {}).get("status") != "cancelled":
                raise ValueError("Finish/archive or hand off the affected task first; do not resume it under new code")
            sources.append({**reference.model_dump(), "scope": store.scope,
                            "outcome": (task.get("task_result") or {}).get("outcome"),
                            "archived": task["archived"], "operation_ids": task["operation_ids"]})
        finally:
            store.close()
    return sources


def install(root, project, record, request, local_file, *, allow_network=False, persist=None):
    selected = []
    for wheel in request.wheels:
        path = local_file(project, wheel.path, evidence=True)
        if path.suffix != ".whl" or sha256(path) != wheel.sha256:
            raise ValueError("Candidate wheel must match the selected project artifact SHA256")
        selected.append(path)
    fingerprint = sorted(w.sha256 for w in request.wheels)
    history = record.setdefault("candidate_installs", [])
    current = verify(root)
    installed = {item["sha256"] for item in current["local_wheels"]}
    retry = record.pop("candidate_retry", False)
    if set(fingerprint) <= installed or any(item["fingerprint"] == fingerprint and (not retry or item["status"] == "installed") for item in history):
        raise CandidateNoProgress("Candidate is unchanged/already attempted; stop without another install or model turn")
    sources = boundary(project, request, local_file)
    target = root.parent / ("coding-candidate-" + uuid.uuid4().hex)
    receipt = {"from_host": str(root), "host": str(target), "fingerprint": fingerprint,
               "wheels": [w.model_dump() for w in request.wheels], "source_tasks": sources,
               "reason": request.reason, "status": "installing"}
    history.append(receipt)
    if persist:
        persist(record)
    # The caller persists this same original coding_delivery after return/failure.
    try:
        with tempfile.TemporaryDirectory(prefix="coding-wheels-", dir=root.parent) as directory:
            copied = []
            for source, wheel in zip(selected, request.wheels):
                destination = Path(directory) / source.name
                if destination.exists():
                    raise ValueError("Select distinct wheel filenames")
                shutil.copyfile(source, destination)
                if sha256(destination) != wheel.sha256:
                    raise ValueError("Selected project wheel changed before installation")
                copied.append(destination)
            result = update(root / "approved.json", target, copied, allow_network=allow_network)
        manifest = target / "approved.json"
        approved = json.loads(manifest.read_text())
        approved.update(coding_project=str(project), coding_origin=record.get("origin_host", str(root)))
        manifest.write_text(json.dumps(approved, ensure_ascii=False, indent=2) + "\n")
        receipt.update(status="installed", core=result["core"], entry=result["entry"],
                       local_wheels=approved["local_wheels"])
        record.update(host=str(target))
        return target, receipt
    except Exception as exc:
        receipt.update(status="failed", error=str(exc))
        raise


def resume_host(root, project, record):
    active = Path(record["host"])
    if active == root:
        return root
    origin = record.get("origin_host", record["host"])
    if str(root) != origin or active.parent != root.parent or not active.name.startswith("coding-candidate-"):
        raise ValueError("Resume through the recorded fixed host entry: " + str(active))
    base, selected = verify(root), verify(active)
    if (selected.get("coding_project") != str(project) or selected.get("coding_origin") != origin
            or selected["core"] != base["core"]
            or selected["dependencies_sha256"] != base["dependencies_sha256"]):
        raise ValueError("Candidate continuation differs from this project's approved core/dependencies")
    return active
