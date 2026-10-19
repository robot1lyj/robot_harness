from pydantic import Field

from robot_rsi.contracts import Contract, VersionRef
from robot_rsi.resources import FKRequest


class Parameters(Contract):
    position_m: float = Field(default=0.3, ge=0, le=1)


class Result(Contract):
    camera_source: str
    geometry_source: str
    model_ref: VersionRef
    tip_m: list[float]
    artifact_id: str


async def run(context, args):
    context.stage("recorded_calculation", None)
    observation = await context.bindings.camera.capture("sample", "preview", None)
    result = await context.bindings.kinematics.fk(
        FKRequest(
            model_ref=VersionRef.model_validate(context.resource_versions["model"]),
            arm="slider",
            joints={"slide": args.position_m},
            base_frame="lab",
            target_frame="tip",
        )
    )
    return Result(
        camera_source=observation.source,
        geometry_source=result.source,
        model_ref=result.model_ref,
        tip_m=[result.pose.matrix[i][3] for i in range(3)],
        artifact_id=observation.frames[0].artifact_id,
    )
