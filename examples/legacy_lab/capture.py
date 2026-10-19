"""Existing recording reader; never opens a camera or imports the operator entry."""

import json
from importlib.resources import files


def read_recording(view):
    data = json.loads(files(__package__).joinpath("recordings.json").read_text())
    return data[view]
