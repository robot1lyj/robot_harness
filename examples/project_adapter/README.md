# Existing project → installed candidates → saved task binding

The source is [legacy_lab](../legacy_lab/README.md), an authored offline project fixture without
a Harness manifest. Its recording reader and mm slider function are included unchanged in the
adapter wheel. The operator application is unfinished and is neither imported nor packaged.
[Capability inventory](capabilities.json) distinguishes source, dependencies, tests and gaps.
This self-test is not a blind integration of an unknown project or physical camera evidence.

```bash
uv sync --frozen --group dev
uv pip install --no-deps -e examples/project_adapter
uv run --no-sync robot-rsi --profile examples/project_adapter/profile.json config-check
uv run --no-sync robot-rsi --profile examples/project_adapter/profile.json call binding_candidates '{"requirements":{"camera.capture":1}}'
uv run --no-sync python examples/project_adapter/demo.py --state-root /tmp/lab-binding --task first --selection examples/project_adapter/example-selection.json
uv run --no-sync python examples/project_adapter/demo.py --state-root /tmp/lab-binding --task second
```

The first demo replays the **explicit supplied choice**; it does not claim to make an Agent decision.
The second command starts a new host process and task with the saved binding, without discovery,
selection or repeated trials. Both runs finish and archive the explicit read/calculation goal; output
separates operation status from task_result. Use a fresh state directory for a new first run: saved versions are immutable.
For wheels use `uv build --wheel examples/project_adapter`; its build includes only four specified
legacy source/data files. Importing the old application or starting a service is unnecessary.

There are two camera candidates (detail 12×8 / survey 4×3) and two FK candidates (250 / 200 mm
fixture height). The public profile leaves both default ports empty (`default_ports=false`);
the Agent can discover candidates, then the sample choice binds detail and its named assets for its task.
Hosts that explicitly set default_ports=true retain survey as their unbound default.
No candidate provides motion, IK, live freshness or result recognition. Recordings keep their original
capture timestamp; asking for a live max_age fails. Calibration metadata and known formula agreement
only validate this offline fixture.

Agent integration uses `binding_candidates`, `binding_validate`, `binding_save`, `binding_read` and
`task_begin(binding_ref, binding_conditions)` as described in [interfaces](../../docs/interfaces.md).
Selection is data, not executable code. Saving requires host-produced matching evidence. For a task
already established, attach_to_task is allowed only before any operation and before a binding is fixed.
Read summary/changes on reuse; submit only changed roles to validation with previous=the old reference.
Older task references and unresolved operations are retained; the binding does not authorize replay.
