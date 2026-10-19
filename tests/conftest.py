import json
import os
from pathlib import Path

import pytest

from robot_rsi.contracts import Profile


@pytest.fixture
def profile(tmp_path):
    return Profile(state_root=str(tmp_path / "state"), mock_step_s=0.002)


@pytest.fixture
def b01_evidence(request):
    def capture(runtime, **details):
        destination = os.environ.get("B01_EVIDENCE_ROOT")
        if destination:
            root = Path(destination)
            root.mkdir(parents=True, exist_ok=True)
            body = {
                "source": request.node.nodeid,
                "qualification": "deterministic_fixture; no actual Agent decision",
                "operations": runtime.store.all("operation"),
                "checkpoints": runtime.store.all("task_checkpoint"),
                "commands": runtime.robot.commands,
                **details,
            }
            (root / (request.node.name + ".json")).write_text(
                json.dumps(body, ensure_ascii=False, indent=2) + "\n"
            )

    return capture


@pytest.fixture
def b02_evidence(request):
    def capture(runtime, **details):
        destination = os.environ.get("B02_EVIDENCE_ROOT")
        if not destination:
            return
        root = Path(destination)
        root.mkdir(parents=True, exist_ok=True)
        checkpoints = runtime.store.all("task_checkpoint")
        files = []
        for record in checkpoints:
            state = record.get("memory_state")
            if state:
                path = runtime.store.root / state["file_ref"]
                files.append(
                    {
                        "task_id": record["task_id"],
                        "text": path.read_text(),
                        "bytes": path.stat().st_size,
                        "mtime_ns": path.stat().st_mtime_ns,
                    }
                )
        body = {
            "source": request.node.nodeid,
            "qualification": "deterministic_fixture",
            "checkpoints": checkpoints,
            "files": files,
            "operations": runtime.store.all("operation"),
            "archives": runtime.store.all("task_memory_archive"),
            "commands": runtime.robot.commands,
            **details,
        }
        (root / (request.node.name + ".json")).write_text(
            json.dumps(body, ensure_ascii=False, indent=2) + "\n"
        )

    return capture
