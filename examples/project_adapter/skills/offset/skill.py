from pydantic import Field

from robot_rsi.contracts import Contract, VersionRef
from robot_rsi.resources import FKRequest


class Parameters(Contract):
    position_m: float = Field(default=0.3, ge=0, le=1)
    offset_mm: float = Field(default=0, ge=-100, le=100)


class Result(Contract):
    tip_m: list[float]
    source: str


async def run(context, args):
    position = args.position_m
    result = await context.bindings.kinematics.fk(FKRequest(
        model_ref=VersionRef.model_validate(context.resource_versions["model"]),
        arm="slider", joints={"slide": position}, base_frame="lab", target_frame="tip"))
    return Result(tip_m=[result.pose.matrix[i][3] for i in range(3)], source=result.source)
