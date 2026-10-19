"""Compatibility and dependency boundaries for the static catalog/reference-task split."""
import json
import subprocess
import sys

from robot_rsi import recipe, runtime, skill_models, skills, tool_specs
from robot_rsi.builtin_tasks import pick_place
from robot_rsi.runtime import Runtime
from robot_rsi.skill_result import SkillResult


def test_old_exports_are_the_same_objects():
    assert runtime.TOOLS is tool_specs.TOOLS
    assert runtime.MOTION_TOOLS is tool_specs.MOTION_TOOLS
    assert runtime.OPERATION_TOOLS is tool_specs.OPERATION_TOOLS
    assert skills.pick_place is pick_place.pick_place
    assert skills.PICK_PLACE_VERSION == pick_place.PICK_PLACE_VERSION
    assert skill_models.PickPlaceParameters is pick_place.PickPlaceParameters
    assert skill_models.SkillResult is SkillResult
    for name in recipe.__all__:
        assert getattr(recipe, name) is getattr(pick_place, name)


def test_standalone_memory_metadata_and_registry_do_not_import_runtime(tmp_path):
    script = '''
import json,sys
from robot_rsi.contracts import Profile, MemoryRecall
from robot_rsi.memory import Memory
from robot_rsi.skill_result import SkillResult
from robot_rsi import skill_management
from robot_rsi.storage import Store
profile=Profile(state_root=sys.argv[1])
calls=[]
original=skill_management.installed_packages
def counted():
    calls.append(1)
    return original()
skill_management.installed_packages=counted
store=Store(profile)
try:
    one=Memory(store)
    two=Memory(store)
    assert one.skill_versions==two.skill_versions=={'pick_place':'0.2.0'}
    for _ in range(5):
        assert not one.recall(MemoryRecall())['records']
    assert len(calls)==1
    assert 'robot_rsi.skills' not in sys.modules
    assert 'robot_rsi.builtin_tasks.pick_place' not in sys.modules
    assert 'robot_rsi.runtime' not in sys.modules
finally:
    store.close()
from robot_rsi.registry import Registry
registry=Registry(profile)
try:
    assert registry.describe('workspace_context')['entrypoint']['tool']=='workspace_read'
    assert 'robot_rsi.runtime' not in sys.modules
finally:
    registry.close()
print(json.dumps({'metadata_scans':len(calls),'runtime_imported':False}))
'''
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)],
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {"metadata_scans": 1, "runtime_imported": False}


async def test_runtime_supplies_registry_versions_without_distribution_fallback(profile, monkeypatch):
    from robot_rsi import skill_management

    def not_needed():
        raise AssertionError("Runtime must supply actual workspace versions")

    monkeypatch.setattr(skill_management, "installed_code_versions", not_needed)
    async with Runtime(profile) as host:
        assert host.memory.skill_versions == host.registry.versions()
        assert {"pick_place", "task_memory", "workspace_context"} <= host.memory.skill_versions.keys()
