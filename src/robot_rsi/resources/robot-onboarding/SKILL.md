---
name: robot-onboarding
description: Inspect and classify existing robot software; deliver working integration artifacts when implementation and host development tools are authorized.
---

# Robot project onboarding

Use `robot_onboarding` for relevant inspection, integration or implementation gaps. Start from the host's environment summary, purpose, installed versions and valid bindings; no initialization skill is needed. Keep source, local extension development/tests, configuration and runtime state in one robot usage directory; Harness/shared extensions remain global. Reuse delivered facts and relevant `workspace_read` note cards/keys. Ordinary runtime uses installed capabilities/methods without repeating onboarding.

## Inspect and classify

For a request to inspect, selectively read the README, configuration, entry points, relevant owner/implementation and tests with host-enabled `project_files`. Follow offsets; stop when the evidence supports the request. Do not run/import the application, edit source or connect devices. Deliver sourced findings, the relevant mapping and a minimal recommendation.

For each relevant source record: module/symbol -> category -> actual contract/version or skill dependencies -> treatment and reason -> artifact, evidence and gaps. Distinguish device resources, capability behavior, provider/adapter implementations, reusable task skills, versioned model/calibration/weight assets and instance configuration. Choose direct reuse, thin adaptation, extraction or missing separately for each source. Stable methods with a goal, applicability and result criterion belong in executable skills; SDK functions or UI buttons are not automatically skills. Preserve one device writer. Do not default to wrapping the entire old API or register every function. Report a missing public contract with a minimal reproducer.

## Implement when requested and equipped

An explicit implementation request with host-authorized development tools continues through actual local provider/skill/configuration artifacts, builds, fixed installation and necessary invocation/validation against the whole goal. A plan or inventory alone does not complete implementation. A restricted source-only host instead identifies the exact missing tools and hands off the unfinished work; it cannot grant itself development or device permissions.

Reuse Profile/Runtime/Registry/Store. With uv, declare core and all extension wheels/fixed sources in pyproject.toml and uv.lock; at an execution-task boundary install with `uv sync --locked --no-dev --no-editable`. Keep configuration/assets/state outside .venv. Start with environment executables or `uv run --no-sync`; ordinary runtime calls do not install/update packages. Check necessary calls and provider/skill persistence after normal sync and restart. Preserve active task pins. System SDK/CUDA/CAN conditions are deployment prerequisites, not implicit Python installs. The public integration guide owns detailed commands.

## Reuse and finish

Similar device models only suggest candidates. Reuse requires matching contracts, SDK/protocol and dependency support, units/frames/time semantics and applicable evidence. Instance addresses, serial numbers, local bindings and calibration remain local; sharing a provider does not transfer their validity.

For useful shared candidates, leave a short source/version/interface/dependency record with existing checks and gaps for maintainers to collect selected changes. Continue ordinary local fixes without waiting for collection. Do not automatically upload or send messages; no package-market search or extra approval round is required.

Save changed sourced knowledge with `workspace_note`, separating facts, hypotheses and untested conditions. For inspection, a note can evidence completion without an operation (`kind=workspace_note`, id=its key). For implementation, cite actual artifacts and validation, distinguishing created, installed, used and still missing; do not finish successfully from a proposed plan. When the full goal is evidenced, use `task_finish` with the current epoch and applicable operation/evidence references, then archive. Report performed work, gaps and the smallest next step in Chinese.
