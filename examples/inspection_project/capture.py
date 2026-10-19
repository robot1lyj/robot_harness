import json
from pathlib import Path


def load_sample(path):
    record = json.loads(Path(path).read_text())
    return {"time_ns": record["time_ns"], "samples": record["samples"], "unit": record["unit"]}
