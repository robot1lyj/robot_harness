# One robot usage directory, independent locked environment

This authored offline example extracts a slider height inspection from legacy_lab,
reuses its calculation and recording reader, and installs lab_adapter as a separate wheel.
The [source mapping](integration-map.md) records the classification and reasons.
It is not a hardware test or an unknown-project benchmark.

Use one directory for this robot's sources, extension development/tests, configuration,
Python environment and runtime state. Harness development/release is shared globally.
The optional second profile below tests isolation inside the same directory; it is not
a second robot project or a required validation project.

```text
slider-use/
  sources/legacy_lab/       existing authored source (not on the runtime import path)
  sources/extension/        local provider and task package
  pyproject.toml / uv.lock  all runtime Python dependencies, including both wheels
  wheels/                  fixed core and extension artifacts
  .venv/                   disposable non-editable installation
  manage.py / a.json        host setup, normal profile
  b.json                   optional same-project isolation profile
  state/                   original Store/Registry scopes, outside .venv
```

From the shared Harness checkout, choose a new usage directory. Copy only these public
example files and source packages; no development AGENTS, chat history or evaluation answers:

```bash
USE_DIR=/tmp/slider-use
mkdir -p "$USE_DIR/wheels" "$USE_DIR/sources"
cp examples/robot_environment/{README.md,pyproject.toml,uv.lock,manage.py,integration-map.md} "$USE_DIR/"
cp -R examples/legacy_lab "$USE_DIR/sources/legacy_lab"
cp -R examples/project_adapter "$USE_DIR/sources/extension"
uv build --wheel --out-dir "$USE_DIR/wheels"
uv build --wheel "$USE_DIR/sources/extension" --out-dir "$USE_DIR/wheels"
cd "$USE_DIR"
uv sync --locked --no-dev --no-editable
uv run --no-sync python manage.py prepare
```

`prepare` is an explicit host setup operation: it discovers installed skill packages, enables
the inspection method, validates read/compute providers and saves named assets/bindings and
a short source note. It creates two profiles with separate original Store scopes. It does
not install packages, start the legacy application or connect hardware. Run it once in a new
state directory; existing immutable binding versions are retained instead of overwritten.
All modules used at runtime come from site-packages, not sources/ or an editable checkout.

Use the environment's executable with the existing Codex CLI/login:

```bash
.venv/bin/robot-rsi --profile a.json agent run "请检查离线滑台在0.3米位置的工具高度是否位于0.19到0.21米之间，并保存结果。"
```

The host supplies environment facts before skill selection. The Agent discovers the installed
method, runs it through Registry/task_run and reports its actual conclusion in Chinese.
Recorded images retain their original timestamps. The bounded known formula and authored
intrinsics/model records do not establish physical calibration, live readiness, IK or motion.
The business result `within_bounds=false` is a valid completed inspection, distinct from a
bad input or an unavailable required capability.

For deterministic fresh-process reuse, with no additional model inference:

```bash
uv run --no-sync python manage.py run --task next
uv run --no-sync python manage.py run --environment b --task other --lower 0.24 --upper 0.26
uv run --no-sync python manage.py run --task bad --lower 0.3 --upper 0.2
```

These commands explicitly choose the method, reuse each saved binding without new trials,
and finish/archive the task. They are protocol exercises, not autonomous selection evidence.
The last records an explanatory failed task without rewriting a previous success.

Normal synchronization must preserve every declared provider/skill:

```bash
uv sync --locked --no-dev --no-editable
uv run --no-sync robot-rsi adapter list
uv run --no-sync robot-rsi --profile a.json call skills_list '{"query":"高度"}'
```

Do not use a lock-external pip install as a deployment requirement. For updates, build new
candidate wheels, update their declared sources/versions and `uv lock` in this same project,
then sync at a task boundary. The checked-in lock refers to the exact supplied example wheel
content; changing a wheel requires regenerating the lock rather than bypassing it. Preserve
profiles/state/assets when recreating .venv. Startup uses the environment executable or
`uv run --no-sync` and does not resolve/install dependencies. System SDK/CUDA/CAN prerequisites
belong to separately authorized deployment; none is needed or installed here. uv's Python
installation behavior is not a robot latency measurement.

For an external MCP client use `.venv/bin/robot-rsi --profile a.json mcp`. Initialize carries
configuration identity/qualification; workspace_read/task_context provide installed/task facts.
A source-only first look can use `robot-rsi mcp --project PATH`, sharing the agent run root
mapping. Protocol delivery does not prove any particular desktop client/model followed it.
