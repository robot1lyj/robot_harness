import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from robot_rsi.agent_client import CodexClient
from robot_rsi.agent_host import AgentHost
from robot_rsi.contracts import HarnessError, TaskContext
from robot_rsi.recipe import run_recipe
from robot_rsi.runtime import Runtime
from robot_rsi.storage import canonical
from robot_rsi.task_context import build_task_context


@pytest.fixture
def native(tmp_path, monkeypatch):
    root = tmp_path / 'login'
    root.mkdir()
    (root / 'auth.json').write_text('{}')
    monkeypatch.setenv('CODEX_HOME', str(root))
    trace = tmp_path / 'native.jsonl'
    monkeypatch.setenv('C01_TRACE', str(trace))
    original = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        if args[0] == 'codex':
            args = (sys.executable, str(Path(__file__).parent / 'fixtures/c01_app_server.py'))
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(CodexClient, 'binding', lambda self, role, config: {
        'client': 'protocol-fixture', 'role': role, 'model': config.model or 'fixture',
        'reasoning_effort': config.reasoning_effort})
    return trace


def capture(name, **body):
    if destination := os.environ.get('C01_EVIDENCE_ROOT'):
        root = Path(destination)
        root.mkdir(parents=True, exist_ok=True)
        (root / (name+'.json')).write_text(json.dumps(body, ensure_ascii=False, indent=2)+'\n')


@pytest.mark.parametrize('failures,decisions', [(0,2),(2,3)])
async def test_native_reuse_compact_results_and_host_wait(profile, native, failures, decisions):
    profile.active_task_id = 'native-task'
    profile.mock_grasp_failures = failures
    profile.mock_step_s = 0.02
    profile.task_limits.duration_s = 300
    profile.workspace.title = '模拟桌面'
    profile.workspace.constraints = ['仅使用 mock，结果与证据标为 synthetic。']
    async with Runtime(profile) as runtime:
        assert (await runtime.invoke('task_begin', {'goal':'抓取红色积木放到蓝盒；用操作证据确认整个目标完成，保存任务结果并归档。','constraints':['仅使用 mock，结果与证据标为 synthetic。']}))['ok']
        async with AgentHost(runtime).serve() as host:
            result = await host.run('native-task')
        assert result['status'] == 'complete'
        sessions = runtime.store.all('agent_session')
        assert len(sessions) == decisions
        assert len({s['native_session_id'] for s in sessions}) == 1
        assert len({s['native_turn_id'] for s in sessions}) == decisions
        messages = [json.loads(line) for line in native.read_text().splitlines()]
        initialized = next(m['params']['dynamicTools'] for m in messages if m.get('method') == 'thread/start')
        assert {'resource_connect', 'robot_state'} <= {tool['name'] for tool in initialized}
        assert sum(m.get('method') == 'initialize' for m in messages) == 1
        assert sum(m.get('method') == 'thread/start' for m in messages) == 1
        views = [json.loads(m['params']['input'][0]['text'].split('\n',1)[1])
                 for m in messages if m.get('method') == 'turn/start']
        assert views[0]['full'] and all(not v['full'] for v in views[1:])
        assert all('workspace' not in v['blocks'] and 'skills' not in v['blocks'] for v in views[1:])
        names = [json.loads(row[0])['name'] for row in runtime.store.db.execute(
            "SELECT body FROM events WHERE kind='tool.started'")]
        assert 'operation_get' not in names and 'task_context' not in names
        assert 'task_checkpoint_save' not in names
        assert runtime.task_memory._load('native-task')['archived']
        assert all(s['usage']['input_tokens'] == 100 for s in sessions)
        capture('native-'+str(failures), sessions=sessions, views=views, tools=names,
                messages=messages, operations=runtime.store.all('operation'), result=result,
                qualification='protocol_double; token counts synthetic')


async def test_blocks_repeated_reads_changes_invalid_cursor_and_unknown(profile):
    async with Runtime(profile) as runtime:
        await runtime.invoke('task_begin', {'task_id':'blocks','goal':'抓取积木','constraints':['保留约束']})
        args = TaskContext(task_id='blocks', selected_skill='pick_place', blocks=True)
        first = build_task_context(runtime, args)
        second = await runtime.invoke('task_context', {**args.model_dump(), 'delivered':first['delivered']})
        assert second['ok'] and not second['result']['blocks']
        assert second['result']['metrics']['block_bytes'] == 2
        record = runtime.task_memory._load('blocks')
        record['unknowns'] = ['效果未知，不可重放']
        runtime.task_memory._write(record)
        changed = build_task_context(runtime, args.model_copy(update={'delivered':first['delivered']}))
        assert list(changed['blocks']) == ['results']
        assert changed['blocks']['results']['unknowns'] == ['效果未知，不可重放']
        invalid = build_task_context(runtime, args.model_copy(update={'delivered':{'identity':'wrong'}}))
        assert invalid['full'] and invalid['blocks']['task']['constraints'] == ['保留约束']
        await runtime.invoke('task_begin', {'task_id':'other','goal':'另一目标'})
        other = build_task_context(runtime, TaskContext(task_id='other',blocks=True,delivered=first['delivered']))
        assert other['full'] and '效果未知' not in canonical(other)
        capture('blocks', first=first, repeated=second, changed=changed, other=other)


