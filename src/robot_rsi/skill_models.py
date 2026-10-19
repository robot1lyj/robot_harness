"""Published skill contracts, separate from the stable submission envelope."""

from pydantic import Field, RootModel

from .contracts import Contract, Identifier


class PickPlaceParameters(Contract):
    arm: Identifier = "left"
    object_id: Identifier = "red_block"
    target_region: Identifier = "blue_box"
    grasp_retries: int = Field(default=1, ge=0, le=1)

    def validate_context(self, context):
        from .capabilities import PickPlaceGoal

        context.bindings.manipulation.validate_goal(
            PickPlaceGoal(arm=self.arm, object_id=self.object_id, target_region=self.target_region)
        )


class SkillResult(RootModel[dict]):
    pass
