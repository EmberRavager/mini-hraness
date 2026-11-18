"""旧检查点和离线示例的虚拟工具，以及共用的文件名校验和调用入口。"""
import re
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory


def _web_search(query, state):
    from tool_registry import DEFAULT_CONFIG, connect_tools

    if not isinstance(query, str) or not 1 <= len(query) <= 256 or not query.isprintable() or not query.strip():
        raise ValueError('网页搜索词须为 1 至 256 个可打印字符，且不能全为空格')
    if time.time() >= state['deadline']:
        raise ValueError('网页搜索超时')
    # 旧调用只需搜索服务，使用独立临时目录，不向它暴露会话文件。
    with TemporaryDirectory() as directory:
        with connect_tools(DEFAULT_CONFIG, Path(directory), state, provider='search') as tools:
            return tools.functions['search'](query=query.strip(), limit=5)


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
        if state.get('online') is True:
            tools['web_search'] = lambda query: _web_search(query, state)
    return tools


def call_tool(tools, action):
    args = action.copy()
    tool = tools[args.pop('op')]
    return tool(**args)
