import json
import os
from pathlib import Path

import pytest

from robot_rsi.catalog import SKILLS
from robot_rsi.runtime import Runtime
from robot_rsi.storage import canonical


async def test_unsupported_goal_reports_capability_gap_without_motion(profile, b01_evidence):
    async with Runtime(profile) as runtime:
        started = await runtime.invoke("task_begin", {"task_id": "unsupported", "goal": "焊接零件"})
        assert started["ok"]
        response = await runtime.invoke("task_context", {"task_id": "unsupported"})
        assert response["ok"]
        context = response["result"]
        assert context["skill_candidates"] == []
        assert context["capability_gap"]["reason"] == "no_matching_skill"
        assert not runtime.robot.commands and runtime.store.all("operation") == []
        b01_evidence(runtime, context=context)


@pytest.mark.parametrize("size", [10, 100, 1000])
async def test_task_context_growth_remains_bounded(profile, monkeypatch, size):
    fixtures = [
        {
            **SKILLS[0],
            "name": f"directory_fixture_{i}",
            "description": "无关目录测试正文" * 100,
            "keywords": [],
            "kind": "agent_workflow",
            "requires": {},
        }
        for i in range(size)
    ]
    async with Runtime(profile) as runtime:
        runtime.registry.seed_fixtures(fixtures)
        started = await runtime.invoke(
            "task_begin", {"task_id": "growth", "goal": "抓取", "constraints": ["关键约束必须保留"]}
        )
        assert started["ok"]
        for i in range(size):
            runtime.store.put(
                "memory",
                f"memory-fixture-{i}",
                {
                    "id": f"memory-fixture-{i}",
                    "claim": "抓取相关经验" if i < 4 else "无关历史测试正文" * 100,
                    "tags": [],
                    "status": "verified",
                    "scope": runtime.store.scope,
                    "skill_name": "pick_place",
                    "skill_version": SKILLS[0]["version"],
                    "capabilities": runtime.bindings.catalog(),
                    "source": "retrieval_fixture_only",
                },
            )
        response = await runtime.invoke(
            "task_context", {"task_id": "growth", "selected_skill": "pick_place"}
        )
        assert response["ok"], response
        context = response["result"]
        body = canonical(context)
        assert [card["name"] for card in context["skill_candidates"]] == ["pick_place"]
        assert context["selected_skill"]["name"] == "pick_place"
        assert len(context["memories"]) == 3
        assert all("抓取相关经验" == memory["claim"] for memory in context["memories"])
        assert "无关目录测试正文" not in body and "无关历史测试正文" not in body
        assert context["checkpoint"]["constraints"] == ["关键约束必须保留"]
        assert (
            context["metrics"]["returned_bytes"]
            == len(body.encode())
            <= profile.task_limits.context_bytes
        )
        destination = os.environ.get("B01_EVIDENCE_ROOT")
        if destination:
            root = Path(destination)
            root.mkdir(parents=True, exist_ok=True)
            (root / f"growth-{size}.json").write_text(
                json.dumps(
                    {
                        "size": size,
                        "qualification": "retrieval_fixture_only; no executable skills added",
                        "skill_candidates": len(context["skill_candidates"]),
                        "selected_contracts": 1,
                        "memories": len(context["memories"]),
                        "body_bytes": len(body.encode()),
                        "retrieval_ms": context["metrics"]["retrieval_ms"],
                        "model_tokens": None,
                        "body_serialization": "canonical JSON UTF-8; excludes protocol envelope and client schemas",
                        "irrelevant_text_present": False,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )
