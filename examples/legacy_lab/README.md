# Offline legacy laboratory fixture

This small project deliberately has no Harness manifest or factory. `capture.read_recording`
reads an authored synthetic recording; `geometry.tip_mm` calculates one slider point in mm.
`operator_app.inspect_height` is a manual UI-coupled interval inspection; the
[independent environment](../robot_environment/README.md) extracts its method as a code skill.
`operator_app.align_and_move` is unfinished and requires an operator/device implementation.
The recording is a fixture, not a physical camera sample. Do not run the operator entry.

Existing tests can run before adaptation with
`PYTHONPATH=examples python -m pytest examples/legacy_lab/tests -q`.
