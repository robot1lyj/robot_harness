"""Installed-protocol double: never runs a model or accesses task truth."""
import json
import os
import sys

threads = {}
sequence = 1000
mode = os.environ.get('C01_MODE', 'normal')
log = os.environ['C01_TRACE']


def send(message):
    print(json.dumps(message), flush=True)


def receive():
    line = sys.stdin.readline()
    if not line:
        raise EOFError
    message = json.loads(line)
    with open(log, 'a') as handle:
        handle.write(json.dumps(message)+'\n')
    return message


def request_tool(thread, turn, name, arguments):
    global sequence
    sequence += 1
    send({'id': sequence, 'method':'item/tool/call','params':{
        'threadId':thread,'turnId':turn,'callId':str(sequence),'tool':name,'arguments':arguments}})
    message = receive()
    if message.get('method') == 'turn/interrupt':
        send({'id':message['id'],'result':{}})
        send({'method':'turn/completed','params':{'threadId':thread,'turn':{'id':turn,'status':'interrupted','error':None}}})
        return None
    return json.loads(message['result']['contentItems'][0]['text'])


try:
    while True:
        message = receive()
        method, identity = message.get('method'), message.get('id')
        params = message.get('params', {})
        if method == 'initialized':
            continue
        if method == 'initialize':
            send({'id':identity,'result':{'userAgent':'fixture/0.160.0'}})
        elif method == 'thread/start':
            thread = 'native-' + str(len(threads)+1)
            threads[thread] = {'turns':0,'view':{},'usage':0}
            send({'id':identity,'result':{'thread':{'id':thread}}})
        elif method == 'thread/unsubscribe':
            send({'id':identity,'result':{'status':'unsubscribed'}})
        elif method == 'turn/interrupt':
            send({'id':identity,'result':{}})
        elif method == 'turn/start':
            thread = params['threadId']
            entry = threads[thread]
            entry['turns'] += 1
            turn = thread + '-turn-' + str(entry['turns'])
            send({'method':'turn/started','params':{'threadId':thread,'turn':{'id':turn}}})
            send({'id':identity,'result':{'turn':{'id':turn,'status':'inProgress'}}})
            view = json.loads(params['input'][0]['text'].split('\n',1)[1])
            entry['view'].update(view['blocks'])
            view = entry['view']
            if 'required_details' in view:
                details = {}
                for reference in view['required_details']['read_first']:
                    response = request_tool(thread, turn, reference['tool'], reference['arguments'])
                    assert response and response['ok'], response
                    details[reference['tool']] = response['result']
                checkpoint = details['task_checkpoint_get']
                assert details['workspace_read']['workspace']['constraints'] == checkpoint['constraints']
                view = {'task': {'execution': checkpoint['execution']},
                        'results': {'operations': checkpoint['operations'], 'allowed_recovery': []}}
            task = view['task']
            if mode == 'readonly':
                response = request_tool(thread,turn,'task_context',{})
            elif view['results']['allowed_recovery']:
                response = request_tool(thread,turn,'task_recover',{
                    'request_id':turn, 'task_epoch':task['execution']['epoch'],
                    **view['results']['allowed_recovery'][0]})
            elif not view['results']['operations']:
                response = request_tool(thread,turn,'task_run',{
                    'request_id':turn,'task_epoch':task['execution']['epoch']})
            else:
                response = request_tool(thread,turn,'task_finish',{
                    'outcome':'succeeded','summary':'完整抓放目标已验证（协议替身）',
                    'task_epoch':task['execution']['epoch'],
                    'operation_ids':[view['results']['operations'][-1]['id']]})
                if response and response['ok']:
                    request_tool(thread,turn,'task_archive',{})
            if response is None:
                continue
            entry['usage'] += 100
            usage = {'inputTokens':entry['usage'],'cachedInputTokens':entry['usage']//2,
                     'outputTokens':entry['usage']//10, 'reasoningOutputTokens':entry['usage']//20,
                     'cacheWriteInputTokens':0, 'totalTokens':entry['usage']*11//10}
            send({'method':'thread/tokenUsage/updated','params':{
                'threadId':thread,'turnId':turn,'tokenUsage':{
                    'total':usage,'last':{**usage,'inputTokens':100},'modelContextWindow':10000}}})
            send({'method':'turn/completed','params':{'threadId':thread,'turn':{
                'id':turn,'status':'completed','error':None}}})
        else:
            send({'id':identity,'error':{'code':-32601,'message':'unsupported'}})
except EOFError:
    pass
