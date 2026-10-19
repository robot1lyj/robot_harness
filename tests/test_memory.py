import pytest

from robot_rsi.contracts import HarnessError, MemoryRecall, MemoryRemember
from robot_rsi.memory import Memory
from robot_rsi.storage import Store


def evidence(store, status="succeeded"):
    store.put(
        "operation",
        "operation-1",
        {
            "id": "operation-1",
            "status": status,
            "qualification": "synthetic",
            "skill_name": "pick_place",
            "skill_version": "0.2.0",
            "tool": "task_run",
        },
    )


def test_candidates_need_explicit_host_review(profile):
    store = Store(profile)
    try:
        evidence(store)
        memory = Memory(store)
        record = memory.remember(
            MemoryRemember(claim="Synthetic grasp lesson", operation_id="operation-1")
        )
        assert not memory.recall(MemoryRecall())["records"]
        assert (
            memory.recall(MemoryRecall(include_candidates=True))["records"][0]["id"] == record["id"]
        )
        memory.review(record["id"], "verified", "Reviewed synthetic trace, only applies to mock")
        found = memory.recall(MemoryRecall(query="grasp"))["records"][0]
        assert found["qualification"] == "synthetic"
        assert found["evidence_operation"] == "operation-1"
        memory.review(record["id"], "stale", "Skill superseded")
        assert not memory.recall(MemoryRecall(include_candidates=True))["records"]
    finally:
        store.close()


def test_development_runtime_evaluation_are_separate(profile):
    identities = []
    for role in ("development", "runtime", "evaluation"):
        selected = profile.model_copy(update={"role": role})
        store = Store(selected)
        try:
            assert not store.all("memory")
            evidence(store)
            candidate = Memory(store).remember(
                MemoryRemember(claim=role, operation_id="operation-1")
            )
            artifact = store.artifact(role.encode(), "text/plain")
            for previous in identities:
                with pytest.raises(HarnessError):
                    store.get("memory", previous[0])
                with pytest.raises(HarnessError):
                    store.read_artifact(previous[1])
            identities.append((candidate["id"], artifact))
        finally:
            store.close()


def test_no_fabricated_or_unfinished_evidence(profile):
    store = Store(profile)
    try:
        memory = Memory(store)
        args = MemoryRemember(claim="claim", operation_id="operation-1")
        with pytest.raises(HarnessError):
            memory.remember(args)
        evidence(store, "running")
        with pytest.raises(HarnessError, match="finish"):
            memory.remember(args)
    finally:
        store.close()


def test_scope_and_artifact_symlinks_rejected(profile, tmp_path):
    store = Store(profile)
    identity = store.artifact(b"data", "text/plain")
    path = store.artifact_path(identity)
    path.unlink()
    outside = tmp_path / "outside"
    outside.write_text("secret")
    path.symlink_to(outside)
    with pytest.raises(HarnessError, match="outside"):
        store.read_artifact(identity)
    root = store.root
    store.close()
    changed = profile.model_copy(update={"namespace": "other"})
    (root.parent / "other").symlink_to(root)
    with pytest.raises(HarnessError, match="symlink"):
        Store(changed)


def test_scope_backend_change_rejected(profile):
    store = Store(profile)
    store.close()
    with pytest.raises(HarnessError, match="identity"):
        Store(profile.model_copy(update={"robot": "yam_legacy"}))


def test_old_skill_experience_is_not_recalled_as_current(profile):
    store = Store(profile)
    try:
        evidence(store)
        operation = store.get("operation", "operation-1")
        operation["skill_version"] = "old-version"
        store.put("operation", operation["id"], operation)
        memory = Memory(store)
        record = memory.remember(
            MemoryRemember(claim="Old version lesson", operation_id="operation-1")
        )
        memory.review(record["id"], "verified", "Valid only for old version")
        assert not memory.recall(MemoryRecall(include_candidates=True))["records"]
    finally:
        store.close()


def test_backend_binding_change_excludes_existing_lesson(profile):
    store = Store(profile)
    try:
        evidence(store)
        operation = store.get("operation", "operation-1")
        operation["capabilities"] = [{"provider_id": "backend_old"}]
        store.put("operation", operation["id"], operation)
        memory = Memory(store, capabilities=[{"provider_id": "backend_old"}])
        lesson = memory.remember(
            MemoryRemember(claim="Binding-specific lesson", operation_id="operation-1")
        )
        memory.review(lesson["id"], "verified", "Evidence for old backend only")
        assert memory.recall(MemoryRecall())["records"]
        replacement = Memory(store, capabilities=[{"provider_id": "backend_new"}])
        assert not replacement.recall(MemoryRecall())["records"]
    finally:
        store.close()
