"""One installed base prompt and its observable identity, not a prompt service."""
import hashlib
from functools import lru_cache
from importlib.resources import files


@lru_cache(maxsize=1)
def base_prompt():
    text = files("robot_rsi").joinpath("resources/prompts/base.md").read_text().strip()
    return {"text": text, "identity": {"sha256": hashlib.sha256(text.encode()).hexdigest(),
            "bytes": len(text.encode()), "characters": len(text),
            "estimated_tokens": (len(text.encode()) + 3) // 4,
            "measurement": "UTF-8 bytes / 4 heuristic for English, not client token usage"}}
