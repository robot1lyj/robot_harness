"""UI-free extraction of operator_app.inspect_height, using installed typed ports."""

from pydantic import Field, model_validator

from .measure import Parameters as MeasureParameters
from .measure import Result as MeasureResult
from .measure import run as measure


class Parameters(MeasureParameters):
    lower_m: float = Field(ge=0, le=1, allow_inf_nan=False)
    upper_m: float = Field(ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def valid_interval(self):
        if self.lower_m > self.upper_m:
            raise ValueError("lower_m must not exceed upper_m")
        return self


class Result(MeasureResult):
    lower_m: float
    upper_m: float
    within_bounds: bool
    qualification: str = "authored recording and known formula; no physical validation"


async def run(context, args):
    result = await measure(context, args)
    context.stage("height_interval_checked", None)
    return Result(**result.model_dump(), lower_m=args.lower_m, upper_m=args.upper_m,
                  within_bounds=args.lower_m <= result.tip_m[2] <= args.upper_m)
