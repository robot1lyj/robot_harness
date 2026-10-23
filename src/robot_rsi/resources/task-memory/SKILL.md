---
name: task-memory
description: Save changed task facts, recover pinned execution and finish the whole goal.
---

# Task memory

Use task_begin for a new task, otherwise recover task_context/task_checkpoint_get. Keep the original goal, constraints, epoch and fixed skill/resource/installation versions. Use existing scene bindings; do not select or trial again without a change. Read selected skills and referenced details only as needed.

When a whole-goal contract helps, add delivery at task_begin or task_checkpoint_save: original_goal, kind (one_off/reusable_method), necessary criteria, actual versioned task_run entry/input example, and remaining work. Preserve original intent and necessary criteria; phase progress goes in last_confirmed. Update only changed facts. The existing owner maintains the short view and cold history; no per-step/model summaries. Token usage is observation, not an execution quota.

Accepted operations are not completed tasks. Read operation_get. Continue with the current task_epoch and after_operation_id; recover only through allowed_recovery. Never replay unknown effects. Cancel through task_cancel. Domain failed/unknown observations are separate from unknown physical effects.

Finish in the same turn once evidence supports the whole goal: task_finish with current epoch, outcome, summary and operation_ids/evidence_refs. Declared required criteria need passed sourced criterion_results from domain code; original operation results cannot be overridden. A reusable method also needs method_evidence (a fixed JSON artifact containing existing Registry registration and two different-input operation reports). The core checks consistency without executing or rejudging observations. Ordinary tasks need no new delivery contract. A report may finish with anomalies when normality is not its goal.

Archive after successful task_finish. Archive preserves failed/cancelled/unknown/incomplete outcomes. include_archived/archive_id recover old evidence. Workspace notes are separate; save concise facts/sources by key. No repeated review or approval.
