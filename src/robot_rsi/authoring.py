"""Small public SDK for code skills. Domain code owns observation and judgment."""
from uuid import uuid4

from .capabilities import SkillContext
from .contracts import Contract, CriterionResult, TaskEvidence
from .skill_result import SkillResult

__all__ = ["Contract", "SkillContext", "SkillResult", "artifact", "criterion", "result", "request_id"]


def request_id(prefix="skill"):
    return prefix + "_" + uuid4().hex


def artifact(context: SkillContext, data: bytes, *, mime="application/octet-stream", metadata=None):
    """Write through the operation's scoped artifact port and return its real reference."""
    if context.artifacts is None:
        raise ValueError("This skill requires the host artifact port")
    return TaskEvidence(kind="artifact", id=context.artifacts.write(data, mime, metadata or {}))


def criterion(identity, status, *, source, evidence, remaining=()):
    """Validate an explicit domain judgment; never promote failed/unknown to passed."""
    return CriterionResult(criterion_id=identity, status=status, source=source,
                           evidence_refs=evidence, remaining=list(remaining))


def result(*, completed: bool, verified: bool, qualification: str, criteria=(), **details):
    """Build the existing result shape; callers must explicitly choose both outcome flags."""
    return SkillResult({**details, "completed": completed, "verified": verified,
                        "qualification": qualification,
                        "criterion_results": [CriterionResult.model_validate(c).model_dump() for c in criteria]}).model_dump()
