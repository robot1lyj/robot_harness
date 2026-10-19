# Robot RSI Harness

**让通用 Agent 使用已有的机器人软件、设备与算法。**

Robot RSI Harness 提供机器人任务的运行框架：统一能力接口、技能调用、任务状态、上下文与记忆，以及版本化的技能管理。使用方把自己的机器人和算法接入这些接口，再由 Agent 根据任务选择和调用。

本仓库发布框架源码、接入规范和基础技能。具体机器人的驱动、现场配置、模型权重和业务技能由使用方项目提供。

[项目架构与代码导航](docs/architecture.md) · [接入指南](docs/integration.md) · [接口参考](docs/interfaces.md) · [文档目录](docs/README.md)

## 框架负责什么

| 职责 | 提供的能力 |
|---|---|
| 接入 | 将已有 SDK、控制程序、相机和计算包组织成统一能力，由宿主配置装配 |
| 执行 | 调用技能，管理操作状态、等待、取消、结果和证据 |
| 上下文与记忆 | 按任务加载技能说明、当前状态和适用经验，保存检查点并支持归档 |
| 版本与改进 | 注册和启用技能版本，固定任务所用版本，支持候选评估、更新和回退 |

Agent 负责理解目标和选择方法；设备控制器、算法包负责具体执行。Harness 连接两者，并保存可追溯的任务过程。

## 从自己的机器人项目开始

### 1. 安装框架

需要 Python 3.12 或 3.13，以及 uv。在本仓库根目录执行：

```bash
uv sync --frozen --no-dev
uv run --no-sync robot-rsi --version
```

### 2. 接入已有能力

向你的 coding Agent 提供机器人项目或 SDK 的位置，并让它使用本仓库的 [robot-onboarding](skills/robot-onboarding/SKILL.md)：

> 阅读 skills/robot-onboarding/SKILL.md 和接入指南，梳理我的机器人项目中已有的能力，按当前任务需要生成适配包、宿主配置和必要的验证说明。

接入可以复用原项目中的控制、采集和计算模块。机械臂、相机、运动学和深度算法按能力组合；地址、设备名称和部署位置留在配置中。具体接口与安装步骤见[接入指南](docs/integration.md)。

### 3. 检查配置并连接 Agent

将下面的 `path/to/profile.json` 替换为接入时生成的宿主配置路径。在装有框架及适配包的环境运行：

```bash
uv run --no-sync robot-rsi --profile path/to/profile.json config-check
uv run --no-sync robot-rsi --profile path/to/profile.json mcp
```

`config-check` 检查配置与接口装配信息。`mcp` 提供 Agent 工具入口，由支持 MCP 的客户端连接；框架也提供 CLI 和 HTTP 接口。

### 4. 在项目中执行和扩充任务

Agent 读取当前工作区、查找适用技能、按需查看调用说明，再执行任务和检查结果。常用入口包括 `workspace_read`、`skills_list`、`skill_describe`、`task_begin`、`task_run` 和 `task_context`。

已有能力直接复用；缺少能力时，在自己的项目中补适配实现或技能包。安装更新后在任务边界选用新版本，运行配置和任务记忆独立保留。

## 仓库怎么读

| 路径 | 用途 |
|---|---|
| `src/robot_rsi/` | 框架的 Python 实现：接口、运行器、上下文、记忆、技能管理和适配入口 |
| `skills/robot-onboarding/` | 供接入方 Agent 使用的基础接入技能 |
| `docs/` | 公共架构、接入方法和接口说明 |
| `examples/` | 接口和扩展包的参考示例 |
| `configs/` | 配置样例 |
| `tests/` | 框架与接口测试 |

`robot_rsi` 是 Python 导入包名，安装发行名是 `robot-rsi-harness`，命令行入口是 `robot-rsi`。包内各模块的职责见[代码导航](docs/architecture.md#代码导航)。

当前框架已通过软件与离线案例验证；真实设备支持及效果由适配实现和现场验证确定。
