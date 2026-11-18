"""工具执行器：文件只来自本次任务。工具不认识 LLM，也不决定下一步。"""
import re
import subprocess
import time


def filename(name):
    if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9_-][a-zA-Z0-9_.-]{0,99}', name):
        raise ValueError('只允许简单文件名，不允许路径')
    return name


def toolbox(state, approve, child):
    files = state['files']

    def read(name):
        return files[filename(name)]

    def write(name, text):
        filename(name)
        if not isinstance(text, str) or not approve(name, text):
            raise ValueError('文本无效或用户拒绝写入')
        if time.time() >= state['deadline']:
            raise ValueError('审批期间已超时')
        files[name] = text
        return f'已写入 {name}'

    def command(name, file):
        if name != 'word_count':
            raise ValueError('只允许 word_count')
        # 文件内容作为标准输入，而不是命令；不给模型 Shell 或宿主文件路径。
        return subprocess.run(['/usr/bin/wc', '-w'], input=read(file), text=True, env={},
                              capture_output=True, check=True,
                              timeout=max(0, state['deadline'] - time.time())).stdout.strip()

    tools = {'list': lambda: '\n'.join(sorted(files)), 'read': read}
    if not child:
        tools.update(write=write, command=command)
    return tools


def call_tool(tools, action):
    args = action.copy()
    tool = tools[args.pop('op')]
    return tool(**args)
