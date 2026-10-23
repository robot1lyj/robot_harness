# 编写代码技能

安装后用 `robot-rsi guide skill-authoring` 读取本页。

从 `robot-rsi skill create ./my-skill my_method --kind code` 开始，按生成的
skill.json 声明输入/输出、实际能力与资源；`SKILL.md` 只写必要用法。
方法保持 `async run(context, parameters)`，普通长流程写在代码中。

公开入口 `robot_rsi.authoring` 导出 Contract、SkillContext、SkillResult、
artifact、criterion、result、request_id；字段细节可用 Python `help()`。
参数模型沿用 Pydantic。context.bindings 仅含宿主绑定的能力，stage 保存阶段证据，
operation_id 与 resource_versions 由框架给出，artifacts 读写本操作作用域的产物。
不要自行构造操作/任务状态或安装包。

```python
from robot_rsi.authoring import artifact, criterion, result

async def run(context, args):
    observation = await context.bindings.camera.capture(camera=args.camera, require="preview")
    frame = observation.frames[0]
    pixels = context.artifacts.read(frame.artifact_id)
    status = judge(pixels)  # 方法自己的观测判据，返回 passed/failed/unknown
    reference = artifact(context, pixels, mime="image/png", metadata={"source": observation.id})
    return result(completed=True, verified=status == "passed",
        qualification=observation.qualification,
        criteria=[criterion("normal", status, source="本方法的独立像素判据",
            evidence=[reference], remaining=[] if status == "passed" else ["处理异常或重采不可读观测"])])
```

示例须自行实现 judge 与参数模型、声明 camera.capture:1；无运动则 resources=[]。
result 的 completed/verified 必须显式给出；辅助函数不推断成功，不把 failed/unknown 改成 passed。
任务的完整目标由 delivery 的必要 criteria 决定，操作成功本身不足以宣告完成。

用原 `skill register ./my-skill`、`skill enable my_method 1.0.0` 管理目录包，
或 wheel entry point 返回其资源目录后 `skill discover`。通过原 `task_begin`、
`task_run`、`operation_get` 调用；新任务固定当时的版本，换输入再运行验证复用。
完整可调用例子见[巡检方法](../examples/multipoint_inspection/README.md)，
其运动反馈与图像判断分别记录，未知动作不重放，只允许有界只读重采。

方法交付报告直接从原记录导出，无需抄写内部 JSON：

```sh
robot-rsi --profile profile.json evidence --method my_method \
  --task-id first --task-id reuse --output method-report.json
```

程序保留实际 registration、operation、输入、固定版本、task/call/installation 引用，
不运行插件、不复测、不生成领域成功结果。两次不同输入和原目标的全部必要判据仍须真实满足；
coding handoff 的 method_evidence 可引用这个文件。旧的单任务 `evidence --task-id ...` 保持兼容。
