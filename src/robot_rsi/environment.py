"""Environment facts projected from existing owners; never a second runtime or inventory."""

from .contracts import SkillQuery
from .scene_bindings import BindingRead


def configured_environment(profile):
    """Safe before MCP lifespan starts: configuration only, no factory or device discovery."""
    source_only = profile.robot is None and profile.host.adapter is None
    return {
        "id": f"{profile.station}/{profile.namespace}",
        "role": profile.role,
        "project_id": profile.project.id if profile.project else None,
        "purpose": profile.project.purpose if profile.project else "run",
        "qualification": "source_only" if source_only else "configured_only",
        "state": "configuration_only; physical availability not checked",
        "details": "workspace_read / task_context; tools_list for host tool scope",
    }


def environment_summary(runtime, task_id=None, bindings=None):
    bound = bindings if bindings is not None else runtime.task_bindings(task_id)
    task = runtime.task_memory._load(task_id) if task_id else None
    execution = (task or {}).get("execution") or {}
    capabilities = bound.catalog()
    query = task["goal"][:200] if task else ""
    skills = runtime.registry.cards(SkillQuery(query=query, limit=3), bindings=bound)
    if task is None:
        skills["skills"] = [{key: card[key] for key in ("name", "version", "kind", "available")}
                            for card in skills["skills"]]
    pinned = (task or {}).get("skill_versions", {})
    for card in skills["skills"]:
        if card["name"] in pinned and card["version"] != pinned[card["name"]]:
            fixed = runtime.registry.describe(card["name"], pinned[card["name"]], bindings=bound)
            card.update({key: fixed[key] for key in card})
    versions = runtime.resource_versions(task_id) if task_id else runtime.assets.selected
    selected = runtime.scenes.read(BindingRead(), task_id)
    resources = runtime.adapter.describe()
    return {
        **configured_environment(runtime.profile),
        "scope": runtime.store.scope,
        "installation_digest": execution.get("installation_digest") or runtime.task_memory.installation_digest,
        "tool_scope": {"source_read": runtime.project.can_read_sources,
                       "development_tools": "not provided by this Harness",
                       "execution": "installed capabilities and task permissions only"},
        "provider_qualifications": sorted({c["qualification"] for c in capabilities}),
        "capabilities": {"cards": capabilities[:8], "total": len(capabilities),
                         "omitted": max(0, len(capabilities) - 8), "details": "capabilities_list"},
        "resources": {"cards": resources[:3], "total": len(resources),
                      "omitted": max(0, len(resources) - 3), "details": "capabilities_list"},
        "skills": {**skills, "details": "skills_list / skill_describe",
                   "task_versions": pinned},
        "selected_assets": versions,
        "bindings": selected,
        "missing": "Inspect skill missing_capabilities; binding_candidates for unselected providers. "
                   "Configured providers are not live availability evidence.",
    }
