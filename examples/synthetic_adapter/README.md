# Independent synthetic adapter

An installable `robot_rsi.adapters` factory with no hardware SDK. It provides one three-joint named manipulator (`bridge_3`), a `vacuum` tool measured in Pa, an optional independent camera, and a known Cartesian/yaw FK/IK fixture. It provides no scene localization, pick/place planner or outcome verifier. These are software contract examples, not a supported physical device or general IK solver.

After installing the core project:

```bash
uv pip install --no-deps -e examples/synthetic_adapter
uv run --no-sync robot-rsi --profile examples/synthetic_adapter/profiles/shared.json config-check
uv run --no-sync python examples/synthetic_adapter/demo.py --state-root /tmp/synthetic-demo
uv run --no-sync python examples/synthetic_adapter/demo.py --profile examples/synthetic_adapter/profiles/camera_only.json --state-root /tmp/camera-demo
uv run --no-sync python examples/synthetic_adapter/demo.py --profile examples/synthetic_adapter/profiles/compute_only.json --state-root /tmp/compute-demo
```

Use a new state directory for each independent exercise. Within one Runtime, `resource_connect` explicitly opens each owner; shared robot/camera resources connect and close that owner once. `resources_list` and `capabilities_list` read metadata without connecting. `robot_state` returns sampled joint/tool values, clock identity and model reference; `model_get` returns the corresponding definition. `robot_command` returns an operation handle, whose submitted receipt is distinct from measured completion. Raw commands do not declare whole-goal success.

`profiles/robot_only.json` omits the camera. The camera-only and compute-only profiles set the legacy built-in robot selector to null and supply no execution owner. FK/IK perform no device access. Model/calibration `v1` and `v2` are fixed synthetic records; profiles select versions, tasks keep their initial references.

An optional code skill exercises the same calculation from the existing task runner:

```bash
uv run --no-sync robot-rsi --profile examples/synthetic_adapter/profiles/compute_only.json skill register examples/synthetic_adapter/skills/check_fk
uv run --no-sync robot-rsi --profile examples/synthetic_adapter/profiles/compute_only.json skill enable fixture_fk 1.0.0
uv run --no-sync robot-rsi --profile examples/synthetic_adapter/profiles/compute_only.json --task-id fk-example call task_begin '{"goal":"计算 FK"}'
uv run --no-sync robot-rsi --profile examples/synthetic_adapter/profiles/compute_only.json call task_run '{"task_id":"fk-example","task_epoch":0,"request_id":"fk-1","skill":"fixture_fk"}'
```

The last command waits for the calculation; the pose is not physical or collision validation. The integration workflow [robot-onboarding](../../skills/robot-onboarding/SKILL.md) and [public guide](../../docs/integration.md) explain the factory and binding contract.
