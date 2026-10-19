"""Installed project fixture and real Runtime paths; choices here are protocol tests."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from robot_rsi.contracts import Profile
from robot_rsi.runtime import Runtime

pytest.importorskip("lab_adapter")
ROOT = Path(__file__).parents[1]
CONDITIONS = {"dataset": "authored_lab"}


async def test_ambiguous_installed_candidate_ids_are_rejected(profile, monkeypatch):
    import lab_adapter

    from robot_rsi.contracts import HarnessError

    original = lab_adapter.factory

    def duplicate(context):
        result = original(context)
        result.candidates = (*result.candidates, result.candidates[0])
        return result

    duplicate.config_model = original.config_model
    monkeypatch.setattr(lab_adapter, "factory", duplicate)
    with pytest.raises(HarnessError) as error:
        Runtime(configured(profile))
    assert error.value.code == "ambiguous_candidate"


async def test_actual_camera_trial_checks_asset_dimensions_not_self_report(profile, monkeypatch):
    async with Runtime(configured(profile)) as runtime:
        survey = runtime.scenes.candidates["survey_camera"].bindings.camera

        async def wrong_capture(*args, **kwargs):
            return await survey.capture(*args, **kwargs)

        monkeypatch.setattr(
            runtime.scenes.candidates["detail_camera"].bindings.camera, "capture", wrong_capture
        )
        checked = await call(
            runtime, "binding_validate", {"selection": selection(), "trials": trials()}
        )
        assert not checked["valid"]
        assert (
            checked["results"]["camera"]["outputs"][0]["issue"]["code"]
            == "trial_calibration_mismatch"
        )
        evidence("functional-failure", validation=checked)


async def test_cancelled_task_does_not_execute_selected_read_compute_ports(profile):
    async with Runtime(configured(profile)) as runtime:
        ref, _ = await save(runtime)
        await begin(runtime, "cancelled", ref)
        before = runtime.scenes.candidates["detail_camera"].bindings.camera.calls
        await call(runtime, "task_cancel", {"task_id": "cancelled"})
        reply = await runtime.invoke("camera_capture", {}, task_id="cancelled")
        assert reply["error"]["code"] == "task_closed"
        assert runtime.scenes.candidates["detail_camera"].bindings.camera.calls == before


def configured(profile, **options):
    host = Profile(
        host={
            "adapter": {"factory": "legacy_lab", "options": options},
            "cameras": None,
            "depth": None,
        }
    ).host
    return profile.model_copy(update={"robot": None, "host": host})


def selection(view="detail", version="1"):
    return {
        "scene_id": "slider_recording",
        "purpose": "offline slider calculation from a recorded view",
        "reason": "Detail view has the requested resolution and calibrated fixture height",
        "roles": {"camera": view + "_camera", "kinematics": view + "_kinematics"},
        "requirements": {"camera.capture": 1, "kinematics.fk": 1},
        "model_ref": {"id": view + "_slider", "version": version},
        "calibration_refs": [{"id": view + "_intrinsics", "version": "1"}],
        "conditions": CONDITIONS,
        "skills": ["lab_measure"],
    }


def trials(view="detail", version="1"):
    return [
        {
            "role": "camera",
            "tool": "camera_capture",
            "arguments": {"camera": "sample", "require": "preview"},
        },
        {
            "role": "kinematics",
            "tool": "kinematics_fk",
            "arguments": {
                "model_ref": {"id": view + "_slider", "version": version},
                "arm": "slider",
                "joints": {"slide": 0.3},
                "base_frame": "lab",
                "target_frame": "tip",
            },
        },
    ]


async def call(runtime, name, args=None, task_id=None):
    reply = await runtime.invoke(name, args or {}, task_id=task_id)
    assert reply["ok"], reply
    return reply["result"]


def evidence(name, **data):
    if output := os.environ.get("C03_EVIDENCE_ROOT"):
        path = Path(output)
        path.mkdir(parents=True, exist_ok=True)
        (path / (name + ".json")).write_text(
            json.dumps(
                {"qualification": "protocol_test; scripted choices, not autonomous Agent", **data},
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )


async def save(runtime, view="detail", version="1", binding_version="1", **extra):
    checked = await call(
        runtime,
        "binding_validate",
        {"selection": selection(view, version), "trials": trials(view, version), **extra},
    )
    assert checked["valid"], checked
    ref = {"id": "slider_recording", "version": binding_version}
    await call(runtime, "binding_save", {"reference": ref, "validation_id": checked["id"]})
    return ref, checked


async def begin(runtime, task_id, ref):
    return await call(
        runtime,
        "task_begin",
        {
            "task_id": task_id,
            "goal": "calculate recorded slider",
            "binding_ref": ref,
            "binding_conditions": CONDITIONS,
        },
    )


async def run(runtime, task_id, request_id):
    accepted = await call(
        runtime,
        "task_run",
        {
            "task_id": task_id,
            "task_epoch": 0,
            "request_id": request_id,
            "skill": "lab_measure",
            "parameters": {"position_m": 0.3},
        },
    )
    result = await call(runtime, "operation_get", {"operation_id": accepted["id"], "wait_s": 2})
    assert result["status"] == "succeeded", result
    return result


def enable(runtime):
    runtime.registry.register(ROOT / "examples/project_adapter/skills/measure")
    runtime.registry.enable("lab_measure", "1.0.0")


async def test_task_memory_recall_matches_context_and_filters_other_scopes_providers(profile):
    import copy

    from robot_rsi.contracts import MemoryRecall

    p = configured(profile, default_ports=False)
    async with Runtime(p) as runtime:
        enable(runtime)
        detail_ref, _ = await save(runtime)
        survey_ref, _ = await save(runtime, "survey", binding_version="survey")
        fk_choice = selection()
        fk_choice.update(
            roles={"kinematics": "detail_kinematics"},
            requirements={"kinematics.fk": 1},
            calibration_refs=[],
        )
        checked = await call(
            runtime, "binding_validate", {"selection": fk_choice, "trials": [trials()[1]]}
        )
        fk_ref = {"id": "slider_recording", "version": "fk-only"}
        await call(runtime, "binding_save", {"reference": fk_ref, "validation_id": checked["id"]})
    async with Runtime(p) as runtime:
        defaults = copy.deepcopy((runtime.memory.capabilities, runtime.memory.skill_versions))
        expected = {}
        for task_id, ref, height in [
            ("detail-memory", detail_ref, 0.25),
            ("survey-memory", survey_ref, 0.2),
        ]:
            await begin(runtime, task_id, ref)
            operation = await run(runtime, task_id, task_id + "-1")
            assert operation["result"]["tip_m"] == [0.3, 0.0, height]
            record = await call(
                runtime,
                "memory_remember",
                {
                    "operation_id": operation["id"],
                    "claim": "sliderlesson known offline result",
                    "tags": ["sliderlesson"],
                },
            )
            runtime.memory.review(
                record["id"],
                "verified",
                "Test fixture: asserted selected source, exact known point and artifact; not field experience",
            )
            expected[task_id] = record["id"]
        candidate = await call(
            runtime,
            "memory_remember",
            {
                "operation_id": operation["id"],
                "claim": "sliderlesson unreviewed candidate",
                "tags": ["sliderlesson"],
            },
        )
        base = runtime.store.get("memory", expected["detail-memory"])
        invalid = {}
        for name in ("scope", "provider", "version", "rejected"):
            record = copy.deepcopy(base)
            record["id"] = "negative_" + name
            if name == "scope":
                record["scope"]["namespace"] = "other-workspace"
            elif name == "provider":
                record["capabilities"][0]["provider_id"] = "different-provider"
            elif name == "version":
                record["skill_version"] = "0.0.0"
            else:
                record["status"] = "rejected"
            runtime.store.put("memory", record["id"], record)
            invalid[name] = record["id"]
        await begin(runtime, "fk-memory", fk_ref)
        views = []
        for _ in range(2):
            for task_id in ("detail-memory", "survey-memory"):
                direct, context = await asyncio.gather(
                    call(runtime, "memory_recall", {"query": "sliderlesson"}, task_id),
                    call(runtime, "task_context", {"task_id": task_id, "query": "sliderlesson"}),
                )
                assert [m["id"] for m in direct["records"]] == [expected[task_id]]
                assert [m["id"] for m in context["memories"]] == [expected[task_id]]
                views.append({"task": task_id, "direct": direct, "context": context["memories"]})
        explicit = await call(
            runtime,
            "memory_recall",
            {"query": "sliderlesson", "include_candidates": True},
            "survey-memory",
        )
        assert {m["id"] for m in explicit["records"]} == {
            expected["survey-memory"],
            candidate["id"],
        }
        assert not (await call(runtime, "memory_recall", {"query": "sliderlesson"}, "fk-memory"))[
            "records"
        ]
        host = await call(
            runtime, "memory_recall", {"query": "sliderlesson", "include_candidates": True}
        )
        assert host == runtime.memory.recall(
            MemoryRecall(query="sliderlesson", include_candidates=True)
        )
        assert not host["records"]
        assert (runtime.memory.capabilities, runtime.memory.skill_versions) == defaults
        evidence(
            "task-recall",
            views=views,
            explicit_candidates=explicit,
            negative_records=invalid,
            host_default=host,
        )


async def test_task_memory_recall_prefers_pinned_skill_version_over_current_catalog(
    profile, tmp_path
):
    import copy
    import shutil

    p = configured(profile, default_ports=False)
    async with Runtime(p) as runtime:
        enable(runtime)
        ref, _ = await save(runtime)
    async with Runtime(p) as runtime:
        defaults = copy.deepcopy((runtime.memory.capabilities, runtime.memory.skill_versions))
        await begin(runtime, "old-version", ref)
        old = await run(runtime, "old-version", "old-1")
        assert old["skill_version"] == "1.0.0"
        old_memory = await call(
            runtime,
            "memory_remember",
            {
                "operation_id": old["id"],
                "claim": "versionlesson old selected skill",
                "tags": ["versionlesson"],
            },
        )
        runtime.memory.review(
            old_memory["id"],
            "verified",
            "Explicit version-filter fixture; original skill result succeeded",
        )
        candidate = tmp_path / "skill-version-1-1"
        shutil.copytree(ROOT / "examples/project_adapter/skills/measure", candidate)
        manifest = json.loads((candidate / "skill.json").read_text())
        manifest["version"] = "1.1.0"
        (candidate / "skill.json").write_text(json.dumps(manifest))
        runtime.registry.register(candidate)
        from robot_rsi.contracts import HarnessError

        with pytest.raises(HarnessError) as boundary:
            runtime.registry.enable("lab_measure", "1.1.0")
        assert boundary.value.code == "task_boundary_required"
        # Finish releases activation pins; the task retains its historical version reference.
        await call(
            runtime,
            "task_finish",
            {
                "task_id": "old-version",
                "task_epoch": runtime.task_memory._load("old-version")["execution"]["epoch"],
                "outcome": "succeeded",
                "summary": "Known recording/slider result complete",
                "operation_ids": [old["id"]],
            },
        )
        runtime.registry.enable("lab_measure", "1.1.0")
        await begin(runtime, "new-version", ref)
        new = await run(runtime, "new-version", "new-1")
        assert new["skill_version"] == "1.1.0"
        new_memory = await call(
            runtime,
            "memory_remember",
            {
                "operation_id": new["id"],
                "claim": "versionlesson current selected skill",
                "tags": ["versionlesson"],
            },
        )
        runtime.memory.review(
            new_memory["id"],
            "verified",
            "Explicit version-filter fixture; new declared version uses same installed calculation",
        )
        await begin(runtime, "unpinned-version", ref)
        views = []
        for task_id, expected in [
            ("new-version", new_memory["id"]),
            ("old-version", old_memory["id"]),
            ("unpinned-version", new_memory["id"]),
            ("old-version", old_memory["id"]),
        ]:
            if task_id == "old-version":
                from robot_rsi.contracts import MemoryRecall

                # Finished-task tool admission stays closed; the historical context uses
                # the same host recall projection, with its retained version reference.
                closed = await runtime.invoke(
                    "memory_recall", {"query": "versionlesson"}, task_id=task_id
                )
                assert closed["error"]["code"] == "task_closed"
                direct = runtime.recall_memory(MemoryRecall(query="versionlesson"), task_id)
            else:
                direct = await call(runtime, "memory_recall", {"query": "versionlesson"}, task_id)
            context = await call(
                runtime, "task_context", {"task_id": task_id, "query": "versionlesson"}
            )
            assert (
                [m["id"] for m in direct["records"]]
                == [m["id"] for m in context["memories"]]
                == [expected]
            )
            views.append({"task": task_id, "records": direct["records"]})
        assert (runtime.memory.capabilities, runtime.memory.skill_versions) == defaults
        evidence(
            "skill-version-recall",
            views=views,
            current_catalog=runtime.registry.versions(),
            pinned_old=runtime.task_memory._load("old-version")["skill_versions"],
        )


async def test_public_project_demo_finishes_and_archives_both_tasks(tmp_path, capsys):
    import importlib.util
    from types import SimpleNamespace

    spec = importlib.util.spec_from_file_location(
        "project_demo", ROOT / "examples/project_adapter/demo.py"
    )
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    state = str(tmp_path / "public-demo-state")
    await demo.run(
        SimpleNamespace(
            state_root=state,
            task="first",
            selection=str(ROOT / "examples/project_adapter/example-selection.json"),
        )
    )
    first = json.loads(capsys.readouterr().out)
    await demo.run(SimpleNamespace(state_root=state, task="second", selection=None))
    second = json.loads(capsys.readouterr().out)
    for output in [first, second]:
        assert output["operation"]["status"] == output["task_result"]["outcome"] == "succeeded"
        assert output["archived"] and output["task_status"] == "finished"
        assert output["task_result"]["operation_ids"] == [output["operation"]["id"]]
    assert first["validations"] == second["validations"] == 1
    assert first["binding"]["reference"] == second["binding"]["reference"]
    p = Profile.model_validate_json(
        (ROOT / "examples/project_adapter/profile.json").read_text()
    ).model_copy(update={"state_root": state})
    async with Runtime(p) as runtime:
        assert all(
            c["archived"] and c["execution"]["status"] == "finished"
            for c in runtime.store.all("task_checkpoint")
        )
        assert (
            runtime.registry.db.execute(
                "SELECT count(*) FROM pins WHERE workspace=?", (runtime.registry.workspace,)
            ).fetchone()[0]
            == 0
        )
        evidence("demo-complete", first=first, second=second, active_pins=0)


async def test_candidate_only_binding_drives_skill_views_and_execution(profile, monkeypatch):
    async with Runtime(configured(profile, default_ports=False)) as runtime:
        enable(runtime)
        assert runtime.bindings.camera is runtime.bindings.kinematics is None
        candidates = await call(
            runtime, "binding_candidates", {"requirements": {"camera.capture": 1}}
        )
        assert len(candidates["candidates"]) == 2
        ref, checked = await save(runtime)
        await begin(runtime, "chosen", ref)
        listed = await call(runtime, "skills_list", {"query": "lab_measure"}, "chosen")
        assert [item["name"] for item in listed["skills"]] == ["lab_measure"]
        described = await call(runtime, "skill_describe", {"name": "lab_measure"}, "chosen")
        assert described["available"]
        context = await call(
            runtime,
            "task_context",
            {"task_id": "chosen", "query": "lab_measure", "selected_skill": "lab_measure"},
        )
        assert context["selected_skill"]["available"] and not context["capability_gap"]
        assert context["workspace"]["environment"]["cameras"] == ["sample"]
        workspace = await call(runtime, "workspace_read", task_id="chosen")
        station = await call(runtime, "station_describe", task_id="chosen")
        capabilities = await call(runtime, "capabilities_list", task_id="chosen")
        tools = await call(runtime, "tools_list", task_id="chosen")
        assert workspace["environment"]["cameras"] == station["cameras"] == ["sample"]
        assert "lab_measure" in station["supported_tasks"]
        assert next(skill for skill in capabilities["skills"] if skill["name"] == "lab_measure")[
            "available"
        ]
        assert next(tool for tool in tools["tools"] if tool["name"] == "camera_capture")[
            "available"
        ]
        assert not (await call(runtime, "skills_list", {"query": "lab_measure"}))["skills"]
        assert (await call(runtime, "workspace_read"))["environment"]["cameras"] == []
        result = await run(runtime, "chosen", "chosen-1")
        assert result["result"]["camera_source"] == "legacy_recording_detail"
        assert result["result"]["tip_m"] == [0.3, 0.0, 0.25]
        assert runtime.bindings.camera is runtime.registry.bindings.camera is None
        await call(
            runtime,
            "workspace_note",
            {
                "key": "operator-guide",
                "title": "Read sources",
                "text": "Recorded data remains readable when a provider needs rebinding.",
                "source": "authored fixture",
            },
        )

        def changed(*args, **kwargs):
            from robot_rsi.contracts import HarnessError

            raise HarnessError("binding_changed", "fixture dependency changed")

        monkeypatch.setattr(runtime.scenes, "resolve", changed)
        note = await call(runtime, "workspace_read", {"key": "operator-guide"}, "chosen")
        assert note["source"] == "authored fixture"
        evidence(
            "no-default-ports",
            candidates=candidates,
            validation=checked,
            skills=listed,
            context=context,
            workspace=workspace,
            station=station,
            result=result,
        )


async def test_interleaved_task_capability_sets_have_distinct_skill_cache_views(profile):
    async with Runtime(configured(profile, default_ports=False)) as runtime:
        enable(runtime)
        full_ref, _ = await save(runtime)
        await begin(runtime, "full", full_ref)
        only_fk = selection()
        only_fk["roles"] = {"kinematics": "detail_kinematics"}
        only_fk["requirements"] = {"kinematics.fk": 1}
        only_fk["calibration_refs"] = []
        checked = await call(
            runtime, "binding_validate", {"selection": only_fk, "trials": [trials()[1]]}
        )
        assert checked["valid"]
        fk_ref = {"id": "slider_recording", "version": "fk-only"}
        await call(runtime, "binding_save", {"reference": fk_ref, "validation_id": checked["id"]})
        await begin(runtime, "fk-only", fk_ref)
        await call(runtime, "task_begin", {"task_id": "empty", "goal": "lab_measure"})

        async def views():
            return await asyncio.gather(
                *(
                    call(
                        runtime,
                        "skills_list",
                        {"query": "lab_measure", "available_only": available},
                        task,
                    )
                    for task in ("full", "fk-only", "empty")
                    for available in (True, False)
                )
            )

        first = await views()
        builds = runtime.registry.counters["query_builds"]
        for _ in range(3):
            assert await views() == first
        assert runtime.registry.counters["query_builds"] == builds
        assert first[0]["skills"][0]["available"]
        assert not first[2]["skills"] and not first[4]["skills"]
        fk_missing = first[3]["skills"][0]["missing_capabilities"]
        empty_missing = first[5]["skills"][0]["missing_capabilities"]
        assert {item["name"] for item in fk_missing} == {"camera.capture"}
        assert {item["name"] for item in empty_missing} == {"camera.capture", "kinematics.fk"}
        gap = await call(runtime, "task_context", {"task_id": "fk-only", "query": "lab_measure"})
        assert gap["capability_gap"] and gap["workspace"]["environment"]["cameras"] == []
        denied = await runtime.invoke(
            "task_run",
            {
                "task_id": "fk-only",
                "task_epoch": 0,
                "request_id": "missing-camera",
                "skill": "lab_measure",
                "parameters": {},
            },
        )
        assert denied["error"]["code"] == "unsupported"
        result, last_views = await asyncio.gather(run(runtime, "full", "valid-1"), views())
        assert (
            last_views == first and result["result"]["camera_source"] == "legacy_recording_detail"
        )
        assert runtime.bindings.camera is runtime.bindings.kinematics is None
        evidence(
            "interleaved-catalog",
            views=first,
            query_builds=builds,
            cache_hits=runtime.registry.counters["query_hits"],
            gap=gap,
            denied=denied,
            result=result,
        )


async def test_candidate_only_binding_reaches_existing_client_preparation(profile):
    from robot_rsi.agent_host import AgentHost

    async with Runtime(configured(profile, default_ports=False)) as runtime:
        enable(runtime)
        ref, _ = await save(runtime)
        await call(
            runtime,
            "task_begin",
            {
                "task_id": "client",
                "goal": "lab_measure",
                "binding_ref": ref,
                "binding_conditions": CONDITIONS,
            },
        )
        request = runtime.agent_state.request("client")
        assert request["role"] == "fast", request
        captured = {}

        class Client:
            def binding(self, role, config):
                return {"client": "protocol-fixture", "role": role, "model": "no-model-call"}

            async def run(self, turn, emit):
                captured["view"] = turn["view"]
                assert turn["view"]["blocks"]["skills"]["selected_skill"]["name"] == "lab_measure"
                assert turn["view"]["blocks"]["workspace"]["environment"]["cameras"] == ["sample"]
                reply = await turn["invoke"](
                    "task_run",
                    {
                        "request_id": "client-1",
                        "skill": "lab_measure",
                        "parameters": {},
                        "task_epoch": 0,
                    },
                )
                assert reply["ok"] and reply["result"]["status"] == "succeeded", reply
                captured["operation"] = reply
                cancelled = await turn["invoke"]("task_cancel", {})
                assert cancelled["ok"], cancelled
                return {"completed": True, "returncode": 0}

        result = await AgentHost(runtime).run("client", client=Client())
        assert result["status"] in ("cancelled", "superseded"), result
        assert runtime.task_memory._load("client")["execution"]["status"] == "cancelled"
        assert runtime.bindings.camera is None
        evidence("client-preparation", request=request, captured=captured, result=result)


async def test_static_relevant_candidates_do_not_execute_old_entry_or_capture(profile):
    async with Runtime(configured(profile)) as runtime:
        reply = await call(runtime, "binding_candidates", {"requirements": {"camera.capture": 1}})
        assert len(reply["candidates"]) == 2 and all(
            c["roles"] == ["camera"] for c in reply["candidates"]
        )
        missing = await call(runtime, "binding_candidates", {"requirements": {"outcome.verify": 1}})
        assert missing["missing"] and not missing["candidates"]
        checked = await call(runtime, "binding_validate", {"selection": selection()})
        assert checked["static"]["valid"] and not checked["valid"]
        assert not runtime.store.all("camera_observation") and not runtime.store.all("operation")
        assert all(
            getattr(c.bindings.camera or c.bindings.kinematics, "calls") == 0
            for c in runtime.scenes.candidates.values()
        )
        assert "legacy_lab.operator_app" not in sys.modules
        no_save = await runtime.invoke(
            "binding_save",
            {
                "reference": {"id": "slider_recording", "version": "1"},
                "validation_id": checked["id"],
            },
        )
        assert no_save["error"]["code"] == "binding_not_validated"
        evidence("static", candidates=reply, missing=missing, static_only=checked)


async def test_real_tools_skill_and_concurrent_tasks_keep_their_selected_ports(profile):
    async with Runtime(configured(profile)) as runtime:
        enable(runtime)
        ref, checked = await save(runtime)
        await begin(runtime, "detail-task", ref)
        other_ref, _ = await save(runtime, "survey", binding_version="2")
        await begin(runtime, "survey-task", other_ref)
        detailed, survey = await asyncio.gather(
            run(runtime, "detail-task", "d-1"), run(runtime, "survey-task", "s-1")
        )
        assert detailed["result"]["camera_source"] == "legacy_recording_detail"
        assert detailed["result"]["tip_m"] == [0.3, 0.0, 0.25]
        assert survey["result"]["camera_source"] == "legacy_recording_survey"
        assert survey["result"]["tip_m"] == [0.3, 0.0, 0.2]
        assert "legacy_geometry_detail" in detailed["result"]["geometry_source"]
        assert runtime.resource_versions("detail-task")["scene_binding"] == ref
        assert runtime.resource_versions("survey-task")["scene_binding"] == other_ref
        tool = await call(runtime, "kinematics_fk", trials()[1]["arguments"], task_id="detail-task")
        assert "legacy_geometry_detail" in tool["source"]
        assert runtime.bindings.camera.view == "survey"  # process default was not changed
        evidence(
            "selected-ports", validation=checked, detail=detailed, survey=survey, direct_fk=tool
        )


async def test_saved_binding_reuses_after_restart_without_trial_and_does_not_replay_unknown(
    profile,
):
    p = configured(profile)
    async with Runtime(p) as runtime:
        enable(runtime)
        ref, _ = await save(runtime)
        await begin(runtime, "first", ref)
        result = await run(runtime, "first", "first-1")
        # Recorded interruption fixture, not a physical crash or replayed device effect.
        result["status"] = "running"
        runtime.store.put("operation", result["id"], result)
        validation_count = len(runtime.store.all("binding_validation"))
    async with Runtime(p) as runtime:
        read = await call(runtime, "binding_read", {"reference": ref, "conditions": CONDITIONS})
        assert read["valid"]
        assert all(
            (c.bindings.camera or c.bindings.kinematics).calls == 0
            for c in runtime.scenes.candidates.values()
        )
        previous = await call(runtime, "operation_get", {"operation_id": result["id"]})
        assert previous["status"] == "unknown"
        duplicate = await call(
            runtime,
            "task_run",
            {
                "task_id": "first",
                "task_epoch": 0,
                "request_id": "first-1",
                "skill": "lab_measure",
                "parameters": {"position_m": 0.3},
            },
        )
        assert duplicate["id"] == result["id"] and duplicate["status"] == "unknown"
        await begin(runtime, "second", ref)
        second = await run(runtime, "second", "second-1")
        assert len(runtime.store.all("binding_validation")) == validation_count
        assert runtime.scenes.candidates["detail_camera"].bindings.camera.calls == 1
        assert runtime.scenes.candidates["detail_kinematics"].bindings.kinematics.calls == 1
        evidence(
            "restart-reuse",
            binding=read,
            interrupted=previous,
            second=second,
            validations=validation_count,
        )


async def test_related_change_revalidates_only_affected_port_unrelated_change_is_ignored(profile):
    async with Runtime(configured(profile)) as runtime:
        ref, original = await save(runtime)
    async with Runtime(
        configured(profile, survey_revision="2", survey_description="unrelated revised notes")
    ) as runtime:
        assert (await call(runtime, "binding_read", {"reference": ref}))["valid"]
        assert not runtime.store.all("operation")
    async with Runtime(configured(profile, detail_geometry_revision="2")) as runtime:
        changed = await call(runtime, "binding_read", {"reference": ref})
        assert [item["role"] for item in changed["changes"]] == ["kinematics"]
        blocked = await runtime.invoke(
            "task_begin",
            {
                "task_id": "stale",
                "goal": "use stale selection",
                "binding_ref": ref,
                "binding_conditions": CONDITIONS,
            },
        )
        assert blocked["error"]["code"] == "binding_changed"
        checked = await call(
            runtime,
            "binding_validate",
            {
                "selection": selection(version="2"),
                "previous": ref,
                "trials": [trials(version="2")[1]],
            },
        )
        assert checked["valid"]
        assert checked["results"]["camera"]["reused_validation_id"] == original["id"]
        assert runtime.scenes.candidates["detail_camera"].bindings.camera.calls == 0
        assert runtime.scenes.candidates["detail_kinematics"].bindings.kinematics.calls == 1
        new_ref = {"id": "slider_recording", "version": "2"}
        await call(runtime, "binding_save", {"reference": new_ref, "validation_id": checked["id"]})
        await begin(runtime, "changed", new_ref)
        result = await call(
            runtime, "kinematics_fk", trials(version="2")[1]["arguments"], "changed"
        )
        assert result["pose"]["matrix"][2][3] == 0.26
        evidence(
            "related-change", changed=changed, blocked=blocked, revalidation=checked, result=result
        )


async def test_evidence_cannot_authorize_a_different_choice_or_replace_a_version(profile):
    async with Runtime(configured(profile)) as runtime:
        ref, checked = await save(runtime)
        mismatch = await runtime.invoke(
            "binding_save",
            {"reference": ref, "validation_id": checked["id"], "selection": selection("survey")},
        )
        assert mismatch["error"]["code"] == "invalid_arguments"
        survey = await call(
            runtime,
            "binding_validate",
            {"selection": selection("survey"), "trials": trials("survey")},
        )
        conflict = await runtime.invoke(
            "binding_save", {"reference": ref, "validation_id": survey["id"]}
        )
        assert conflict["error"]["code"] == "version_conflict"
        wrong = selection()
        wrong["model_ref"] = {"id": "survey_slider", "version": "1"}
        invalid = await call(runtime, "binding_validate", {"selection": wrong})
        assert invalid["static"]["issue"]["code"] == "binding_asset_mismatch"
        wrong = selection()
        wrong["roles"]["camera"] = "absent"
        unavailable = await call(runtime, "binding_validate", {"selection": wrong})
        assert unavailable["static"]["issue"]["code"] == "candidate_unavailable"
        unsafe = await runtime.invoke(
            "binding_validate",
            {
                "selection": selection(),
                "trials": [{"role": "camera", "tool": "robot_command", "arguments": {}}],
            },
        )
        assert unsafe["error"]["code"] == "invalid_arguments"
        wrong_trial = await call(
            runtime, "binding_validate", {"selection": selection(), "trials": trials("survey")}
        )
        assert not wrong_trial["valid"]
        assert (
            wrong_trial["results"]["kinematics"]["outputs"][0]["issue"]["code"]
            == "task_version_mismatch"
        )
        evidence(
            "validation-integrity",
            conflict=conflict,
            invalid=invalid,
            unavailable=unavailable,
            wrong_trial=wrong_trial,
        )


async def test_binding_attachment_conditions_context_and_installation_boundary(profile):
    async with Runtime(configured(profile)) as runtime:
        enable(runtime)
        await call(runtime, "task_begin", {"task_id": "first", "goal": "choose recording"})
        checked = await call(
            runtime, "binding_validate", {"selection": selection(), "trials": trials()}, "first"
        )
        ref = {"id": "slider_recording", "version": "1"}
        await call(
            runtime,
            "binding_save",
            {"reference": ref, "validation_id": checked["id"], "attach_to_task": True},
            "first",
        )
        context = await call(runtime, "task_context", {"task_id": "first", "blocks": True})
        assert context["blocks"]["scene_binding"]["reference"] == ref
        serialized = json.dumps(context)
        assert "operator_app" not in serialized and "source_analysis" not in serialized
        repeated = await call(
            runtime,
            "task_context",
            {"task_id": "first", "blocks": True, "delivered": context["delivered"]},
        )
        assert "scene_binding" in repeated["unchanged"]
        checkpoint = await call(runtime, "task_checkpoint_get", {"task_id": "first"})
        fields = {
            key: checkpoint[key]
            for key in (
                "task_id",
                "goal",
                "constraints",
                "skill_versions",
                "last_confirmed",
                "operation_ids",
                "unknowns",
                "remaining_budget",
                "evidence_refs",
            )
        }
        fields["last_confirmed"] = "binding selected; calculation next"
        await call(runtime, "task_checkpoint_save", fields)
        assert runtime.resource_versions("first")["scene_binding"] == ref
        wrong = await runtime.invoke(
            "task_begin",
            {
                "task_id": "wrong",
                "goal": "different conditions",
                "binding_ref": ref,
                "binding_conditions": {"dataset": "live"},
            },
        )
        assert wrong["error"]["code"] == "binding_changed"
        result = await run(runtime, "first", "once")
        reject = await runtime.invoke(
            "binding_save",
            {"reference": ref, "validation_id": checked["id"], "attach_to_task": True},
            task_id="first",
        )
        assert reject["error"]["code"] == "new_task_required"
        runtime.task_memory.installation_digest = "changed-installation"
        install = await runtime.invoke("kinematics_fk", trials()[1]["arguments"], task_id="first")
        assert install["error"]["code"] == "installation_changed"
        evidence(
            "context-boundary",
            context=context,
            repeated=repeated,
            wrong_conditions=wrong,
            result=result,
            installation=install,
        )
