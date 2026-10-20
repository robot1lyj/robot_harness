# 接入已有机器人项目

<a id="source-usage-target-and-an-actionable-plan"></a>
## 当前目录是使用项目

框架源码、净产品发行与机器人使用项目各有职责。使用目录的 pyproject/uv.lock/.venv 属于应用，
本地 extensions、skills、profiles、assets 和新状态保存在这里；框架是固定安装的依赖。
不要把框架根 pyproject 当成使用项目，也不要用发布整树覆盖本地实现。

```bash
robot-rsi init . --core-wheel /path/robot_rsi_harness-0.3.0-py3-none-any.whl --source /path/legacy
uv run --no-sync robot-rsi guide agent
```

init 在选定目录生成骨架并运行 `uv lock`、`uv sync --locked --no-dev --no-editable`。
没有安装命令时可由 uv tool run 从指定 wheel 启动。`--source` 可暂缺，`--no-sync` 可仅生成。
初始化不运行来源代码、安装硬件 SDK 或连接设备；初始 Profile 没有 robot/adapter/camera/depth。
已有项目声明、Profile 或同名不同 wheel 会保留并给出冲突；框架目录会明确拒绝，不重定向。
已有 README/AGENTS 不覆盖，需合并的模板放 `.rsi/harness/`。重复初始化不改原配置和依赖。

未显式选配置时，CLI 从当前目录向父级读取最近的 tool.robot-rsi.profile 声明，Profile 文件及其
相对文件系统根按声明所在项目根解析。显式 --profile 和原 ROBOT_RSI_PROFILE 环境选择优先；
声明缺文件/无效会明确报错，不退回 mock。显式 --project 的只读来源入口不加载这个隐式默认，
不会凭空产生 profile 冲突；项目外没有声明时保留原行为。

## coding 宿主与只读方案

coding Agent 在当前项目沿自己的文件/终端工具实施已授权目标。生成的短 AGENTS 指向当前安装的
`robot-rsi guide agent`：英文 base、规范 robot_onboarding 与中文项目指南同源读取。
不复制开发历史或全局配置，不要求先开只能读源码的模型，也不强制 MCP；CLI/Python/MCP 均可调用
原 Runtime。模板存在不代表模型已读，真实交接要核对实际工具输出与后续产物。

只需要方案时，原入口继续使用：

```bash
robot-rsi agent run --project /path/legacy --usage-root /path/robot-use "这是我的机器人项目，给我一个能开始实施的接入方案。"
robot-rsi mcp --project /path/legacy --usage-root /path/robot-use
```

source-only 客户端没有开发文件/终端工具；它只分析来源、给可实施方案和具体交接。
project.usage_root 只是宿主选定目标，不自动建目录、安装或授予写权限；project_files 仍限来源根。
未选择时显示 not_selected，选择后未检查显示 selected_not_inspected。--project 与 --profile 不同时使用。
新普通请求不带 --task-id，仍能按同一项目读取必要笔记；--task-id 恢复原目标，不替换为新需求。

<a id="foundation-first-optional-workflows-by-choice"></a>
## 基础优先与一次范围选择

