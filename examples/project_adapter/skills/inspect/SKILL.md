# Offline slider height inspection

Run `lab_inspect_height` through `task_run`. Supply `position_m`, `lower_m` and `upper_m`.
The host/task must bind camera.capture and kinematics.fk v1 with a named slider model.
Use an existing valid scene binding; if absent select and validate compatible installed
providers. Bounds must be ordered. The method reads one recorded preview, computes the
slider tip in metres and returns `within_bounds` plus the interval, source/model and image
artifact. A negative decision is a valid inspection result, not an execution failure.
This is an authored offline calculation; it performs no robot motion or live acquisition.
Complete the user's inspection goal from the actual terminal operation result and evidence,
then finish/archive the task. Do not claim physical calibration or deployment.
