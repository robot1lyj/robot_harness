"""Generic skill result contract, independent of any reference task."""
from pydantic import RootModel


class SkillResult(RootModel[dict]):
    pass
