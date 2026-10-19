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
            for key, item in value.items():
                kind = {'observation_id': 'observation', 'camera_observation_id': 'camera_observation',
                        'plan_id': 'plan', 'depth_id': 'depth'}.get(key)
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
