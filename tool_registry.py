"""把外部服务器声明的工具交给模型；每轮、每个工作目录单独连接。"""
import json
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

from workspace_tools import filename, toolbox


DEFAULT_CONFIG = Path(__file__).with_name('tools.json')


class ToolRegistry:
    def __init__(self):
        self.schemas = {}
        self.functions = {}
        self.read_only = {}

    def register(self, tool, client, read_only):
        name = tool['name']
        if name in self.functions or name in ('delegate', 'done', 'command'):
            raise ValueError(f'工具名重复或保留：{name}')
        self.schemas[name] = dict(type='function', function=dict(
            name=name, description=tool.get('description', ''), parameters=tool['inputSchema']))
        invoke = lambda **arguments: client.call(name, arguments)
        self.functions[name] = invoke
        if read_only:
            self.read_only[name] = invoke


@contextmanager
def connect_tools(config, workspace, state, *, provider=None):
    from external_tools import MCPClient
    config = Path(config).resolve()
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    specs = json.loads(config.read_text(encoding='utf-8'))['mcpServers']
    registry = ToolRegistry()

    def resolve(value):
        if '{workspace}' in value:
            return value.replace('{workspace}', str(workspace))
        return str(config.parent / value) if value.startswith('./') else value

    with ExitStack() as stack:
        for server, spec in specs.items():
            if provider is not None and server != provider:
                continue
            if spec.get('online') and not state.get('online'):
                continue
            argv = [resolve(value) for value in [spec['command'], *spec.get('args', [])]]
            remaining = state['deadline'] - time.time()
            if remaining <= 0:
                raise ValueError('连接外部工具时已达本轮时限')
            try:
                client = stack.enter_context(MCPClient(argv, cwd=workspace, timeout=remaining))
                available = {tool['name']: tool for tool in client.list_tools()}
            except FileNotFoundError as error:
                raise ValueError(f'找不到外部工具 {server}。请先运行 ./start.sh --setup。') from error
            for name in spec['allow']:
                if name not in available:
                    raise ValueError(f'{server} 未提供配置中的工具 {name}')
                registry.register(available[name], client, name in spec.get('readOnly', []))
        # 固定词数统计继续调用系统 wc；模型不能拼装命令。
        registry.functions['command'] = toolbox(state, None, False)['command']
        yield registry


def artifacts(folder):
    """浏览器下载会话目录内的文本产物。保留旧版单文件下载接口。"""
    files = {}
    for path in sorted(folder.iterdir()):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            continue
        try:
            name = filename(path.name)
            files[name] = path.read_text(encoding='utf-8')
        except (UnicodeDecodeError, ValueError):
            continue
    return files
