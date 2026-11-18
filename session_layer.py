"""会话层：终端和 HTTP 共用，自己不依赖任何一种界面。"""
import json
import re
from pathlib import Path
from uuid import UUID

from harness import run, start
from workspace_tools import filename


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


def dispatch(root, route, body, model, *, model_name=None):
    workspace = body.get('workspace', 'default')
    if not isinstance(workspace, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,60}', workspace):
        raise ValueError('工作区名称只允许字母、数字、下划线和短横线（最多 60 字符）')
    base = child(child(root, workspace), '.mini-harness')
    if route == '/sessions':
        return dict(sessions=recent_sessions(base))
    sid = str(UUID(str(body.get('session'))))
    folder = child(base, sid)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    checkpoint = child(folder, 'session.json')
    session = json.loads(checkpoint.read_text()) if checkpoint.exists() else dict(turns=[], files={}, active=None)
    state = session['active']
    def snapshot():
        view = dict(turns=session['turns'], files=session['files'],
                    trace=state['trace'] if state else session.get('trace', []))
        if state:
            action = state['stack'][-1]['pending']
            view.update(approval=dict(name=action['name'], text=action['text']),
                        pendingMessage=state['stack'][0]['task'])
        return view
    if route == '/session':
        return snapshot()
    if route == '/chat' and not state:
        message = body.get('message')
        if not isinstance(message, str) or not message.strip() or len(message) > 16000:
            raise ValueError('请输入 1 至 16000 字符')
        state = start(message)
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
            raise NeedApproval()
        allowed, decision = decision, None
        return allowed
    def decide(task, history, actions, timeout):
        # 父助手接收最近六轮问答；子助手只看到委派给自己的任务。
        if len(state['stack']) == 1 and session['turns']:
            task = dict(message=task, previous_turns=[[u[:2000], a[:2000]] for u, a in session['turns'][-6:]])
        if model_name is not None:
            return model(state.get('model', model_name), task, history, actions, timeout)
        return model(task, history, actions, timeout)
    try:
        run(state, decide, approve, lambda _: None)
    except NeedApproval:
        put(checkpoint, json.dumps(session, ensure_ascii=False))
        return snapshot()
    # 虚拟文件仅在本轮结束后导出到会话专属目录，不覆盖已有项目。
    outputs = child(folder, 'files')
    for name, content in state['files'].items():
        if session['files'].get(name) != content:
            outputs.mkdir(exist_ok=True)
            put(child(outputs, filename(name)), content)
    session['turns'].append([state['stack'][0]['task'], state['result']])
    session.update(files=state['files'], active=None, trace=state['trace'])
    put(checkpoint, json.dumps(session, ensure_ascii=False))
    return dict(reply=state['result'], status=state['status'], files=state['files'], trace=state['trace'])
