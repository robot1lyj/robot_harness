# 机器人使用项目

用户叫法按目标和交付要求理解，普通任务复用现有方法。宿主如提供受控入口，使用其固定底座，
在本地扩展/技能目录改进并构建候选，由宿主保留式安装；不修改已装core或基础提示。
保护范围以实际入口/挂载事实为准，AGENTS与锁文件不构成权限边界。说明见 `robot-rsi guide project`。

当前目录保存本地能力实现、任务技能、配置、资产和新任务记录。框架是固定的
`robot-rsi-harness=={{CORE_VERSION}}` 依赖，不要把发布源码整树覆盖到这里。

先在当前目录启动已有的 coding Agent，提出实施需求。Agent 保留宿主原有编辑/终端工具，
通过 `uv run --no-sync robot-rsi guide agent` 读取安装引导；无需先启动只读子 Agent。
旧来源位于 `profiles/development.json` 的 project.root，工作目标是当前目录。

| 位置 | 用途 |
|---|---|
| extensions/、skills/ | 本地可安装实现与代码技能 |
| profiles/development.json | 来源分析与开发身份 |
| profiles/runtime.json | 实际资源装配与日常任务 |
| assets/ | 固定模型、标定及所需数据 |
| .rsi/state/ | 本项目新状态，按原角色/工作区隔离 |
| pyproject.toml、uv.lock、.venv/ | 本项目的依赖与运行环境 |

初始化没有配置机器人/相机，也不证明设备就绪。实现后用实际目录确认：
默认 CLI 读取当前项目 tool.robot-rsi.profile，无需每次加 --profile；显式配置和原环境选择优先。
从子目录调用仍按声明所在根解析，声明损坏会报错。开发身份请显式选择 development Profile。

```bash
uv run --no-sync robot-rsi call capabilities_list
uv run --no-sync robot-rsi call resources_list
uv run --no-sync robot-rsi call skills_list
```

接入应同时交付能力映射和可复用的日常代码技能，例如状态/相机观测及其来源、时效和缺项报告。
通过 task_run 执行实际目标，保存真实证据，完成成功后归档。接入说明技能不是日常执行技能。

框架/扩展更新时选择新 wheel，修改当前项目依赖与路径，运行 `uv lock` 及
`uv sync --locked --no-dev --no-editable`。在任务边界更新，保留旧 wheel/任务引用，
不删除本地 extensions、profiles、assets、记忆或历史。已有 AGENTS/README 不会被 init 覆盖；
存在冲突时请合并 `.rsi/harness/` 中生成的入口。

更多公共说明通过 `robot-rsi guide project`、`guide integration`、`guide interfaces` 读取。
