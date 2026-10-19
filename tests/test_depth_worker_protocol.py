"""Exercise real subprocess IPC, reuse and cancellation using a declared fake worker.

These tests do NOT validate NVIDIA model execution, CUDA compatibility or image quality.
"""

import asyncio
import sys

import numpy as np
import pytest

import robot_rsi.depth as depth_module
from robot_rsi.contracts import FFSConfig, HarnessError
from robot_rsi.depth import FastFoundationStereo


@pytest.fixture
def worker(tmp_path, monkeypatch):
    repo = tmp_path / "fake-source"
    (repo / "core/utils").mkdir(parents=True)
    (repo / "core/utils/utils.py").write_text("# test fixture, not NVIDIA source\n")
    checkpoint = tmp_path / "fixture.pth"
    checkpoint.write_text("not a model")
    monkeypatch.setattr(depth_module, "__file__", str(tmp_path / "depth.py"))

    def make(delay=0, timeout=3):
        (tmp_path / "ffs_worker.py").write_text(f"""
import json, sys, time
import numpy as np
print(json.dumps({{"ready": True, "model": {{"provider": "PROTOCOL_TEST_DOUBLE"}}}}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    time.sleep({delay})
    np.save(request["output"], np.full((16, 32), 5, dtype=np.float32), allow_pickle=False)
    print(json.dumps({{"ok": True, "inference_s": {delay}}}), flush=True)
""")
        return FastFoundationStereo(
            FFSConfig(
                python=sys.executable,
                repository=str(repo),
                checkpoint=str(checkpoint),
                timeout_s=timeout,
            )
        )

    return make


async def test_worker_reused_without_reloading(worker):
    provider = worker()
    try:
        disparity, provenance = await provider.predict(b"left-fixture", b"right-fixture")
        pid = provider.process.pid
        assert np.all(disparity == 5)
        assert provenance["model"]["provider"] == "PROTOCOL_TEST_DOUBLE"
        await provider.predict(b"left-fixture", b"right-fixture")
        assert provider.process.pid == pid
    finally:
        process = provider.process
        await provider.close()
    assert process.returncode is not None


async def test_deadline_kills_worker(worker):
    provider = worker(delay=2, timeout=0.4)
    with pytest.raises(HarnessError, match="deadline"):
        await provider.predict(b"left", b"right")
    assert provider.process is None


async def test_cancel_kills_worker_and_rejects_concurrent_queue(worker):
    provider = worker(delay=2)
    task = asyncio.create_task(provider.predict(b"left", b"right"))
    await asyncio.sleep(0)
    with pytest.raises(HarnessError, match="already running"):
        await provider.predict(b"new-left", b"new-right")
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.process is None
