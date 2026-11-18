"""会话层：终端和 HTTP 共用，自己不依赖任何一种界面。"""
import json
import re
from contextlib import nullcontext
from pathlib import Path
from threading import Lock
from uuid import UUID
from weakref import WeakValueDictionary

from harness import publish, run, start
from ollama_chat import loaded_context_length
from workspace_tools import filename
from tool_registry import connect_tools


class NeedApproval(Exception):
    """退出本次 HTTP 请求，保留当前 run 的 pending 动作。"""


def child(parent, name):
    path = parent / name
    if path.is_symlink():
        raise ValueError('工作区不支持符号链接')
    return path


def put(path, text):
    from tempfile import NamedTemporaryFile
    with NamedTemporaryFile('w', dir=path.parent, encoding='utf-8', delete=False) as tmp:
        tmp.write(text)
    Path(tmp.name).replace(path)


def recent_sessions(base):
    entries = []
    for folder in base.iterdir() if base.exists() else ():
        if folder.is_symlink() or not folder.is_dir():
            continue
        try:
            if str(UUID(folder.name)) != folder.name:
                continue
            checkpoint = child(folder, 'session.json')
            data = json.loads(checkpoint.read_text())
            title = data['turns'][0][0] if data['turns'] else data['active']['stack'][0]['task']
            entries.append((checkpoint.stat().st_mtime_ns, dict(id=folder.name, title=title[:70])))
        except (FileNotFoundError, ValueError, KeyError, TypeError, IndexError):
            continue
    return [entry for _, entry in sorted(entries, key=lambda item: item[0], reverse=True)]


def turn_usage(state):
    """模型事件按 call_id 更新，审批继续时可从同一批事件重算，不会重复累计。"""
    responses = {event['call_id']: event['response'] for event in state.get('events', [])
                 if event.get('kind') == 'model' and isinstance(event.get('response'), dict)}
    calls = state.get('call_count', len(responses))
    saved = state.get('usage')
    if saved is not None and saved['calls'] == calls:
        return saved
    last = responses.get(calls, {})

    def count(response, field):
        value = response.get(field)
        return value if type(value) is int and value >= 0 else None

    def total(field):
        values = [count(response, field) for response in responses.values()]
        if values and len(values) == calls and None not in values:
            return sum(values)
        return None

    output = total('eval_count')
    duration = total('eval_duration')
    elapsed = total('total_duration')
    model = last.get('model') or (state.get('model') if 'message' in last else None)
    capacity = loaded_context_length(model) if isinstance(model, str) and model else None
    return dict(input_tokens=total('prompt_eval_count'), output_tokens=output,
                output_tps=output * 1_000_000_000 / duration if duration and output is not None else None,
                last_input_tokens=count(last, 'prompt_eval_count'), context_length=capacity,
                elapsed_ms=elapsed / 1_000_000 if elapsed is not None else None, calls=calls)


def action_summary(events):
    # 历史回复只存动作标签，不重复存文件正文、提示词和模型报文。
    result = []
    for event in events:
        if event.get('kind') != 'tool':
            continue
        args = event.get('request') or {}
        labels = {key: str(args[key])[:72] for key in ('name', 'path', 'query', 'file', 'task') if key in args}
        result.append(dict(kind='tool', name=event['name'], role=event['role'],
                           status=event['status'], request=labels))
    return result


_write_locks = WeakValueDictionary()
_registry_lock = Lock()


def _session_write_lock(folder):
    # 仅持有者和等待者强引用这把锁；全部离开后，登记项自动回收。
    key = folder.resolve()
    with _registry_lock:
        return _write_locks.setdefault(key, Lock())


def dispatch(root, route, body, model, *, model_name=None, emit=None, tool_config=None):
    workspace = body.get('workspace', 'default')
    if not isinstance(workspace, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,60}', workspace):
        raise ValueError('工作区名称只允许字母、数字、下划线和短横线（最多 60 字符）')
    base = child(child(root, workspace), '.mini-harness')
    if route == '/sessions':
        return dict(sessions=recent_sessions(base))
    sid = str(UUID(str(body.get('session'))))
    folder = child(base, sid)
    # 锁覆盖从读取检查点到提交的整个写请求。只读请求可直接查看已提交的快照。
    with _session_write_lock(folder) if route in ('/chat', '/approve') else nullcontext():
        return _dispatch_session(folder, route, body, model, model_name, emit, tool_config)


