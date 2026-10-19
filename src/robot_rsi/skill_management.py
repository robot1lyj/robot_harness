"""Small host-only creation entry; candidates still require independent evaluation."""

import json
from pathlib import Path

from .registry import Manifest

CODE = """import asyncio
from pydantic import Field
from robot_rsi.contracts import Contract
from robot_rsi.skill_models import SkillResult

class Parameters(Contract):
    message: str = Field(default="fixture", max_length=200)
    delay_s: float = Field(default=0, ge=0, le=10)

async def run(context, args):
    context.stage("fixture_wait", None)
    await asyncio.sleep(args.delay_s)
    observation = await context.bindings.scene.observe()
    return {"message": args.message, "observation_id": observation.id,
            "qualification": "fixture_only"}
"""


def create(directory, name, version, kind):
    data = Manifest(
        name=name,
        version=version,
        kind=kind,
        category="fixture",
        description="演示技能 fixture example",
        qualification="fixture_only",
        requires={"scene.observe": 1} if kind == "code" else {},
        resources=["manipulator"] if kind == "code" else [],
        keywords=["演示", "example"],
        input_model="skill:Parameters" if kind == "code" else "robot_rsi.contracts:Empty",
        output_model="robot_rsi.skill_models:SkillResult",
        entry="skill:run" if kind == "code" else "workspace_read",
    )
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "skill.json").write_text(
        json.dumps(data.model_dump(), ensure_ascii=False, indent=2) + "\n"
    )
    (directory / "SKILL.md").write_text(
        """# 演示夹具

读取说明后复用已有工具；本模板不代表真实机器人能力。
"""
        + (
            "使用 task_run，skill 为本技能 ID，parameters 按 input_schema 提交。用 operation_get 查看结果。\n"
            if kind == "code"
            else "用 workspace_read 取得工作区资料；有新资料时用 workspace_note 按原键更新。准备说明不等于任务完成。\n"
        )
    )
    if kind == "code":
        (directory / "skill.py").write_text(CODE)
    return {
        "created": str(directory),
        "name": name,
        "version": version,
        "qualification": "fixture_only",
    }


def installed_packages():
    from importlib.resources import files

    return [
        path
        for path in files("robot_rsi").joinpath("resources").iterdir()
        if path.joinpath("skill.json").is_file()
    ]
