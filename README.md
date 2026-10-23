# Robot RSI Harness

让已有 coding Agent 在自己的机器人项目中实现、发现和使用能力与任务技能。
框架负责统一合同、执行、版本、任务和记忆；使用项目负责本地实现、配置、资产和日常方法；
coding 宿主保留原有文件、终端和授权。框架不依赖 ROS 2，也不接管设备控制器。

完成授权开发功能/修复时，验证、更新原事实与短检查点并本地提交，报告 SHA 和未决项；普通运行不执行 Git。
具体提交范围和项目记忆接续见[项目工作流](docs/project-workflow.md#开发完成提交与接续)。

用户说“能力、技能、工具或功能”时，先按目标、输入输出和交付要求选择已有方法；不要求用户先分类。
明确要求接口/技能 manifest 时仍照办。底座版本与可写范围由[受控宿主入口](docs/project-workflow.md#固定底座与受控入口)落实，普通桌面全权限会话不会自动受保护。

## 从当前使用目录开始

准备固定 core wheel，在你选定的机器人使用目录运行：

```bash
uv tool run --from /path/robot_rsi_harness-0.4.0-py3-none-any.whl robot-rsi init . \
  --core-wheel /path/robot_rsi_harness-0.4.0-py3-none-any.whl --source /path/legacy-project
uv run --no-sync robot-rsi guide agent
```

已有安装也可直接用 `robot-rsi init`。初始化生成独立 pyproject、既有 Profile、中文 README
和短 AGENTS 桥接，再由 uv 创建锁与安装。旧来源不动，框架只是固定依赖；不会安装硬件 SDK、
连接设备或虚构机器人能力。`--no-sync` 只生成文件，之后显式运行 `uv lock` 和
`uv sync --locked --no-dev --no-editable`。

若当前目录仍是框架工程，init 会报告角色冲突；它不会偷偷另建隐藏使用目录。
已有项目声明/配置保持不变，已有 README/AGENTS 保留，需合并的入口放在 `.rsi/harness/`。
重复初始化不覆盖项目文件。详细边界见[接入指南](docs/integration.md)。

## 让现有 Agent 直接实施

在当前使用目录启动已有 coding Agent，只需表达普通需求，例如：

> 在当前目录接入这个项目的基础能力，完成一次机器人与相机观测任务，旧项目不动。

产品生成的 AGENTS 引导它读取当前安装的 `guide agent`，并查看 development/runtime Profile。
它直接使用宿主已经提供的编辑、终端工具，不必先启动另一个只读子 Agent。
`robot-rsi agent run --project SOURCE` 仍适合只读方案，不是具有开发工具的 coding 宿主。

默认接入适用的环境、控制/状态、FK/IK、相机与模型/标定资产；录制、遥操作、HIL、策略推理
由用户选择，后续按任务增量引入。明确的单独数据任务仍保持其目标。

## 看见实际能力与技能

```bash
uv run --no-sync robot-rsi --profile profiles/runtime.json call capabilities_list
uv run --no-sync robot-rsi --profile profiles/runtime.json call resources_list
uv run --no-sync robot-rsi --profile profiles/runtime.json call skills_list
uv run --no-sync robot-rsi --profile profiles/runtime.json call skill_describe '{"name":"YOUR_SKILL"}'
```

初始化时能力目录为空是正常事实。接入实施必须产生固定安装的能力实现和至少一个日常代码技能，
例如状态与选定相机观测、来源/时效/缺项报告；再通过 `task_run` 实际执行、存证、完成和归档。
接入说明技能和一次验收脚本不代替日常任务。相机采样不是数据集录制，计算 FK 不是实测状态，
命令被接受也不等于目标完成。

CLI、MCP、Python 宿主和独立 HTTP API 复用同一 Runtime。需保持连接和等待操作的流程使用同一
Runtime/MCP 生命周期；多个一次性 CLI 进程不会保持同一个设备连接。具体合同由实际 schema 拥有。

## 项目与框架分别维护

使用项目保存 extensions、skills、profiles、assets、uv.lock/.venv 和 `.rsi/state`。
框架开发仓库与净产品发布共同维护公共实现；升级使用项目时更新固定 wheel/依赖与锁，
在任务边界安装，保留本地扩展、配置、记忆、旧 wheel 与任务引用，不做产品整树覆盖。

| 目的 | 入口 |
|---|---|
| 最短启动、接入与实际交付 | [接入指南](docs/integration.md) |
| 项目内实施、运行、更新和反馈 | [项目工作流](docs/project-workflow.md) |
| 职责、四层与源码位置 | [架构导航](docs/architecture.md) |
| 端口、能力、数据字段和工具 | [接口参考](docs/interfaces.md) |
| 测量方法与已验结果 | [性能说明](docs/performance.md) |
| 按需查阅示例 | [文档目录](docs/README.md) |

当前历史证据覆盖所列软件合同、原生方案和离线任务；它们不代表任意项目自动接入或真实设备已接管。
实际 coding 项目交接与现场状态分别取证。token 只观测；常规执行优先稳定代码和有效绑定，
模型处理任务选择与缺口，不增加每步总结或固定重复复核。
