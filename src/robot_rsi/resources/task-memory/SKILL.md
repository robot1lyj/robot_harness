---
name: task-memory
description: 推进、恢复或结束任务时，保存短检查点、读取操作证据并归档。
---

# 任务记忆管理

运行技能 ID：`task_memory`。已有信息直接复用，取得下一步材料后继续任务。

- 新任务用 `task_begin(task_id, goal, constraints)` 建立检查点与预算；已有任务直接恢复。宿主可能已绑定 task_id，其他接口可在外层关联任务。
- 已有场景绑定时 task_begin 带 binding_ref 和 binding_conditions；引用固定在 resource_versions，恢复直接沿用。首次执行前的选择流程归 workspace_context；有变化用 binding_read 看相关项，不每轮重新挑选或试用，也不改在途任务。
- `task_context(task_id)` 按需取进度、预算、最多 3 张技能卡片与 3 条经验；selected_skill 取一项合同，query 按当前阶段检索。超预算时分步读取，不截断必要事实。
- 操作接受不代表成功，按 operation_get 读取证据。动作使用当前 task_epoch；已确认失败按 allowed_recovery 调 task_recover。下一步骤以 after_operation_id 引用上一成功操作，不重复已完成步骤。未知效果不重放，取消用 task_cancel。
- Harness 按有效变化维护短记忆，重复合并、旧过程归档；无变化不写文件、不调用模型总结。仅补新事实或交接时 task_checkpoint_save，不能刷新预算。token 只观测。
- task_checkpoint_get 恢复当前记录与原操作状态，archive_id 按需取回旧视图。evidence_refs 以 kind/id 引用资料或证据；经验用 memory_recall。
- 整个目标达成后，在同一回合用 task_finish 写当前 task_epoch、outcome=succeeded、简短整项目标结论和 operation_ids/evidence_refs。成功步骤不等于任务成功；活动、未知或不适用证据不能支撑成功。
- 结束记录用 task_archive，include_archived=true 追溯。归档不改变任务结果；取消、失败、未知与未完成仍保留原语义。无需每步总结、重复复核或老师审批。

任务记录与工作区备注分开；保留版本与未决信息。更新文件不清空客户端对话。
