---
name: robot-onboarding
description: Inspect existing robot software and propose a source-grounded integration path; implement adapters only for an explicit integration request.
---

# Robot project onboarding

Use `robot_onboarding` to understand the current project before proposing integration. Reuse the supplied project root, purpose, installed versions and saved facts. If notes exist, find cards with `workspace_read(query="")`, then read relevant keys. Check only source changes or uncertainties that affect the current request.

For an inspection request, use `project_files` to list the root and selectively read the README, configuration, relevant entry points, device owner and implementation/tests. Follow returned offsets when more text is needed. Do not run/import the project, connect hardware or write source. Stop reading once the evidence supports the requested conclusion; do not scan every file.

Distinguish existing implementations, usable source functions, missing dependencies and untested behavior. Cite paths and symbols. Classify devices, computation such as FK/depth, versioned assets, stable task methods and application/UI code. A function or button is not an integrated capability. Suggest the smallest useful next step using existing capability contracts and installed extensions; preserve a single device writer. Ordinary local gaps belong to the project or extension package, public semantic gaps to the framework with a minimal reproducer.

Only an explicit implementation request proceeds to adapter/skill packaging and validation. Prefer thin mapping of existing code, units, frames, timestamps and feedback; do not invent missing algorithms or claim integration from static reading. The public integration guide describes packaging.

Save a concise sourced inventory or updated integration note through `workspace_note`, separating facts, hypotheses and untested conditions. For the current information-only goal, finish with `task_finish` referencing that note's key (`kind=workspace_note`), then archive. No operation is needed to inspect source. Return the actual conclusion and minimal next step in Chinese. Do not add repeated reviews, approval rounds or per-step summaries.
