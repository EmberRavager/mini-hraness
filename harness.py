"""一个循环就是一个 harness；模型、人工审批和存档由调用方传入。"""
from subprocess import SubprocessError
import time

from workspace_tools import call_tool, filename, toolbox
from trace_snapshot import snapshot


def frame(task):
    return {'task': task, 'history': [], 'pending': None}


def start(task, steps=20, seconds=120, expect=None):
    # State & Memory：只有本次运行共享文件和预算，每个助手各有自己的历史。
    return dict(stack=[frame(task)], files={}, trace=[], delegates_left=6, steps=steps, deadline=time.time() + seconds,
                expect=filename(expect) if expect else None, status='running', result='')


def accept_message(frame, message, state):
    """自然语言是答案；只有独立的 tool_calls 字段能触发工具。"""
    calls = message.get('tool_calls', [])
    if not isinstance(calls, list):
        raise ValueError('tool_calls 必须是列表')
    if not calls:
        text = message.get('content')
        if not isinstance(text, str) or not text.strip():
            raise ValueError('模型既没有回复也没有请求工具')
        frame['pending'] = dict(op='done', text=text)
        return
    if len(calls) - 1 > state['steps']:
        state.update(status='stopped', result='本批工具调用超过剩余步数')
        return
    pending = []
    for call in calls:
        function = call.get('function') if isinstance(call, dict) else None
        if not isinstance(function, dict) or not isinstance(function.get('name'), str):
            raise ValueError('工具调用缺少函数名')
        args = function.get('arguments')
        if not isinstance(args, dict) or 'op' in args:
            raise ValueError('工具参数格式无效')
        pending.append(dict(op=function['name'], **args))
    state['steps'] -= len(calls) - 1
    frame.update(pending=pending[0], queue=pending[1:], exchange=[message])


def publish(state, emit, event_id, **fields):
    events = state.setdefault('events', [])
    event = next((entry for entry in events if entry.get('id') == event_id), None)
    if event is None:
        event = {'id': event_id}
        events.append(event)
    event.update(fields)
    if emit:
        emit(dict(type='activity', event=event.copy()))


def run(state, model, approve, save, *, emit=None, external=None, directory=None):
    stack, files = state['stack'], state['files']
    events = state.setdefault('events', [])  # 旧检查点没有调用快照，不从摘要猜测参数。
    while state['status'] == 'running':
        current = stack[-1]
        child = len(stack) > 1
        role = '子' if child else '主'
        tools = (external.read_only if child else external.functions) if external else toolbox(state, approve, child)
        actions = [*tools, 'done']
        if not child and state['delegates_left'] > 0:
            actions.append('delegate')
        action = current['pending']
        remaining = state['deadline'] - time.time()
        if remaining <= 0 or (action is None and state['steps'] <= 0):
            state.update(status='stopped', result='时间或步数已用完')
        elif action is None:
            # 先扣共享预算；模型只看当前任务和最近 6 次反馈，不继承父对话。
            state['steps'] -= 1
            current['call_id'] = state['call_count'] = state.get('call_count', 0) + 1
            model_id = f"m{current['call_id']}"
            save(state)
            try:
                declarations = {name: external.schemas.get(name) for name in actions} if external else actions
                proposal = model(current['task'], current['history'][-6:], declarations, remaining)
                if hasattr(proposal, 'wire'):
                    publish(state, emit, model_id, kind='model', role=role, call_id=current['call_id'], **proposal.wire)
                    proposal = dict(proposal)
                elif emit:
                    publish(state, emit, model_id, kind='model', role=role, call_id=current['call_id'],
                            name=state.get('model','模型'), status='ok', response=snapshot(proposal))
                else:
                    current.pop('call_id', None)
                if isinstance(proposal, dict) and proposal.get('role') == 'assistant':
                    accept_message(current, proposal, state)
                else:  # 离线回放使用内部动作，不需要伪造网络协议。
                    current['pending'] = proposal if isinstance(proposal, dict) else {}
            except ValueError as error:
                current['history'].append(dict(action={}, result=f'模型响应无效：{error}'))
            except OSError as error:
                if not hasattr(error, 'wire'):
                    raise
                publish(state, emit, model_id, kind='model', role=role, call_id=current['call_id'], **error.wire)
                state.update(status='failed', result=str(error.wire['response']['error']))
        else:
            # 动作已在上一轮落盘。中断后继续处理它，而不是让模型重新猜。
            operation = action.get('op')
            outcome = 'ok'
            if emit and current.get('call_id') and (operation != 'done' or 'exchange' in current):
                if not current.get('tool_id'):
                    state['tool_count'] = state.get('tool_count', 0) + 1
                    current['tool_id'] = f"t{state['tool_count']}"
                publish(state, emit, current['tool_id'], kind='tool', role=role, call_id=current['call_id'],
                        name=operation, status='running',
                        request=snapshot({key:value for key,value in action.items() if key != 'op'}))
            print(f'[{role} Agent] {operation!r}', flush=True)
            try:
                if operation not in actions or (operation == 'done' and 'exchange' in current):
                    raise ValueError('动作不存在或无权限')
                if operation == 'delegate':
                    task = action['task']
                    if not isinstance(task, str) or not task.strip():
                        raise ValueError('缺少子任务')
                    state['delegates_left'] -= 1
                    stack.append(frame(task))  # 父动作保留；子任务 done 后再填结果。
                    state['trace'].append([role, operation, '开始子任务'])
                    save(state)
                    continue
                if operation in tools:
                    result = call_tool(tools, action)
                    if external and operation not in external.read_only:
                        from tool_registry import artifacts
                        files.update(artifacts(directory))
                else:
                    result = action['text']  # done：子助手向父级返回；主助手才结束整个任务。
                    if not isinstance(result, str):
                        raise ValueError('text 必须是字符串')
                    if child:
                        stack.pop()
                        current = stack[-1]
                    else:
                        expected = state['expect']
                        if expected and not files.get(expected, '').strip():
                            raise ValueError(f'验收未通过：缺少非空文件 {expected}')
                        state.update(status='done' if expected else 'review', result=result)
            except (ValueError, KeyError, TypeError, OSError, SubprocessError) as error:
                outcome = 'error'
                result = f'失败：{error}'
            public_result = snapshot(result)
            state['trace'].append([role, operation, public_result[:100]])
            # 子助手结束时 current 已回到父级，pending 正是原 delegate 调用。
            completed = current['pending']
            if current.get('call_id') and (completed.get('op') != 'done' or 'exchange' in current):
                tool_id = current.pop('tool_id', None) or f"completed-{len(events)}"
                publish(state, emit, tool_id, kind='tool', role='主' if len(stack) == 1 else '子',
                        call_id=current['call_id'], name=completed.get('op'), status=outcome,
                        request=snapshot({key: value for key, value in completed.items() if key != 'op'}),
                        response=public_result)
            record = dict(action=current['pending'], result=result)
            current['history'].append(record)
            current['pending'] = None
            if 'exchange' in current:
                # 整批执行完才把 assistant 和全部 tool 结果交回模型；中途审批可恢复。
                record['native'] = True
                current['exchange'].append(dict(role='tool', tool_name=record['action']['op'], content=result))
                if current['queue']:
                    current['pending'] = current['queue'].pop(0)
                else:
                    record['exchange'] = current.pop('exchange')
                    del current['queue']
        if state['status'] != 'running':
            for event in events:
                if event.get('status') in ('running', 'waiting'):
                    publish(state, emit, event['id'], status='error', response='运行已结束，未获得此步骤的完成结果')
        save(state)
    return state