async def test_long_operation_cancel_is_one_native_turn(profile, native):
    profile.active_task_id = 'long'
    profile.mock_step_s = 0.2
    async with Runtime(profile) as runtime:
        await runtime.invoke('task_begin', {'goal':'抓取积木放到盒子'})
        async with AgentHost(runtime).serve() as host:
            pending = asyncio.create_task(host.run('long'))
            while not runtime.store.all('operation'):
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.25)
            assert len(runtime.store.all('agent_session')) == 1
            await runtime.invoke('task_cancel', {'task_id':'long'})
            result = await asyncio.wait_for(pending, 3)
        assert result['status'] in ('superseded','cancelled')
        assert len(runtime.store.all('agent_session')) == 1
        assert runtime.store.all('operation')[0]['status'] == 'cancelled'
        capture('cancel-wait', result=result, sessions=runtime.store.all('agent_session'),
                operations=runtime.store.all('operation'))


async def test_decision_boundary_rebuild_role_and_task_separation(profile, native, monkeypatch):
    monkeypatch.setenv('C01_MODE', 'readonly')
    async with Runtime(profile) as runtime:
        client = CodexClient()
        async with AgentHost(runtime).serve():
            for identity in ['one','two']:
                await runtime.invoke('task_begin', {'task_id':identity,'goal':'抓取积木'})
            base = {'scope':runtime.store.scope,'task_id':'one','binding':{
                'client':'fixture','role':'fast','model':'fixture','reasoning_effort':None},
                'client_root':str(runtime.store.root/'native-client'),'history_bytes':100000,
                'timeout_s':5,'tools':[]}
            for i, (task,role,rebuild) in enumerate([
                ('one','fast',False),('one','fast',False),('one','teacher',False),
                ('two','fast',False),('one','fast',True)]):
                turn = {**base,'task_id':task,'binding':{**base['binding'],'role':role}}
                if rebuild:
                    client.threads[client.key(turn)]['visible_bytes'] = 100001
                view = build_task_context(runtime, TaskContext(task_id=task,blocks=True,
                                                              delivered=client.context_cursor(turn)))
                turn.update(view=view,prompt='续接\n'+canonical(view))
                async def invoke(name, arguments):
                    return {'ok':True,'result':{'blocks':{},'qualification':'protocol_fixture'}}
                turn['invoke'] = invoke
                events=[]
                assert (await client.run(turn, events.append))['completed']
                if i == 1:
                    assert not view['full'] and view['blocks'] == {}
                else:
                    assert view['full']
            assert client.process_starts == 1 and client.handshakes == 1
            trace = [json.loads(line) for line in native.read_text().splitlines()]
            assert sum(m.get('method') == 'thread/start' for m in trace) == 4
            assert sum(m.get('method') == 'thread/unsubscribe' for m in trace) == 1
            await client.close()
        capture('rebuild-separation', messages=trace)


@pytest.mark.parametrize('failures,expected', [(0,'succeeded'),(2,'pending')])
async def test_recipe_complete_or_failure_retained(profile, failures, expected):
    profile.mock_grasp_failures = failures
    async with Runtime(profile) as runtime:
        result = await run_recipe(runtime,'recipe',{})
        record = runtime.task_memory._load('recipe')
        assert result['task_outcome']['outcome'] == expected
        assert record['archived'] == (expected == 'succeeded')
        count = len(runtime.robot.commands)
        await run_recipe(runtime,'recipe',{})
        assert len(runtime.robot.commands) == count
        assert not runtime.store.all('agent_session')
        capture('recipe-'+str(failures), result=result, checkpoint=record,
                operations=runtime.store.all('operation'), model_calls=0)


async def test_recipe_does_not_finish_free_goal_or_preparation(profile):
    async with Runtime(profile) as runtime:
        await runtime.invoke('task_begin', {'task_id':'free','goal':'抓取积木后还要焊接'})
        with pytest.raises(HarnessError, match='entire task goal'):
            await run_recipe(runtime,'free',{})
        prepared = await runtime.invoke('task_run', {'task_id':'free','task_epoch':0,
            'request_id':'prepare','skill':'task_memory'})
        assert prepared['ok'] and prepared['result']['prepared']
        assert not runtime.task_memory._load('free').get('task_result')
        assert not runtime.store.all('operation')


