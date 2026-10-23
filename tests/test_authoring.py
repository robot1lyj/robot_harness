"""Public skill authoring and original-record export; no hand-built method proof JSON."""
import json
import subprocess
import sys

import pytest

from robot_rsi.authoring import criterion, result
from robot_rsi.evidence import export_method
from robot_rsi.delivery import check_method
from robot_rsi.runtime import Runtime
from test_goal_delivery import configured, prepare, parameters, run, contract


def test_authoring_import_is_independent_and_outcomes_are_explicit():
    reply = subprocess.run([sys.executable, '-c', '''
import sys
from robot_rsi.authoring import result
assert not any('robot_rsi.'+n in sys.modules for n in ('runtime','storage','registry','agent_client','coding_host','builtin_tasks.pick_place'))
try: result(qualification='fixture')
except TypeError: pass
else: raise AssertionError('Outcome flags must be explicit')
'''], check=True)
    assert reply.returncode == 0
    unknown = criterion('normal', 'unknown', source='actual unreadable image', evidence=[], remaining=['resample'])
    output = result(completed=True, verified=False, qualification='fixture', criteria=[unknown])
    assert output['verified'] is False and output['criterion_results'][0]['status'] == 'unknown'
    with pytest.raises(ValueError):
        criterion('normal', 'invented_success', source='fixture', evidence=[])


async def test_public_method_export_preserves_actual_runs_and_reuse(profile):
    async with Runtime(configured(profile)) as runtime:
        inputs = [parameters(), parameters((0.2, 0.6, 0.8))]
        for index, args in enumerate(inputs):
            await prepare(runtime, task='sdk'+str(index), args=args)
            assert (await run(runtime, task='sdk'+str(index), args=args))['status'] == 'succeeded'
        report = export_method(runtime.store, runtime.registry, 'multipoint_inspection', ['sdk0', 'sdk1'])
        assert not check_method(contract(kind='reusable_method'), report)
        assert all(run['source']['call_ids'] for run in report['runs'])
        assert {r['source']['task_id'] for r in report['runs']} == {'sdk0', 'sdk1'}
        assert all(r['operation']['skill_version'] == '0.1.1' for r in report['runs'])
        # Export is a read of the original result, including a later negative observation.
    async with Runtime(configured(profile, scenes={'0.7': 'anomaly'})) as runtime:
        await prepare(runtime, task='negative')
        await run(runtime, task='negative')
        report = export_method(runtime.store, runtime.registry, 'multipoint_inspection', ['negative'])
        assert check_method(contract(kind='reusable_method'), report)
        assert report['runs'][0]['operation']['result']['criterion_results'][1]['status'] == 'failed'
