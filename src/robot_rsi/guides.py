"""Read two installed public guides; the canonical bodies remain in docs/."""

import hashlib
import json
from importlib.metadata import distribution
from pathlib import Path
from typing import Literal

from pydantic import Field

from .contracts import Contract, HarnessError


class HarnessRead(Contract):
    guide: Literal["integration", "interfaces"] = "integration"
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=6000, ge=1, le=16000)


def read_guide(args, installation_digest):
    installed = distribution("robot-rsi-harness")
    resource = f"robot_rsi/resources/guides/{args.guide}.md"
    path = Path(installed.locate_file(resource))
    if path.is_symlink() or not path.is_file():
        raise HarnessError("guide_not_installed", "Install a wheel containing the public guides; no checkout fallback")
    text = path.read_text(encoding="utf-8")
    end = args.offset + args.limit
    return {"guide": args.guide, "canonical_source": f"docs/{args.guide}.md",
            "source": f"robot-rsi-harness:{resource}", "distribution_version": installed.version,
            "distribution_origin": json.loads(installed.read_text("direct_url.json") or "null"),
            "installation_digest": installation_digest, "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "unit": "characters", "offset": args.offset, "line_start": text[:args.offset].count("\n") + 1,
            "text": text[args.offset:end], "total": len(text), "next_offset": end if end < len(text) else None,
            "qualification": "installed framework documentation; not a robot capability or live readiness"}
