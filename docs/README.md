# 文档目录

从你当前要做的事情选择入口，无需顺序读完全部文档。

| 目的 | 入口 |
|---|---|
| 了解框架并开始使用 | [项目 README](../README.md) |
| 理解模块职责、调用关系和源码位置 | [项目架构与代码导航](architecture.md) |
| 了解效率方法、已有结果与测量范围 | [性能方法与样例结果](performance.md) |
| 接入已有机器人项目、SDK 或算法 | [接入指南](integration.md)、[robot-onboarding](../skills/robot-onboarding/SKILL.md) |
| 查能力接口、数据语义和扩展工厂 | [接口参考](interfaces.md) |
| 理解运行中的工作区资料管理 | [workspace_context](../src/robot_rsi/resources/workspace-context/SKILL.md) |
| 理解任务检查点、恢复和归档 | [task_memory](../src/robot_rsi/resources/task-memory/SKILL.md) |
| 在同项目改进本地技能、独立比较与查看框架答复 | [项目工作流](project-workflow.md) |

## 参考案例

这些资料用于理解接口和调用方式，可按需要选读。

| 参考内容 | 入口 |
|---|---|
| 独立相机、纯计算及共享设备 owner 的适配包 | [synthetic_adapter](../examples/synthetic_adapter/README.md) |
| 从已有小项目提取能力、选择绑定并复用 | [project_adapter](../examples/project_adapter/README.md) |
| 任务如何选技能、加载资料、处理业务结果 | [面板巡检流程走查](task-walkthrough.md)，设计案例 |

安装版本的工具 schema 与对应源码定义精确接口；参考案例中的设备、任务和参数不构成使用方项目的默认配置。