“观测能力”和“观测技能”等口语标签先按目标/输入输出理解，复用实际已有方法；不要因名词创建无用包装。
明确要求某接口、manifest或可复用交付时仍遵循，可同时交付能力、技能与资产。只有改变成果或范围的歧义才澄清。
在[受控宿主](project-workflow.md#固定底座与受控入口)中修改项目扩展、构建候选，安装由宿主保留式更新；
不能改已安装core/基础提示或项目锁文件来重新认可底座。隔离与未受保护会话范围由实际宿主事实说明。

首次接入按机器人形态建立适用的独立环境、控制/状态、FK/IK、相机及模型/TCP/帧/标定资产。
缺失或不适用项保留在结果中，不能因数据检查易跑而替代基础接入。状态/图像采样是基础观测，
episode、数据集、导出/质检是上层流程。已选范围直接沿用；模糊接入推荐基础，并集中问一次是否加入
已发现的录制、遥操作、HIL 或策略。未答复不当作选全部，已授权基础工作或方案可继续。
明确的单独数据任务不强制整机接入。“全部”可按依赖安排首批，不需要逐项重复审批。

笔记只留范围依据、已选项、已装基础/绑定、缺口及延期入口/用途；以后需要扩展再按相关来源提取。
不预装全部业务依赖，不把开发能力清单全量灌入日常上下文。

## 接入要交付什么

方案保留四项决定：使用环境和固定依赖；所选来源的去向/维护位置；真实合同字段、单位/帧/时间/状态
及差距归属；首个可执行目标、输入、产物、调用和判据。实施还要给出以下两张简表的实际结果：

| 能力视图 | 来源与类别 | 已装实现/合同/资源/资产 | 状态与缺口 |
|---|---|---|---|
| 每项当前所需基础能力 | 具体模块/符号/版本 | 从 capabilities_list/resources_list 与固定 Profile 取得 | 已安装、待实现、现场待验分别说明 |

| 日常技能视图 | 目标/依赖 | 版本与实际调用 | 证据 |
|---|---|---|---|
| 可复用代码方法 | 如状态和选定相机观测、来源/时效/缺项报告 | skills_list/skill_describe，task_run 的真实 skill/parameters | operation、观测/产物、任务结果 |

表是对真实目录的简要解释，不是第二能力数据库或静态全成功看板。说明型 robot_onboarding 与一次
基础验收脚本均不代替日常代码技能。固定安装后发现并执行该技能，存证、完成成功后归档，
再验证下一任务/重启可复用。时效来自实际采集时钟与接收事实，未知时钟保留未知，不编造设备反馈。

## 逐能力复用

| 来源对象 | 处理方式 |
|---|---|
| 独立实现包 | 固定 wheel/版本安装，保留来源和依赖 |
| 耦合模块 | 只提取需要的选定版本模块到本地包，注明原 SHA 与新维护位置 |
| 既有服务 | 按真实合同复用唯一 owner；发现/安装不自动启动或重启 |
| 模型、标定、数据 | 版本引用或必要复制，实例配置留本地 |
| 任务方法 | 保留目标、适用条件和结果判据，整理为依赖能力合同的代码技能 |

唯一设备写入 owner 不等于旧应用 API 永远必经；计算可直接模块复用，必要服务可继续使用。
不复制整项目/.venv，不用旧源码 PYTHONPATH/editable 作为长期运行依赖，也不自动回写旧来源。
缺传输、状态、停止/未知效果语义要交具体实现或部署补齐，薄包装不能制造这些事实。

## 原合同与生命周期

适配包用 `robot_rsi.adapters` entry point 提供工厂；技能包用 `robot_rsi.skills` 发现，沿原 Registry
显式 discover/enable。Profile 管装配与作用域，Store 管资料/证据，Runtime 是唯一调用分发。
资源元数据读取不连接设备；连接由显式宿主调用，多个资源可共享同一 owner。
同一 Runtime 已由宿主连接的资源已满足前提，Agent 复用该状态；仅未连接时调用已有 resource_connect。
普通运行优先选择已安装日常技能，不重复接入。原生目录的 callable 表示当前客户端暴露，available 仍表示绑定，均不证明现场可用。
需要连接、采样、task_run 和等待的流程在同一个 Runtime/Python 宿主或 MCP 生命周期内执行。
一次性 CLI 适合目录/配置查询，跨进程调用不能假定前一次连接仍存在。

固定任务入口示意（标识符和参数须来自你实际安装的技能）：

```python
from robot_rsi.contracts import Profile
from robot_rsi.runtime import Runtime

async def execute(profile_file, task_id, skill, parameters):
    from pathlib import Path
    profile = Profile.model_validate_json(Path(profile_file).read_text())
    async with Runtime(profile) as runtime:
        # 在同一宿主生命周期，按已授权配置显式连接所需资源。
        started = await runtime.invoke("task_begin", {"task_id": task_id, "goal": "当前完整目标"})
        accepted = await runtime.invoke("task_run", {"task_id": task_id,
            "task_epoch": started["result"]["execution"]["epoch"],
            "request_id": task_id + "_operation", "skill": skill, "parameters": parameters})
        return accepted  # 宿主还需等待实际终态、核验目标并 finish/archive。
```

该示意不提供机器人实现，不自动连接或声称完成。具体 schema 读 tool_describe 和
[接口参考](interfaces.md)。[独立 synthetic 示例](../examples/synthetic_adapter/README.md)
演示状态、回执/反馈、相机、已知 FK/IK；它不是一般求解器或现场证据。

## 已安装指南与范围事实

`robot-rsi guide base|onboarding|project|integration|interfaces` 按需读取当前安装内容；
`guide agent` 合并机器引导与项目说明，供 coding 宿主读取。wheel 包含 integration、interfaces、
project-workflow，规范正文由公共 docs 构建引用，不维护另一份合同。
原 `harness_read` 仍只分页读 integration/interfaces，带字符 offset/next_offset、全文 SHA、来源、
分发包/安装身份；缺已装资源时明确失败，不回退读取开发树。tool_describe 给真实 schema，
有文档不代表该机器人已实现对应能力。

环境摘要由原 Profile/Runtime/Registry/Store 自动提供，区分当前安装、任务固定安装及配置态资格，
不增加必选初始化技能或巡检。运行只加载相关卡片/正文/有效事实。
旧工作区选择新版 workflow 用 `skill discover`、`skill enable robot_onboarding 1.7.0`，
旧任务固定正文和安装引用保持。

<a id="first-inspection-from-an-installed-environment"></a>
## 安装、更新与证据边界

Python 3.12/3.13 与 uv 管理项目环境。所有运行扩展声明进当前 pyproject/uv.lock，用固定 wheel
而非锁外 pip/editable 作为部署依赖；启动用环境可执行文件或 `uv run --no-sync`，不运行时装包。
更新时保留本地文件、配置、状态及旧 wheel，在任务边界选择新版本、uv lock 并 locked sync。
框架发布不覆盖使用分支；旧镜像 worktree 的转换/恢复由宿主明确执行。

source-only、新 coding 会话、软件合同调用、日常任务复用和现场分别取证；新 coding 交接须实际
读到项目入口和安装说明、在当前目录编写安装新来源实现、从真实目录选择并 task_run。
不能把好方案或预装的别的 demo 当作完成。

更多例子：[锁定使用环境](../examples/robot_environment/README.md)、
[已有项目提取](../examples/project_adapter/README.md)、[项目内改进与反馈](project-workflow.md)。
MCP 可选：用项目内 `robot-rsi --profile profiles/runtime.json mcp`；独立 HTTP serve 监听回环，
Bearer 凭据由 ROBOT_RSI_TOKEN 提供，属于另一协议入口，不要求同时启动。
