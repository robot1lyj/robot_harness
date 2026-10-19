---
name: workspace-context
description: 进入机器人工作区、查找相关资料或更新工作区备注时，恢复下一步所需信息。
---

# 工作区资料管理

运行技能 ID：`workspace_context`。目标是取得下一步所需信息，然后继续任务。

- 初次进入工作区调用 `workspace_read`，获得环境、必要约束和资料入口。当前上下文已有这些信息时直接使用；有相关变更再读取。
- 有资料缺口时，用 `workspace_read(query="主题")` 找备注卡片，再用 `workspace_read(key="备注键")` 读取需要的正文。已知键则直接读取。
- 按当前任务用 `skills_list(query="目标")` 找适用技能，选中后调用 `skill_describe(name="技能 ID")`。照其 entrypoint 继续；已有足够信息就执行下一步。
- 需要场景选择时先用 `binding_read` 读已存引用和相关变化；适用即复用。缺项才以所需合同调用 `binding_candidates`，按目标选 provider/资源/资产并写理由；`binding_validate` 静态核对与明确的只读/计算试用，`binding_save` 引用其宿主证据。新任务用 binding_ref，首次执行前也可保存时 attach_to_task；已固定的任务不换绑定。变化仅重验受影响端口，不能自报通过。
- 用户提供新资料或原资料发生变化时，用 `workspace_note` 写入 `key`、`title`、`text`、`source`。同一事实沿用原键更新，失效备注用 `archive=true` 移出检索。

备注记录来源和适用条件；它不修改宿主配置。涉及当前物体位置或设备状态时，通过观测工具获取。常规资料更新直接完成；只在来源冲突影响当前决策时核对相关部分。

需要向用户说明时，简短给出工作区要点、影响当前任务的缺口和下一步。完整历史与无关技能留在库中。
