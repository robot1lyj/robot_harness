"""C01-R1: usable original detail reads and preparation lifetime, without a model."""
import json
import os
from pathlib import Path

import pytest

from robot_rsi.agent_host import AgentHost
from robot_rsi.contracts import HarnessError, TaskContext
from robot_rsi.runtime import Runtime
from robot_rsi.storage import canonical
from robot_rsi.task_context import build_task_context

GOAL = '抓取红色积木放到蓝盒'


def capture(name, **body):
    if destination := os.environ.get('C01_R1_EVIDENCE_ROOT'):
        root = Path(destination)
        root.mkdir(parents=True, exist_ok=True)
        (root / (name + '.json')).write_text(json.dumps(body, ensure_ascii=False, indent=2) + '\n')


class DetailClient:
    """Protocol decision double: consumes supplied references, then original task tools."""
    def __init__(self, constraints):
        self.constraints = constraints
        self.turns = []
        self.reads = []

    def binding(self, role, config):
        return {'client': 'fixture', 'model': 'no-model', 'role': role}

    async def run(self, turn, emit):
        self.turns.append(turn['view'])
        entry = turn['view']['blocks']['required_details']
        assert entry['read_first']
        details = {}
        for reference in entry['read_first']:
            response = await turn['invoke'](reference['tool'], reference['arguments'])
            assert response['ok'], response
            details[reference['tool']] = response['result']
            self.reads.append({'reference': reference, 'result': response['result']})
        assert details['workspace_read']['workspace']['constraints'] == self.constraints
        checkpoint = details['task_checkpoint_get']
        assert checkpoint['constraints'] == self.constraints and checkpoint['goal'] == GOAL
        assert checkpoint['execution']['epoch'] == 0
        assert checkpoint['unknowns'] == []
        assert details['skill_describe']['name'] == 'pick_place'
        operation = await turn['invoke']('task_run', {'request_id': 'read-then-act',
                                                    'task_epoch': checkpoint['execution']['epoch']})
        assert operation['ok'] and operation['result']['terminal'], operation
        done = operation['result']
        assert done['status'] == 'succeeded' and done['hold']['confirmed']
        result = await turn['invoke']('task_finish', {'task_epoch': done['task_epoch'],
            'outcome': 'succeeded', 'summary': '完整抓放目标已验证；原完整约束已读（协议替身）',
            'operation_ids': [done['id']]})
        assert result['ok'], result
        assert (await turn['invoke']('task_archive', {}))['ok']
        return {'completed': True, 'returncode': 0}


@pytest.mark.parametrize('limit,large', [(1000, False), (20000, True)])
async def test_legal_overflow_can_read_complete_details_and_finish(profile, limit, large):
    constraints = (['需保留的工作区条件' + str(i) + '条' + '测' * 185 for i in range(12)]
                   if large else [])
    profile.task_limits.context_bytes = limit
    profile.workspace.constraints = constraints
    profile.active_task_id = 'overflow'
    async with Runtime(profile) as runtime:
        assert (await runtime.invoke('task_begin', {'goal': GOAL, 'constraints': constraints}))['ok']
        client = DetailClient(constraints)
        async with AgentHost(runtime).serve() as host:
            result = await host.run('overflow', client=client)
            assert result['status'] == 'complete'
            assert len(host.grants) == 1
            assert runtime.agent_state.control('overflow')['active_session'] is None
            again = await host.run('overflow', client=client)
            assert again['status'] == 'complete' and len(client.turns) == 1
        assert len(canonical(client.turns[0]).encode()) <= limit
        assert runtime.task_memory._load('overflow')['archived']
        assert len(runtime.store.all('operation')) == 1
        capture('overflow-' + str(limit), result=result, again=again, views=client.turns,
                reads=client.reads, sessions=runtime.store.all('agent_session'),
                model_calls=0, operations=runtime.store.all('operation'))


