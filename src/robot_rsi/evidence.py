"""Host-selected, scoped evidence export using existing record/event identities."""
import json

from .contracts import HarnessError
from .installation import installation_identity


def export_task(store, registry, task_id):
    checkpoint = store.get('task_checkpoint', 'task-checkpoint_' + task_id)
    operations = [store.get('operation', key) for key in checkpoint['operation_ids']]
    operation_ids = set(checkpoint['operation_ids'])
    sessions = [row for row in store.all('agent_session') if row['task_id'] == task_id]
    events = []
    for row in store.db.execute('SELECT seq,at,kind,body FROM events ORDER BY seq'):
        body = json.loads(row['body'])
        if (body.get('task_id') == task_id or body.get('operation_id') in operation_ids
                or body.get('id') in operation_ids):
            events.append({'seq': row['seq'], 'at': row['at'], 'kind': row['kind'], 'data': body})
    found = {}

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            if value.get('kind') in {'artifact', 'camera_observation', 'observation', 'depth', 'memory'} and isinstance(value.get('id'), str):
                key = (value['kind'], value['id'])
                if key not in found:
                    try:
                        found[key] = store.get(*key)
                        visit(found[key])
                    except HarnessError:
                        pass
            for key, item in value.items():
                kind = {'observation_id': 'observation', 'camera_observation_id': 'camera_observation',
                        'plan_id': 'plan', 'depth_id': 'depth', 'report_id': 'artifact'}.get(key)
                if not kind and (key.endswith('_artifact') or key == 'artifact_id'):
                    kind = 'artifact'
                if kind and isinstance(item, str) and (kind, item) not in found:
                    try:
                        record = store.get(kind, item)
                    except HarnessError:
                        continue
                    found[kind, item] = record
                    visit(record)
                if isinstance(item, (dict, list)):
                    visit(item)

    visit([checkpoint, operations, events])
    releases = store.all('release')
    return {'scope': store.scope, 'task': checkpoint, 'operations': operations,
            'agent_sessions': sessions, 'events': events,
            'referenced_records': [{'kind': kind, 'record': value} for (kind, _), value in found.items()],
            'skills': [{key: detail[key] for key in ('name', 'version', 'digest')}
                       for name, version in checkpoint['skill_versions'].items()
                       for detail in [registry.describe(name, version, history=True)]],
            'releases': releases, 'evaluation_refs': [row['evaluation'] for row in releases],
            'installation': installation_identity(),
            'boundary': 'Only this Store scope; artifact metadata, no image bytes or other-role reports'}


def export_method(store, registry, skill, task_ids, *, version=None):
    """Export original method runs and registration, without importing or running code.

    Task selection is explicit. IDs, arguments, versions and call references come
    from their owners; this function does not manufacture a passed criterion.
    """
    tasks = [store.get('task_checkpoint', 'task-checkpoint_' + task) for task in dict.fromkeys(task_ids)]
    versions = {task['skill_versions'].get(skill) for task in tasks}
    if version is None:
        if len(versions) != 1 or None in versions:
            raise HarnessError('skill_version_mismatch', 'Select tasks with the same fixed method version')
        version = versions.pop()
    registration = registry.describe(skill, version, history=True)
    if registration['kind'] != 'code':
        raise HarnessError('invalid_skill', 'A method report requires a callable code skill')
    runs = []
    for task in tasks:
        if task['skill_versions'].get(skill) != version:
            raise HarnessError('skill_version_mismatch', 'Task method version differs from selected registration')
        for identity in task['operation_ids']:
            operation = store.get('operation', identity)
            if (operation.get('skill_name'), operation.get('skill_version')) != (skill, version):
                continue
            calls = []
            for row in store.db.execute("SELECT body FROM events WHERE kind='tool.started' ORDER BY seq"):
                event = json.loads(row['body'])
                if event.get('task_id') == task['task_id'] and event.get('arguments', {}).get('request_id') == operation['args']['request_id']:
                    calls.append(event['id'])
            runs.append({'operation': operation, 'source': {'scope': store.scope,
                         'task_id': task['task_id'], 'checkpoint_id': 'task-checkpoint_' + task['task_id'],
                         'call_ids': calls, 'installation_digest': task.get('execution', {}).get('installation_digest')}})
    return {'registration': registration, 'runs': runs, 'remaining': [],
            'meaning': 'Original registration/run records; domain criteria and caller whole-goal contract determine readiness'}
