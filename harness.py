"""一个循环就是一个 harness；模型、人工审批和存档由调用方传入。"""
from subprocess import SubprocessError
import time

from workspace_tools import call_tool, filename, toolbox


def frame(task):
    return {'task': task, 'history': [], 'pending': None}


def start(task, steps=20, seconds=120, expect=None):
    # State & Memory：只有本次运行共享文件和预算，每个助手各有自己的历史。
    return dict(stack=[frame(task)], files={}, trace=[], delegates_left=6, steps=steps, deadline=time.time() + seconds,
                expect=filename(expect) if expect else None, status='running', result='')


def run(state, model, approve, save):
    stack, files = state['stack'], state['files']
    while state['status'] == 'running':
        current = stack[-1]
        child = len(stack) > 1
        tools = toolbox(state, approve, child)
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
            save(state)
            try:
                proposal = model(current['task'], current['history'][-6:], actions, remaining)
                current['pending'] = proposal if isinstance(proposal, dict) else {}
            except ValueError:
                current['pending'] = {}  # 无效 JSON 也消耗一步，下一轮允许模型修正。
        else:
            # 动作已在上一轮落盘。中断后继续处理它，而不是让模型重新猜。
            operation = action.get('op')
            role = '子' if child else '主'
            print(f'[{role} Agent] {operation!r}', flush=True)
            try:
                if operation not in actions:
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
                result = f'失败：{error}'
            state['trace'].append([role, operation, result[:100]])
            current['history'].append(dict(action=current['pending'], result=result))
            current['pending'] = None
        save(state)
    return state
