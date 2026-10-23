
import pytest
from robot_rsi.coding_host import local_file
from robot_rsi.coding_install import CandidateInstall, boundary, install, resume_host
from robot_rsi.contracts import Profile, TaskBegin, TaskLimits
from robot_rsi.storage import Store
from robot_rsi.task_memory import TaskMemory


def test_affected_task_boundary_reads_original_owner_without_runtime(tmp_path):
    p=Profile(state_root=str(tmp_path/'state'))
    (tmp_path/'profile.json').write_text(p.model_dump_json())
    store=Store(p)
    memory=TaskMemory(store, skill_versions={}, capabilities=[])
    memory.begin(TaskBegin(task_id='old',goal='whole goal'),TaskLimits())
    store.close()
    request=CandidateInstall(wheels=[{'path':'candidate.whl','sha256':'0'*64}],reason='method gap',tasks=[{'profile':'profile.json','task_id':'old'}])
    with pytest.raises(ValueError,match='Finish/archive'):
        boundary(tmp_path,request,local_file)
    store=Store(p);task=store.get('task_checkpoint','task-checkpoint_old');task['archived']=True
    task['operation_ids']=['uncertain'];store.put('operation','uncertain',{'status':'unknown'})
    store.put('task_checkpoint','task-checkpoint_old',task);store.close()
    assert boundary(tmp_path,request,local_file)[0]['operation_ids']==['uncertain']
    store=Store(p);task['archived']=False;task['execution']['status']='cancelled';store.put('task_checkpoint','task-checkpoint_old',task);store.close()
    assert boundary(tmp_path,request,local_file)[0]['archived'] is False
    store=Store(p);store.put('operation','uncertain',{'status':'running'});store.close()
    with pytest.raises(ValueError,match='Wait/cancel'):
        boundary(tmp_path,request,local_file)


def test_candidate_selection_rejects_changes_and_no_progress(tmp_path,monkeypatch):
    wheel=tmp_path/'candidate.whl';wheel.write_bytes(b'changed')
    request=CandidateInstall(wheels=[{'path':'candidate.whl','sha256':'0'*64}],reason='gap')
    with pytest.raises(ValueError,match='SHA256'):
        install(tmp_path/'host',tmp_path,{},request,local_file)
    from robot_rsi.host_boundary import sha256
    request.wheels[0].sha256=sha256(wheel)
    monkeypatch.setattr('robot_rsi.coding_install.verify',lambda _: {'local_wheels':[{'sha256':request.wheels[0].sha256}]})
    with pytest.raises(ValueError,match='unchanged'):
        install(tmp_path/'host',tmp_path,{},request,local_file)


@pytest.mark.parametrize('changed',['project','core','dependencies'])
def test_resume_cannot_adopt_another_project_or_base(tmp_path,monkeypatch,changed):
    origin=tmp_path/'host';active=tmp_path/'coding-candidate-123'
    project=tmp_path/'project'
    base={'core':{'sha256':'fixed'},'dependencies_sha256':'base'}
    chosen={**base,'coding_project':str(project),'coding_origin':str(origin)}
    if changed=='project':chosen['coding_project']='other'
    elif changed=='core':chosen['core']={'sha256':'changed'}
    else:chosen['dependencies_sha256']='changed'
    monkeypatch.setattr('robot_rsi.coding_install.verify',lambda p:base if p==origin else chosen)
    with pytest.raises(ValueError,match='approved core/dependencies'):
        resume_host(origin,project,{'host':str(active),'origin_host':str(origin)})


def test_failed_candidate_can_retry_only_on_explicit_resume(tmp_path,monkeypatch):
    from robot_rsi.host_boundary import sha256
    wheel=tmp_path/'candidate.whl';wheel.write_bytes(b'fixed artifact')
    request=CandidateInstall(wheels=[{'path':'candidate.whl','sha256':sha256(wheel)}],reason='dependency condition changed')
    monkeypatch.setattr('robot_rsi.coding_install.verify',lambda _: {'local_wheels':[]})
    calls=[]
    def unavailable(*args,**kwargs):
        calls.append(True)
        raise ValueError('network unavailable')
    monkeypatch.setattr('robot_rsi.coding_install.update',unavailable)
    record={'candidate_installs':[{'fingerprint':[sha256(wheel)],'status':'failed'}]}
    with pytest.raises(ValueError,match='unchanged'):
        install(tmp_path/'host',tmp_path,record,request,local_file)
    record['candidate_retry']=True
    with pytest.raises(ValueError,match='network unavailable'):
        install(tmp_path/'host',tmp_path,record,request,local_file)
    with pytest.raises(ValueError,match='unchanged'):
        install(tmp_path/'host',tmp_path,record,request,local_file)
    assert calls==[True] and record['candidate_installs'][-1]['status']=='failed'


def test_no_candidate_adds_no_install_or_client_turn(tmp_path,monkeypatch):
    from robot_rsi import coding_host as host
    project=tmp_path/'project';(project/'.rsi/host-home').mkdir(parents=True)
    calls=[]
    monkeypatch.setattr(host,'command',lambda *_args,**_kwargs: [])
    def client(*args,**kwargs):
        calls.append(True)
        body=host.Handoff(kind='runtime',summary='Existing method completed',paths=[],validation=[],checkpoint='',commits=[],commit_message='',no_commit_reason=None)
        return body.model_dump_json(),False,0
    monkeypatch.setattr(host,'run_client',client)
    def forbidden(*_args,**_kwargs):raise AssertionError('No candidate must not install')
    monkeypatch.setattr(host,'install_candidate',forbidden)
    assert host.run(tmp_path/'host',project,['ordinary goal'])==0
    assert calls==[True]


def test_install_failure_returns_to_same_session_for_local_correction(tmp_path,monkeypatch):
    from robot_rsi import coding_host as host
    project=tmp_path/'project';(project/'.rsi/host-home').mkdir(parents=True)
    monkeypatch.setattr(host,'command',lambda *_args,**_kwargs: [])
    calls=[]
    session='00000000-0000-0000-0000-000000000042'
    def client(root,project,options,prompt,schema,record,**kwargs):
        calls.append({'session':kwargs.get('session'),'prompt':prompt})
        record['session_id']=session
        candidate=None if len(calls)==3 else {'wheels':[{'path':'candidate.whl','sha256':str(len(calls))*64}],'reason':'changed local metadata','tasks':[]}
        body=host.Handoff(kind='runtime',summary='whole goal',paths=[],validation=[],checkpoint='Recoverable changed candidate',commits=[],commit_message='',no_commit_reason=None,candidate_install=candidate)
        return body.model_dump_json(),False,0
    monkeypatch.setattr(host,'run_client',client)
    def selected(root,project,record,request,*args,**kwargs):
        if not record.get('candidate_installs'):
            record['candidate_installs']=[{'status':'failed','reason':'local dependency conflicts with fixed base'}]
            raise ValueError('Choose a compatible local dependency; keep base')
        record['candidate_installs'].append({'status':'installed'})
        return root,{'status':'installed','host':str(root)}
    monkeypatch.setattr(host,'install_candidate',selected)
    assert host.run(tmp_path/'host',project,['complete original goal'])==0
    assert len(calls)==3 and calls[1]['session']==calls[2]['session']==session
    assert 'correct local code/dependency metadata' in calls[1]['prompt']
    assert 'installed candidate' in calls[2]['prompt']
