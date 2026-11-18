"""Synchronous, single-owner stdio MCP client for a caller-supplied executable (POSIX).

Use one context per execution. timeout is the total budget from process launch;
max_bytes bounds each outgoing message and all incoming frames of one operation.
"""
import json
import math
import os
import selectors
import signal
import subprocess
import time
from contextlib import contextmanager


_VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')


class MCPError(ValueError):
    """MCP negotiation, protocol, or tool failure."""


class _RemoteError(MCPError):
    """A complete error response leaves the transport usable."""


class MCPClient:
    def __init__(self, argv, *, cwd=None, timeout=10, max_bytes=256 * 1024):
        if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(s, str) for s in argv):
            raise ValueError('MCP argv 须为非空字符串参数列表')
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('MCP timeout 须为有限正数')
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError('MCP max_bytes 须为正整数')
        self._argv, self._cwd = tuple(argv), cwd
        self._timeout, self._limit = timeout, max_bytes
        self._proc = None
        self._id = 0
        self._buffer = bytearray()

    def __enter__(self):
        if self._proc is not None:
            raise MCPError('MCP 客户端已启动')
        self._deadline = time.monotonic() + self._timeout
        self._proc = subprocess.Popen(self._argv, cwd=self._cwd, env={}, stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
                                      start_new_session=True)
        try:
            os.set_blocking(self._proc.stdin.fileno(), False)
            os.set_blocking(self._proc.stdout.fileno(), False)
            with self._operation():
                result = self._request('initialize', dict(protocolVersion=_VERSIONS[0], capabilities={},
                                       clientInfo=dict(name='mini-harness', version='1')))
                if result.get('protocolVersion') not in _VERSIONS:
                    raise MCPError('MCP 协议版本不兼容')
                capabilities = result.get('capabilities')
                if not isinstance(capabilities, dict) or not isinstance(capabilities.get('tools'), dict):
                    raise MCPError('MCP 服务未声明 tools 能力')
                self._send(dict(method='notifications/initialized'))
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def close(self):
        """Close stdin, allow a brief graceful exit, then terminate a stuck server."""
        proc, self._proc = self._proc, None
        self._buffer.clear()
        if proc is None:
            return
        proc.stdin.close()
        proc.stdout.close()
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                proc.wait(timeout=0.2)
                return
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, sig)
                except ProcessLookupError:
                    pass
        proc.wait()

    @contextmanager
    def _operation(self):
        if self._proc is None:
            raise MCPError('请在 MCPClient 的 with 作用域内操作')
        self._budget = self._limit
        try:
            yield
            self._remaining()
        except _RemoteError:
            raise
        except MCPError:
            self.close()
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as error:
            self.close()
            if isinstance(error, TimeoutError):
                raise
            raise MCPError('MCP 管道中断或 JSON 响应无效') from error

    def _remaining(self):
        seconds = self._deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError('MCP 执行超时')
        return seconds

    def _wait(self, stream, event):
        with selectors.DefaultSelector() as selector:
            selector.register(stream, event)
            if not selector.select(self._remaining()):
                raise TimeoutError('MCP 执行超时')

    def _send(self, message):
        raw = json.dumps({'jsonrpc': '2.0', **message}, ensure_ascii=False,
                         allow_nan=False, separators=(',', ':')).encode() + b'\n'
        if len(raw) > self._limit:
            raise MCPError('MCP 请求超过大小上限')
        pending = memoryview(raw)
        while pending:
            self._wait(self._proc.stdin, selectors.EVENT_WRITE)
            pending = pending[os.write(self._proc.stdin.fileno(), pending):]

    def _receive(self):
        self._remaining()
        start = 0
        while (end := self._buffer.find(b'\n', start)) < 0:
            start = len(self._buffer)
            if start >= self._budget:
                raise MCPError('MCP 响应超过大小上限')
            self._wait(self._proc.stdout, selectors.EVENT_READ)
            chunk = os.read(self._proc.stdout.fileno(), min(65536, self._budget - start))
            if not chunk:
                raise MCPError('MCP 服务提前关闭了输出')
            self._buffer.extend(chunk)
        if end + 1 > self._budget:
            raise MCPError('MCP 响应超过大小上限')
        self._budget -= end + 1
        try:
            message = json.loads(self._buffer[:end].decode('utf-8'))
        except (ValueError, RecursionError) as error:
            raise MCPError('MCP JSON 响应无效') from error
        del self._buffer[:end + 1]
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
            raise MCPError('MCP JSON-RPC 消息格式无效')
        return message

    def _request(self, method, params):
        self._id += 1
        self._send(dict(id=self._id, method=method, params=params))
        while True:
            msg = self._receive()
            if 'method' in msg:
                if not isinstance(msg['method'], str) or 'result' in msg or 'error' in msg:
                    raise MCPError('MCP 请求或通知格式无效')
                if 'id' in msg:
                    if type(msg['id']) not in (str, int):
                        raise MCPError('MCP 服务请求 id 无效')
                    reply = (dict(result={}) if msg['method'] == 'ping' else
                             dict(error=dict(code=-32601, message='Method not found')))
                    self._send(dict(id=msg['id'], **reply))
                continue
            if type(msg.get('id')) is not int or msg['id'] != self._id:
                raise MCPError('MCP 响应 id 与当前请求不符')
            if 'error' in msg and 'result' not in msg:
                error = msg['error']
                if (isinstance(error, dict) and type(error.get('code')) is int
                        and isinstance(error.get('message'), str)):
                    raise _RemoteError(f"MCP {method} 失败（{error['code']}）：{error['message']}")
            if 'error' in msg or not isinstance(msg.get('result'), dict):
                raise MCPError('MCP 响应缺少有效的 result 或 error')
            return msg['result']

    def list_tools(self):
        """Return original tool descriptors, including inputSchema, across all pages."""
        tools, cursors = [], set()
        params = {}
        with self._operation():
            while True:
                page = self._request('tools/list', params)
                entries = page.get('tools')
                if not isinstance(entries, list) or not all(
                    isinstance(tool, dict) and isinstance(tool.get('name'), str) and tool['name']
                    and isinstance(tool.get('inputSchema'), dict) for tool in entries
                ):
                    raise MCPError('MCP tools/list 返回了无效的工具定义')
                tools.extend(entries)
                cursor = page.get('nextCursor')
                if cursor is None:
                    return tools
                if not isinstance(cursor, str) or cursor in cursors:
                    raise MCPError('MCP tools/list 返回了无效或重复的分页游标')
                cursors.add(cursor)
                params = dict(cursor=cursor)

    def call(self, name, arguments):
        """Return joined text blocks, or structuredContent as JSON; raise on isError."""
        if not isinstance(name, str) or not name or not isinstance(arguments, dict):
            raise ValueError('MCP 调用需要工具名和参数对象')
        with self._operation():
            result = self._request('tools/call', dict(name=name, arguments=arguments))
            content = result.get('content', [])
            failed = result.get('isError', False)
            if type(failed) is not bool or not isinstance(content, list):
                raise MCPError('MCP tools/call 结果格式无效')
            parts = []
            for block in content:
                if not isinstance(block, dict) or not isinstance(block.get('type'), str):
                    raise MCPError('MCP 内容块格式无效')
                if block['type'] == 'text':
                    if not isinstance(block.get('text'), str):
                        raise MCPError('MCP 文本内容格式无效')
                    parts.append(block['text'])
            if parts:
                text = '\n'.join(parts)
            elif isinstance(result.get('structuredContent'), dict):
                text = json.dumps(result['structuredContent'], ensure_ascii=False, separators=(',', ':'))
            elif content:
                raise _RemoteError(f'MCP 工具 {name} 未返回可回填的文本或结构化结果')
            elif 'content' not in result:
                raise MCPError('MCP tools/call 缺少结果内容')
            else:
                text = ''
            if failed:
                raise _RemoteError(f'MCP 工具 {name} 执行失败：{text}')
            return text
