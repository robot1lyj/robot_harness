"""Optional whole-goal consistency, shared by task ending and coding handoff.

Domain code judges observations. This module reads existing results only; it never
imports extensions, executes checks, or infers physical effects from a criterion.
"""
from .contracts import CriterionResult, GoalDelivery
from .storage import canonical


def preserve(previous, proposed, goal):
    if proposed is None:
        return previous
    value = proposed.model_dump() if isinstance(proposed, GoalDelivery) else proposed
    if value["original_goal"] != goal:
        raise ValueError("Delivery must preserve the original whole goal")
    if previous and any(previous[k] != value[k] for k in ("kind", "original_goal", "criteria")):
        raise ValueError("Original goal, delivery kind and necessary criteria are fixed")
    return value


def check_results(delivery, results):
    """Return missing/negative goal evidence, without rejudging a domain observation."""
    by_id = {}
    issues = []
    if not isinstance(results, list):
        return ["Criterion results must be a list of sourced results"]
    expected = {c["id"] for c in delivery["criteria"]}
    for raw in results:
        try:
            result = CriterionResult.model_validate(raw).model_dump()
        except ValueError as exc:
            issues.append("Invalid criterion result: " + str(exc))
            continue
        identity = result["criterion_id"]
        if identity in by_id or identity not in expected:
            issues.append("Duplicate or undeclared criterion: " + identity)
        by_id[identity] = result
    for criterion in delivery["criteria"]:
        if not criterion["required"]:
            continue
        result = by_id.get(criterion["id"])
        if not result or result["status"] != "passed" or result["remaining"] or not result["evidence_refs"]:
            issues.append("Required criterion lacks passed sourced evidence: " + criterion["id"])
    if delivery["remaining"]:
        issues.append("Necessary delivery work remains: " + "; ".join(delivery["remaining"]))
    return issues


def check_method(delivery, report):
    """Structural correspondence of existing Registry/run reports; no plugin load."""
    entry = delivery.get("entry")
    if not entry or not isinstance(report, dict):
        return ["Reusable method requires a versioned task_run entry and existing registration/run reports"]
    registration = report.get("registration", {})
    if not isinstance(registration, dict) or not isinstance(report.get("runs", []), list):
        return ["Method report requires a registration object and a run list"]
    issues = []
    if (registration.get("name"), registration.get("version"), registration.get("kind")) != (entry["skill"], entry["version"], "code") or not registration.get("entry"):
        issues.append("Method registration differs from the callable code entry")
    runs = report.get("runs", [])
    valid_inputs = set()
    operation_ids = set()
    for run in runs:
        if not isinstance(run, dict) or not isinstance(run.get("operation"), dict):
            issues.append("Method run requires an original operation object")
            continue
        op = run["operation"]
        args, result = op.get("args", {}), op.get("result") or {}
        if not isinstance(args, dict) or not isinstance(result, dict):
            issues.append("Method run requires original arguments and result objects")
            continue
        if (op.get("skill_name"), op.get("skill_version"), op.get("status")) != (entry["skill"], entry["version"], "succeeded"):
            issues.append("Method run is not a successful invocation of the fixed entry")
            continue
        if (not op.get("id") or op["id"] in operation_ids or not op.get("scope")
                or not op.get("capabilities") or not run.get("source")
                or result.get("verified") is not True or result.get("completed") is not True):
            issues.append("Method run lacks distinct operation/provenance or whole-goal completion")
        if "manipulator" in op.get("resources", []) and not (op.get("hold") or {}).get("confirmed"):
            issues.append("Method motion report lacks confirmed HOLD")
        if check_results(delivery, result.get("criterion_results", [])):
            issues.append("Method run lacks the necessary end-to-end criterion evidence")
        if args.get("skill") != entry["skill"] or "parameters" not in args:
            issues.append("Method run lacks actual callable inputs")
        else:
            valid_inputs.add(canonical(args["parameters"]))
        operation_ids.add(op.get("id"))
    if len(valid_inputs) < 2 or canonical(entry["parameters"]) not in valid_inputs:
        issues.append("Method needs end-to-end and different-input reuse reports including the example input")
    if report.get("remaining"):
        issues.append("Method evidence reports necessary remaining work")
    return issues


def operation_results(store, identities):
    """Selected sources cannot hide an earlier negative under a later step's success.

    A later complete recheck can be selected alone; retaining both sources retains
    their negative evidence. No physical execution status is changed here.
    """
    results = {}
    rank = {"passed": 0, "unknown": 1, "failed": 2}
    for identity in identities:
        operation = store.get("operation", identity)
        for raw in (operation.get("result") or {}).get("criterion_results", []):
            result = CriterionResult.model_validate(raw).model_dump()
            previous = results.get(result["criterion_id"])
            if previous is None or rank[result["status"]] >= rank[previous["status"]]:
                results[result["criterion_id"]] = result
    return list(results.values())