async def test_native_restart_from_checkpoint_does_not_replay(profile, native):
    class DisconnectAfterStep(CodexClient):
        calls = 0
        async def run(self, turn, emit):
            self.calls += 1
            if self.calls > 1:
                raise ConnectionError('fixture host loss after confirmed step')
            return await super().run(turn, emit)

    profile.active_task_id = 'restart'
    async with Runtime(profile) as runtime:
        await runtime.invoke('task_begin', {'goal':'抓取积木放到盒子'})
        async with AgentHost(runtime).serve() as host:
            result = await host.run('restart', client=DisconnectAfterStep())
        assert result['status'] == 'disconnected'
        before = runtime.task_memory._load('restart')
        assert len(runtime.store.all('operation')) == 1
    async with Runtime(profile) as runtime:
        async with AgentHost(runtime).serve() as host:
            result = await host.run('restart')
        after = runtime.task_memory._load('restart')
        assert result['status'] == 'complete'
        assert after['execution']['deadline_at'] == before['execution']['deadline_at']
        assert after['skill_versions'] == before['skill_versions']
        assert len(runtime.store.all('operation')) == 1 and not runtime.robot.commands
        capture('native-restart', before=before, after=after,
                sessions=runtime.store.all('agent_session'))


async def test_recipe_unknown_never_finishes_and_parameter_changes_rejected(profile):
    profile.mock_grasp_failures = 2
    async with Runtime(profile) as runtime:
        await run_recipe(runtime,'unknown',{})
        operation = runtime.store.all('operation')[0]
        operation.update(status='unknown', error={'code':'verification_unknown'})
        runtime.store.put('operation',operation['id'],operation)
        result = await run_recipe(runtime,'unknown',{})
        record = runtime.task_memory._load('unknown')
        assert result['task_outcome']['outcome'] == 'pending'
        assert not record['archived'] and not record.get('task_result')
        with pytest.raises(HarnessError):
            await run_recipe(runtime,'unknown',{'grasp_retries':0})
        capture('recipe-unknown', result=result, checkpoint=record, operation=operation)


@pytest.mark.parametrize('limit,large', [(1000, False), (20000, True)])
async def test_native_reference_entry_reads_complete_sources_before_actions(profile, native, limit, large):
    constraints = (['需保留的工作区条件' + str(i) + '条' + '测' * 185 for i in range(12)]
                   if large else [])
    profile.task_limits.context_bytes = limit
    profile.workspace.constraints = constraints
    profile.active_task_id = 'native-overflow'
    async with Runtime(profile) as runtime:
        assert (await runtime.invoke('task_begin', {'goal': '抓取红色积木放到蓝盒',
                                                   'constraints': constraints}))['ok']
        async with AgentHost(runtime).serve() as host:
            result = await host.run('native-overflow')
            assert result['status'] == 'complete' and len(host.grants) == 1
        trace = [json.loads(line) for line in native.read_text().splitlines()]
        results = [json.loads(m['result']['contentItems'][0]['text'])['result']
                   for m in trace if 'contentItems' in m.get('result', {})]
        workspaces = [r for r in results if 'workspace' in r and 'environment' in r]
        checkpoints = [r for r in results if r.get('goal') == '抓取红色积木放到蓝盒']
        assert workspaces and all(r['workspace']['constraints'] == constraints for r in workspaces)
        assert checkpoints and all(r['constraints'] == constraints for r in checkpoints)
        views = [json.loads(m['params']['input'][0]['text'].split('\n', 1)[1])
                 for m in trace if m.get('method') == 'turn/start']
        assert len(views) == 2 and all(len(canonical(v).encode()) <= limit for v in views)
        assert 'task' not in views[0]['delivered'] and 'results' not in views[0]['delivered']
        assert len(runtime.store.all('operation')) == 1
        assert all(s['status'] == 'completed' for s in runtime.store.all('agent_session'))
        if destination := os.environ.get('C01_R1_EVIDENCE_ROOT'):
            Path(destination).mkdir(parents=True, exist_ok=True)
            (Path(destination) / ('native-overflow-' + str(limit) + '.json')).write_text(json.dumps({
                'qualification': 'App Server protocol double; zero model calls', 'result': result,
                'views': views, 'original_sources_read': results,
                'sessions': runtime.store.all('agent_session'), 'trace': trace,
            }, ensure_ascii=False, indent=2) + '\n')
