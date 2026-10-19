"""Small local catalog: cards for discovery, instructions only on request."""

from __future__ import annotations

import re
from importlib.resources import files

# Compatibility metadata for management fixtures; manifests own all definitions.
SKILLS = [
    __import__('json').loads(files('robot_rsi').joinpath('resources', folder, 'skill.json').read_text())
    for folder in ('pick_place', 'workspace-context', 'task-memory')
]



def relevance(query: str, text: str, keywords=()) -> int:
    """Literal terms plus authored aliases; no semantic or model ranking claim."""
    query, text = query.casefold().strip(), text.casefold()
    if not query:
        return 1
    terms = re.findall(r"[\w]+", query)
    return sum(term in text for term in terms) + sum(
        keyword.casefold() in query for keyword in keywords
    )


def public_descriptor(skill: dict) -> dict:
    return {key: value for key, value in skill.items() if key not in ('keywords', 'instructions_path')}