def _dispatch_session(folder, route, body, model, model_name, emit, tool_config):
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    checkpoint = child(folder, 'session.json')
    session = json.loads(checkpoint.read_text()) if checkpoint.exists() else dict(turns=[], files={}, active=None)
    usage_history = session.setdefault('turn_usage', [])
    usage_history.extend([None] * (len(session['turns']) - len(usage_history)))
    del usage_history[len(session['turns']):]
    action_history = session.setdefault('turn_actions', [])
    action_history.extend([None] * (len(session['turns']) - len(action_history)))
    state = session['active']
    def snapshot():
        view = dict(turns=session['turns'], turn_usage=usage_history, turn_actions=action_history, files=session['files'],
                    trace=state['trace'] if state else session.get('trace', []),
                    events=(state or session).get('events', []))
        if state:
            action = state['stack'][-1]['pending']
            view.update(usage=state.get('usage'), approval=dict(name=action['name'], text=action['text']),
                        pendingMessage=state['stack'][0]['task'])
        return view
    if route == '/session':
        return snapshot()
    if route == '/chat' and not state:
        message = body.get('message')
        if not isinstance(message, str) or not message.strip() or len(message) > 16000:
            raise ValueError('请输入 1 至 16000 字符')
        state = start(message)
        if tool_config:
            state['external_tools'] = True
        if not isinstance(body.get('online', False), bool):
            raise ValueError('online 必须是布尔值')
        state['online'] = body.get('online', False)
        if model_name is not None:
            state['model'] = model_name
        state['files'] = session['files'].copy()
        session['active'] = state
    elif route != '/approve' or not state or not isinstance(body.get('allow'), bool):
        raise ValueError('当前会话状态与操作不符，请刷新')
    decision = body.get('allow') if route == '/approve' else None
    def approve(name, text):
        nonlocal decision
        if decision is None:
            if emit and state['stack'][-1].get('tool_id'):
                publish(state, emit, state['stack'][-1]['tool_id'], status='waiting')
            raise NeedApproval()
        allowed, decision = decision, None
        return allowed
    def decide(task, history, actions, timeout):
        # 父助手接收最近六轮问答；子助手只看到委派给自己的任务。
        if len(state['stack']) == 1 and session['turns']:
            task = dict(message=task, previous_turns=[[u[:2000], a[:2000]] for u, a in session['turns'][-6:]])
        if model_name is not None:
            if emit is not None:
                primary = len(state['stack']) == 1
                selected = state.get('model', model_name)
                number = state['stack'][-1]['call_id']
                if primary:
                    emit(dict(type='reset'))
                def started(request):
                    publish(state, emit, f'm{number}', kind='model', role='主' if primary else '子',
                            call_id=number, name=selected, status='running', request=request)
                return model(selected, task, history, actions, timeout, on_request=started,
                             on_thinking=lambda: publish(state, emit, f'm{number}', phase='thinking'),
                             on_text=(lambda text: emit(dict(type='delta', text=text))) if primary else None)
            return model(state.get('model', model_name), task, history, actions, timeout)
        return model(task, history, actions, timeout)
    try:
        directory = child(folder, 'files')
        use_external = state.get('external_tools', False)
        if use_external and not tool_config:
            raise ValueError('恢复此任务需要启用外部工具配置')
        with connect_tools(tool_config, directory, state) if use_external else nullcontext() as external:
            run(state, decide, approve, lambda _: None, emit=emit, external=external, directory=directory)
    except NeedApproval:
        state['usage'] = turn_usage(state)
        put(checkpoint, json.dumps(session, ensure_ascii=False))
        return snapshot()
    # 虚拟文件仅在本轮结束后导出到会话专属目录，不覆盖已有项目。
    outputs = child(folder, 'files')
    for name, content in state['files'].items():
        if not state.get('external_tools') and session['files'].get(name) != content:
            outputs.mkdir(exist_ok=True)
            put(child(outputs, filename(name)), content)
    usage = turn_usage(state)
    session['turns'].append([state['stack'][0]['task'], state['result']])
    usage_history.append(usage)
    action_history.append(action_summary(state['events']))
    session.update(files=state['files'], active=None, trace=state['trace'], events=state['events'])
    put(checkpoint, json.dumps(session, ensure_ascii=False))
    return dict(reply=state['result'], status=state['status'], files=state['files'],
                trace=state['trace'], events=state['events'], usage=usage)
