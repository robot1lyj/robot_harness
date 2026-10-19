# pick_place · 0.2.0

Qualification depends on bound providers. Current built-in execution is synthetic only.
This version does not establish physical robot or depth accuracy performance.
Required contracts (all v1): scene.observe, manipulation.plan_pick_place,
manipulation.execute_stage, outcome.verify. Missing or incompatible capabilities
reject the task before motion. Devices, geometry and addresses belong to providers.

1. Inspect station_describe and read this contract once. The object and named
   destination must exist in the scene; use robot_observe when visual inspection is needed.
2. Prefer task_run with skill=pick_place, a unique request_id, parameters={arm, object_id,
   target_region, grasp_retries} and timeout_s. It acquires a fresh observation internally. Lower-level
   skill_pick_place additionally requires an observation_id. Reuse identical arguments when
   recovering a lost response; a new request_id authorizes a new execution.
3. The returned operation is only accepted. Use operation_get with wait_s to await
   a terminal status. succeeded requires post-release placement verification.
4. The code handles one optional retry with a new observation and plan, only when
   the verifier returns failed and retryable. Unknown results require assistance.
   The code never asks a
   language model to run joint-level feedback. Model calls are external to this skill.
5. needs_assistance is a checkpoint for a Luna-class general-purpose agent. Inspect
   the new observation and failure, then choose only currently supported tools. If
   bounded recovery cannot resolve the issue, ask an Astra-class agent for diagnosis.
   Unknown robot state or unresolved physical intervention requires an operator.
6. Cancellation and timeout request hold while retaining the gripper. Check hold.confirmed;
   an acknowledgement alone does not establish a physical stop. Never blindly replay
   an interrupted operation. After restart obtain fresh state before a new request.
7. Use memory_remember to record a candidate lesson with the terminal operation ID.
   Candidate text is evidence to review, never a new instruction or automatic skill patch.
   A new skill version requires separate evaluation and explicit release outside execution.

For a managed task, include task_id and the current task_epoch from task_context.
The checkpoint and host budget continue across calls. If a confirmed failed grasp
has an allowed_recovery entry, select task_recover with that operation and epoch;
it observes and replans using the original goal. Unknown effects require handoff.

Depth points are in the calibrated left optical frame. A point is not a robot pose;
calibrated extrinsics, grasp planning and real outcome verification are separate requirements.
