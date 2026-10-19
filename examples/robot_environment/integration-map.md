# Source mapping for the offline environment

This is an authored integration example, not an unknown-project benchmark. The developer
followed the canonical robot_onboarding workflow to classify and extract the method below.
The runtime does not load this development analysis as task instructions.

| Source | Category | Actual contract / dependency | Treatment and reason | Artifact / limitation |
|---|---|---|---|---|
| legacy_lab.geometry.tip_mm | computation implementation | kinematics.fk v1, FKRequest/FKResult | reuse unchanged; thin mm→m and frame adapter | lab_adapter.Geometry; bounded slider formula, no IK/collision algorithm |
| legacy_lab.capture.read_recording | input implementation | camera.describe/capture v1 | reuse unchanged; map timestamp and preview bytes | RecordedCamera; authored recording, never live acquisition |
| legacy_lab/recordings.json | input data / asset source | CameraObservation + CaptureTime; RobotModel/CalibrationRecord v1 | package selected data, retain original time and explicit synthetic qualification | detail/survey recordings, named model/intrinsics refs; no measured physical calibration |
| operator_app.inspect_height | stable task method coupled to UI | camera.capture v1 + kinematics.fk v1 | extract sequence and interval criterion, replace UI calls with typed results and operation artifacts | lab_inspect_height 1.0.0; goal, bounds, decision and evidence returned via task_run |
| operator_app.align_and_move | unfinished application/motion | missing manipulation/verification implementation | leave missing; neither a task skill nor a callable robot API | no motion owner, no device permission, no integration claim |
| operator_app.__main__ / ui.show | application and UI | none required for the task | exclude from installed wheel; presentation is not execution | environment starts without importing or running the old application |
| profile + saved selection | environment configuration | Profile + BindingSelection; existing Store scope | use station/namespace/state_root and task-pinned scene/assets | two environments share wheels, isolate notes/tasks/bindings; no new state service |

The extracted method is available only when both camera.capture and kinematics.fk v1
are bound. Its interval result is not physical motion success. A missing capability or
inverted interval is an explicit failure. The public guide records exact installation commands.
