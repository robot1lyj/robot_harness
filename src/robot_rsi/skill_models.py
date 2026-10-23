"""Published pure skill contracts, independent of reference execution."""
from pydantic import Field
from .contracts import Contract, Identifier
from .capabilities import PickPlaceGoal
from .skill_result import SkillResult

class PickPlaceParameters(Contract):
    arm: Identifier = "left"
    object_id: Identifier = "red_block"
    target_region: Identifier = "blue_box"
    grasp_retries: int = Field(default=1, ge=0, le=1)

    def validate_context(self, context):
        context.bindings.manipulation.validate_goal(
            PickPlaceGoal(arm=self.arm, object_id=self.object_id, target_region=self.target_region)
        )

__all__ = ["PickPlaceParameters", "SkillResult"]