async def test_reference_view_versions_preserve_unknowns_and_new_constraints(profile):
    profile.task_limits.context_bytes = 1000
    async with Runtime(profile) as runtime:
        await runtime.invoke('task_begin', {'task_id': 'refs', 'goal': GOAL,
                                            'constraints': ['完整约束']})
        record = runtime.task_memory._load('refs')
        record['unknowns'] = ['未知材料不能省略' * 40]
        runtime.task_memory._write(record)
        args = TaskContext(task_id='refs', selected_skill='pick_place', blocks=True)
        first = build_task_context(runtime, args)
        repeated = build_task_context(runtime, args.model_copy(update={'delivered': first['delivered']}))
        assert repeated['blocks'] == {}
        assert 'task' not in first['delivered'] and 'results' not in first['delivered']
        detail = await runtime.invoke('task_checkpoint_get', {'task_id': 'refs'})
        assert detail['ok'] and detail['result']['unknowns'] == record['unknowns']
        record = runtime.task_memory._load('refs')
        record['constraints'].append('新的完整条件')
        runtime.task_memory._write(record)
        changed = build_task_context(runtime, args.model_copy(update={'delivered': first['delivered']}))
        assert list(changed['blocks']) == ['required_details']
        invalid = build_task_context(runtime, args.model_copy(update={'delivered': {'identity': 'lost'}}))
        assert invalid['full']
        default = await runtime.invoke('task_context', {'task_id': 'refs', 'selected_skill': 'pick_place'})
        assert default['ok'] and default['result']['details_required']
        assert len(canonical(default['result']).encode()) <= 1000
        assert (await runtime.invoke('task_checkpoint_get', {'task_id': 'refs'}))['result']['constraints'] == record['constraints']
        assert not runtime.store.all('operation')
        capture('reference-versions', first=first, repeated=repeated, changed=changed,
                default=default, detail=detail, model_calls=0)


async def test_preparation_failure_after_acquiring_session_releases_and_retries(profile, monkeypatch):
    import robot_rsi.agent_host as host_module

    profile.task_limits.context_bytes = 1000
    profile.active_task_id = 'prepare'
    original = host_module.build_task_context
    async with Runtime(profile) as runtime:
        await runtime.invoke('task_begin', {'goal': GOAL})
        before = runtime.task_memory._load('prepare')['execution'].copy()
        client = DetailClient([])
        async with AgentHost(runtime).serve() as host:
            def fail_once(*args, **kwargs):
                sessions = runtime.store.all('agent_session')
                assert len(sessions) == 1 and sessions[0]['status'] == 'running'
                assert runtime.agent_state.control('prepare')['active_session'] == sessions[0]['id']
                assert len(host.grants) == 2
                raise HarnessError('injected_preparation_failure', 'Fixture view retrieval failed')
            monkeypatch.setattr(host_module, 'build_task_context', fail_once)
            failed = await host.run('prepare', client=client)
            assert failed['status'] == 'preparation_failed'
            assert failed['preparation_error']['code'] == 'injected_preparation_failure'
            assert len(host.grants) == 1 and not client.turns
            control = runtime.agent_state.control('prepare')
            assert control['active_session'] is None and control['attempts'] == []
            assert runtime.agent_state.request('prepare')['role'] == 'fast'
            assert all(s['status'] != 'running' for s in runtime.store.all('agent_session'))
            assert runtime.task_memory._load('prepare')['execution'] == before
            assert not runtime.robot.commands
            old = failed['session']['id']
            stale = await runtime.invoke('task_run', {'task_epoch': 0, 'request_id': 'late'},
                                         agent_session_id=old)
            assert stale['error']['code'] == 'stale_agent_session'
            monkeypatch.setattr(host_module, 'build_task_context', original)
            resumed = await host.run('prepare', client=client)
            assert resumed['status'] == 'complete' and len(client.turns) == 1
            assert len(host.grants) == 1 and runtime.agent_state.control('prepare')['active_session'] is None
        assert [s['role'] for s in runtime.store.all('agent_session')] == ['fast', 'fast']
        capture('preparation-retry', failed=failed, resumed=resumed, stale=stale,
                control_after_failure=control, sessions=runtime.store.all('agent_session'),
                model_calls=0, operations=runtime.store.all('operation'))
