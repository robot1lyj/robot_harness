"""Public deterministic local-code/evaluation/release replay, not an autonomous Agent trial.

Run with a NEW --root. Phases baseline / compare / reuse can run in separate processes;
all executes baseline, a documented literal candidate patch, compare and a reuse subprocess.
No installed framework source/dependency is edited by this example.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import time
from importlib.metadata import version as distribution_version
from pathlib import Path

from pydantic import ValidationError

from robot_rsi.contracts import Profile, ProjectConfig
from robot_rsi.experience import Experience, Releases, import_evaluation, package_identity
from robot_rsi.feedback import IssueCreate, IssueGet
from robot_rsi.installation import installation_identity
from robot_rsi.resources import FKResult
from robot_rsi.runtime import Runtime

BASE = Path(__file__).parent
BUDGETS = {"commands": 0, "grasp_attempts": 0, "recoveries": 0, "duration_s": 30, "tool_calls": 20}
CASES = [{"id": "normal", "parameters": {"position_m": 0.3, "offset_mm": 0}, "expected": [0.3, 0, 0.25]},
         {"id": "offset", "parameters": {"position_m": 0.3, "offset_mm": 20}, "expected": [0.32, 0, 0.25]}]


def save(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def profile(root, role="runtime", namespace="local"):
    config = ProjectConfig.model_validate_json((root / "project.json").read_text())
    config = config.model_copy(update={"purpose": {"runtime": "run", "development": "improve", "evaluation": "evaluate"}[role]})
    return Profile.model_validate({**json.loads((BASE / "profile.json").read_text()),
                                   "state_root": str(root / "state"), "role": role,
                                   "namespace": namespace, "project": config.model_dump()})


async def call(runtime, name, arguments=None, task_id=None):
    result = await runtime.invoke(name, arguments or {}, task_id=task_id)
    assert result["ok"], result
    return result["result"]


async def binding(runtime):
    if runtime.store.all("scene_binding"):
        return (await call(runtime, "binding_read", {"scene_id": "slider_recording"}))["reference"]
    selection = json.loads((BASE / "example-selection.json").read_text())
    selection["skills"] = ["lab_offset"]
    checked = await call(runtime, "binding_validate", {"selection": selection, "trials": [
        {"role": "camera", "tool": "camera_capture", "arguments": {"require": "preview"}},
        {"role": "kinematics", "tool": "kinematics_fk", "arguments": {
            "model_ref": selection["model_ref"], "arm": "slider", "joints": {"slide": 0.3},
            "base_frame": "lab", "target_frame": "tip"}}]})
    assert checked["valid"], checked
    return (await call(runtime, "binding_save", {"reference": {"id": selection["scene_id"], "version": "1"},
                                                "validation_id": checked["id"]}))["reference"]


def input_goal(parameters):
    return "Calculate the slider tip from inputs " + json.dumps(parameters, sort_keys=True) + "; apply the offset in millimetres using the selected model. Offline computation only."


async def task(runtime, identity, parameters, reference, constraints=()):
    """Runtime entry receives public inputs only; the oracle stays with the host scorer."""
    started = time.monotonic()
    await call(runtime, "task_begin", {"task_id": identity, "goal": input_goal(parameters),
                                      "constraints": list(constraints), "binding_ref": reference,
                                      "binding_conditions": {"dataset": "authored_lab"}})
    context = await call(runtime, "task_context", {"task_id": identity, "selected_skill": "lab_offset"})
    if runtime.profile.role == "runtime":
        assert "DEV_ONLY" not in json.dumps(context) and "EVAL_ONLY" not in json.dumps(context)
    operation = await call(runtime, "task_run", {"task_id": identity, "task_epoch": 0,
                           "request_id": identity + "-run", "skill": "lab_offset", "parameters": parameters})
    operation = await call(runtime, "operation_get", {"operation_id": operation["id"], "wait_s": 2})
    return operation, context, started


async def scored_task(runtime, identity, case, reference, constraints=()):
    """Host-side fixed comparison; only the allowed conclusion crosses back into the task."""
    operation, context, started = await task(runtime, identity, case["parameters"], reference, constraints)
    actual = (operation.get("result") or {}).get("tip_m")
    success = (operation["status"] == "succeeded" and actual is not None and
               len(actual) == 3 and all(abs(a - b) < 1e-8 for a, b in zip(actual, case["expected"], strict=True)))
    checkpoint = await call(runtime, "task_checkpoint_get", {"task_id": identity})
    ended = await call(runtime, "task_finish", {"task_id": identity, "task_epoch": checkpoint["execution"]["epoch"],
                        "outcome": "succeeded" if success else "failed",
                        "summary": f"Input {case['parameters']}; observed {actual}; host comparison {'passed' if success else 'failed'}; offline only",
                        "operation_ids": [operation["id"]]})
    archived = await call(runtime, "task_archive", {"task_id": identity})
    assert archived["archived"] and not operation["resources"]
    return {"task_id": identity, "operation": operation, "task_result": ended["task_result"], "archived": archived,
            "context_before_execution": context,
            "context_bytes": len(json.dumps(context).encode()), "scope": runtime.store.scope,
            "cost": {"commands": 0, "grasp_attempts": 0, "recoveries": 0,
                     "duration_s": time.monotonic() - started, "tool_calls": ended["execution"]["tool_calls"],
                     "assistance_decisions": 0, "model_calls": 0}}


def prepare(root):
    root.mkdir(parents=True, exist_ok=True)
    assert not (root / "frozen.json").exists(), "Choose a new root for an immutable comparison"
    project = root / "project"
    project.mkdir()
    texts = {"common": "Units: metres and millimetres. Selected authored detail fixture height=0.25 m. Pure computation; no physical access.",
             "run": "Keep known unit conversions; compare the whole target with the returned tip. End and archive each task.",
             "dev": "DEV_ONLY local hypothesis: offset conversion missing; inspect selected failure first.",
             "eval": "EVAL_ONLY frozen oracle: normal x=.3, offset x=.32; z=.25. Not runtime knowledge."}
    materials = []
    for key, text in texts.items():
        (project / (key + ".md")).write_text(text)
        purposes = {"common": ["onboarding", "run", "improve", "evaluate"], "run": ["run", "improve"],
                    "dev": ["onboarding", "improve"], "eval": ["evaluate"]}[key]
        materials.append({"key": key, "title": key + " material", "path": key + ".md", "purposes": purposes})
    save(root / "project.json", {"id": "slider_project", "root": str(project), "common_key": "common", "materials": materials})
    shutil.copytree(BASE / "skills/offset", root / "baseline")
    save(root / "frozen.json", {"cases": CASES, "budgets": BUDGETS, "installation": installation_identity(),
                                "qualification": "authored fixture; oracle and literal patch predetermined for public replay"})
    for role in ("runtime", "development", "evaluation"):
        save(root / (role + "-profile.json"), profile(root, role).model_dump())


async def baseline(root):
    prepare(root)
    async with Runtime(profile(root)) as runtime:
        runtime.registry.register(root / "baseline")
        runtime.registry.enable("lab_offset", "1.0.0")
        reference = await binding(runtime)
        rows = [await scored_task(runtime, "first-" + case["id"], case, reference) for case in CASES]
        assert rows[0]["task_result"]["outcome"] == "succeeded" and rows[1]["task_result"]["outcome"] == "failed"
        selection = Experience(runtime.store).export("first-offset")
        save(root / "selected-failure.json", selection)
        save(root / "project/selected-failure.json", selection)
        config = json.loads((root / "project.json").read_text())
        config["materials"].append({"key": "selected_failure", "title": "Selected ended offset failure",
                                    "path": "selected-failure.json", "purposes": ["improve"]})
        save(root / "project.json", config)
        # A real invalid input/selection path, retained separately from semantic routing.
        wrong = await runtime.invoke("binding_validate", {"selection": {
            **json.loads((BASE / "example-selection.json").read_text()), "roles": {"camera": "absent"}}})
        save(root / "wrong-binding.json", wrong)
        local = runtime.feedback.create(IssueCreate(route="local_extension", component="robot-rsi-lab-adapter",
            rationale="FK returns correct zero-offset position; local skill never adds offset_mm. Fix the local method.",
            expected="Apply offset_mm converted to metres before the selected-model FK", actual=str(rows[1]["operation"]["result"]),
            reproducer="lab_offset(position_m=.3, offset_mm=20)", source_task="first-offset",
            operation_ids=[rows[1]["operation"]["id"]]))
        input_issue = runtime.feedback.create(IssueCreate(route="local_input", component="robot-rsi-lab-adapter",
            rationale="Candidate absent from installed inventory; correct selection, no public interface change.",
            expected="installed camera candidate", actual=json.dumps(wrong), reproducer="binding_validate roles.camera=absent"))
        # Real public schema rejection, not a fabricated framework release or missing provider.
        valid = {"model_ref": {"id": "detail_slider", "version": "1"}, "source": "fixture", "qualification": "authored_fixture",
                 "pose": {"target_frame": "lab", "source_frame": "tip", "matrix": [[1,0,0,.3],[0,1,0,0],[0,0,1,.25],[0,0,0,1]]}}
        FKResult.model_validate(valid)
        try:
            FKResult.model_validate({**valid, "covariance_m2": [0.0001]})
        except ValidationError as exc:
            schema_error = str(exc)
        else:
            raise AssertionError("Fixture expects the documented schema gap")
        framework = runtime.feedback.create(IssueCreate(route="framework_contract", component="robot-rsi-harness",
            rationale="A standardized FK uncertainty field concerns the public typed result; local position calculation remains usable.",
            expected="Optional FK covariance with declared semantics", actual=schema_error,
            reproducer=json.dumps({**valid, "covariance_m2": [0.0001]})))
        save(root / "baseline-results.json", {"rows": rows, "issues": [input_issue, local, framework]})
        save(root / "framework-issue.json", runtime.feedback.get(IssueGet(issue_id=framework["id"])))
        save(root / "baseline-installation.json", installation_identity())
    print(json.dumps({"phase": "baseline", "root": str(root), "gap": rows[1]["task_result"]}))


async def compare(root):
    frozen = json.loads((root / "frozen.json").read_text())
    assert frozen["installation"] == installation_identity(), "Installed core/dependencies changed"
    evaluations = {}
    for label in ("bad", "good"):
        candidate = root / label
        report = {"frozen": frozen, "installation": installation_identity(),
                  "qualification": "authored offline calculation; baseline_assisted has no applicable aid, zero assistance/model calls",
                  "rows": []}
        for mode in ("baseline_independent", "baseline_assisted", "candidate_independent"):
            package = candidate if mode == "candidate_independent" else root / "baseline"
            async with Runtime(profile(root, "evaluation", label + "-" + mode)) as runtime:
                runtime.registry.register(package)
                runtime.registry.enable("lab_offset", package_identity(package)["version"])
                reference = await binding(runtime)
                for case in frozen["cases"]:
                    row = await scored_task(runtime, label + "-" + mode + "-" + case["id"], case, reference)
                    report["rows"].append({**row, "case": case["id"], "target_gap": case["id"] == "offset",
                                           "mode": mode, "outcome": row["task_result"]["outcome"],
                                           "budgets": frozen["budgets"], "skill": package_identity(package)})
        async with Runtime(profile(root, "evaluation", label + "-report")) as runtime:
            evaluations[label] = import_evaluation(runtime.store, report)
        save(root / (label + "-report.json"), report)
        save(root / (label + "-evaluation.json"), evaluations[label])
    async with Runtime(profile(root)) as runtime:
        releases = Releases(runtime.store, runtime.registry)
        bad = releases.publish(evaluations["bad"], root / "bad")
        assert bad["status"] == "rejected" and runtime.registry.describe("lab_offset")["version"] == "1.0.0"
        good = releases.publish(evaluations["good"], root / "good")
        assert good["status"] == "published" and runtime.registry.describe("lab_offset")["version"] == "1.1.0"
        original = runtime.task_memory._load("first-offset")
        assert original["task_result"]["outcome"] == "failed" and original["archived"]
        save(root / "releases.json", {"bad": bad, "good": good, "old_failure": original["task_result"]})
    print(json.dumps({"phase": "compare", "bad": bad["status"], "good": good["status"]}))


async def reuse(root):
    frozen = json.loads((root / "frozen.json").read_text())
    assert frozen["installation"] == installation_identity()
    async with Runtime(profile(root)) as runtime:
        validations = len(runtime.store.all("binding_validation"))
        reference = await binding(runtime)
        row = await scored_task(runtime, "released-new-process", frozen["cases"][1], reference,
                         constraints=["Verification of first-offset/local issue; original failed task remains failed."])
        assert row["task_result"]["outcome"] == "succeeded" and runtime.registry.describe("lab_offset")["version"] == "1.1.0"
        assert len(runtime.store.all("binding_validation")) == validations
        assert runtime.registry.db.execute("SELECT count(*) FROM pins").fetchone()[0] == 0
        save(root / "reuse.json", {**row, "installation": installation_identity(), "binding_validations": validations,
                                  "active_pins": 0, "qualification": "fresh host process protocol reuse, not native Agent autonomy"})
    print(json.dumps({"phase": "reuse", "outcome": row["task_result"]["outcome"]}))


def candidates(root):
    # Public deterministic replay of the documented fix. The Sol evidence separately records
    # reading the actual baseline return and source before creating these candidates.
    for name, version, expression in [("good", "1.1.0", "args.position_m + args.offset_mm / 1000"),
                                      ("bad", "1.2.0", "args.position_m * 2 + args.offset_mm / 1000")]:
        directory = root / name
        shutil.copytree(root / "baseline", directory)
        manifest = json.loads((directory / "skill.json").read_text())
        manifest["version"] = version
        save(directory / "skill.json", manifest)
        source = (directory / "skill.py").read_text()
        assert source.count("position = args.position_m\n") == 1
        (directory / "skill.py").write_text(source.replace("position = args.position_m\n", "position = " + expression + "\n"))
        (directory / "SKILL.md").write_text("# Local slider calculation\n\nUse lab_offset with position_m and offset_mm. Pure computation; compare the returned tip with the whole goal.\n")


def feedback(root):
    core_version = distribution_version("robot-rsi-harness")
    issue = json.loads((root / "framework-issue.json").read_text())["issue"]
    prefix = [sys.executable, "-m", "robot_rsi.cli", "--profile", str(root / "runtime-profile.json"), "feedback"]

    def exchange(arguments, name):
        completed = subprocess.run([*prefix, *arguments], check=True, capture_output=True, text=True)
        result = json.loads(completed.stdout)
        save(root / name, result)
        return result

    exchange(["export", issue["id"], "--output", str(root / "issue-outbox.json")], "issue-export.json")
    reply = {"id": "fixture-future", "issue_id": issue["id"], "project_id": issue["project_id"],
             "component": issue["component"], "affected_versions": "==" + core_version, "target_version": "99.0.0",
             "disposition": "update_available", "handling": "framework", "contracts": ["kinematics.fk"],
             "summary": "Simulated optional covariance proposal; no real framework release",
             "details": "Fixture file supplied by the example host, not an actual maintainer response or installed implementation.",
             "migration": "If actually released, end old tasks, install a pinned wheel and recompare selected dependencies.",
             "validation": "Reproduce covariance semantics on an independently evaluated task before use.",
             "provenance": "simulated_fixture"}
    variants = [("future", {}), ("needs", {"disposition": "needs_information", "target_version": None,
                                         "summary": "Need covariance frame/unit definition"}),
                ("range", {"affected_versions": ">=9"}),
                ("unrelated", {"contracts": ["unselected.contract"]}),
                ("present", {"target_version": core_version, "disposition": "answered",
                             "summary": "Fixture acknowledgement of already-present position contract; no covariance implementation or upgrade"})]
    for name, changes in variants:
        value = {**reply, "id": "fixture-" + name, **changes}
        file = root / ("reply-" + name + ".json")
        save(file, value)
        exchange(["import-reply", str(file)], "import-" + name + ".json")
    op_id = json.loads((root / "reuse.json").read_text())["operation"]["id"]
    exchange(["attest", "fixture-present", "--task-id", "released-new-process", "--operation-id", op_id,
              "--note", "Selected position-only task passed on the current fixed installation; covariance remains unimplemented."], "attestation.json")
    exchange(["status", issue["id"]], "feedback-status.json")
    page = exchange(["status"], "feedback-page-1.json")
    number = 1
    while page["next_cursor"]:
        number += 1
        page = exchange(["status", "--cursor", page["next_cursor"]], f"feedback-page-{number}.json")
    exchange(["status", "--kind", "updates"], "feedback-updates.json")
    exchange(["status", "--kind", "pending"], "feedback-pending.json")


async def measure_views(root):
    async def view(identity):
        async with Runtime(profile(root)) as runtime:
            reference = await binding(runtime)
            await call(runtime, "task_begin", {"task_id": identity, "goal": "Read project entry only",
                                               "binding_ref": reference, "binding_conditions": {"dataset": "authored_lab"}})
            workspace = await call(runtime, "workspace_read", task_id=identity)
            context = await call(runtime, "task_context", {"task_id": identity, "selected_skill": "lab_offset", "blocks": True})
            repeated = await call(runtime, "task_context", {"task_id": identity, "selected_skill": "lab_offset",
                                                            "blocks": True, "delivered": context["delivered"]})
            assert repeated["blocks"] == {}
            assert len(workspace["project"]["materials"]) <= 3
            text = json.dumps([workspace, context])
            assert all(marker not in text for marker in ("DEV_ONLY", "EVAL_ONLY", "UNRELATED_BODY"))
            assert "metres and millimetres" in text and "run" in str(workspace["project"]["materials"])
            await call(runtime, "task_cancel", {"task_id": identity})
            await call(runtime, "task_archive", {"task_id": identity})
            return {"workspace_bytes": len(json.dumps(workspace).encode()), "task_block_bytes": context["metrics"]["block_bytes"],
                    "repeat_block_bytes": repeated["metrics"]["block_bytes"], "cards": workspace["project"]["materials"],
                    "visible_count": workspace["project"]["visible_count"], "model_tokens": None}
    before = await view("view-before")
    config = json.loads((root / "project.json").read_text())
    for index in range(200):
        key = f"z_history_{index:03}"
        (root / "project" / (key + ".md")).write_text("UNRELATED_BODY " * 500)
        config["materials"].append({"key": key, "title": "Unrelated old note " + str(index),
                                    "path": key + ".md", "purposes": ["run", "improve"]})
    save(root / "project.json", config)
    after = await view("view-after")
    assert after["workspace_bytes"] < before["workspace_bytes"] + 1000
    async with Runtime(profile(root, "development")) as runtime:
        selected = await call(runtime, "project_read", {"key": "selected_failure"})
        assert "first-offset" in selected["text"]
        assert "DEV_ONLY" in (await call(runtime, "project_read", {"key": "dev"}))["text"]
    save(root / "view-measurements.json", {"before": before, "after": after,
                                           "added_files": 200, "added_body_bytes": 7500 * 200,
                                           "development_selected_failure_read": True})


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--phase", choices=["all", "baseline", "compare", "reuse"], default="all")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.phase == "all":
        asyncio.run(baseline(root))
        candidates(root)
        asyncio.run(compare(root))
        subprocess.run([sys.executable, str(Path(__file__).resolve()), "--root", str(root), "--phase", "reuse"], check=True)
        feedback(root)
        asyncio.run(measure_views(root))
    else:
        asyncio.run({"baseline": baseline, "compare": compare, "reuse": reuse}[args.phase](root))


if __name__ == "__main__":
    main()
