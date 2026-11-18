import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from external_tools import MCPClient, MCPError


# This executable speaks only over its pipes; all files it creates are test fixtures.
_SERVER = r'''
import json, os, signal, sys, time
from pathlib import Path

mode = sys.argv[1]
Path('pid').write_text(str(os.getpid()))

def read():
    return json.loads(sys.stdin.buffer.readline())

def send(message):
    print(json.dumps({'jsonrpc': '2.0', **message}), flush=True)

def result(req, **payload):
    send(dict(id=req['id'], result=payload))

def tool(name):
    return dict(name=name, description='demo', inputSchema={'type': 'object', '$defs': {'原样': {}}},
                annotations={'readOnlyHint': True})

init = read()
assert init['jsonrpc'] == '2.0' and init['method'] == 'initialize'
assert 'id' in init and init['params']['capabilities'] == {}
assert init['params']['clientInfo']['name']
if mode == 'stderr':
    sys.stderr.buffer.write(b'x' * 1000000)
    sys.stderr.flush()
if mode == 'init-hang':
    time.sleep(60)
result(init, protocolVersion='unsupported' if mode == 'version' else '2025-03-26',
       capabilities={} if mode == 'capability' else {'tools': {}},
       serverInfo={'name':'fixture', 'version':'1'})
ready = read()
assert ready == {'jsonrpc':'2.0', 'method':'notifications/initialized'}
if mode == 'blocked-write':
    time.sleep(60)
for line in sys.stdin.buffer:
    req = json.loads(line)
    op = req['method']
    if mode == 'hang':
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(60)
    if mode == 'exit':
        sys.exit(3)
    if mode == 'partial':
        sys.stdout.buffer.write(b'{"jsonrpc":"2.0"')
        sys.stdout.flush()
        sys.exit()
    if mode == 'huge':
        sys.stdout.buffer.write(b'x' * 90000)
        sys.stdout.flush()
        time.sleep(60)
    if mode == 'malformed':
        print('bad json', flush=True)
        continue
    if mode == 'wrong-id':
        send(dict(id=req['id'] + 1, result={}))
        continue
    if op == 'tools/list':
        if mode == 'ping':
            send(dict(method='notifications/tools/list_changed'))
            send(dict(id='ping-id', method='ping'))
            assert read() == dict(jsonrpc='2.0', id='ping-id', result={})
            send(dict(id='unknown-id', method='sampling/createMessage', params={}))
            answer = read()
            assert answer['id'] == 'unknown-id' and answer['error']['code'] == -32601
        cursor = req['params'].get('cursor')
        if mode == 'pages':
            result(req, tools=[tool('x' * 150)], nextCursor=str(req['id']))
        elif mode == 'bad-schema':
            result(req, tools=[dict(name='bad')])
        elif cursor is None or mode == 'loop':
            result(req, tools=[tool('one')], nextCursor='next')
        else:
            assert cursor == 'next'
            result(req, tools=[tool('two')])
    elif op == 'tools/call':
        name, args = req['params']['name'], req['params']['arguments']
        if name == 'tool-error':
            result(req, isError=True, content=[dict(type='text', text='fixture failure')])
        elif name == 'rpc-error':
            send(dict(id=req['id'], error=dict(code=-32602, message='invalid arguments')))
        elif name == 'structured':
            result(req, content=[], structuredContent=args)
        elif name == 'image':
            result(req, content=[dict(type='image', data='AAAA', mimeType='image/png')])
        else:
            result(req, content=[dict(type='text', text='echo'), dict(type='text', text=args['text'])])
Path('closed').touch()
'''


class ExternalToolsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.script = self.folder / 'server.py'
        self.script.write_text(_SERVER, encoding='utf-8')

    def client(self, mode='normal', **options):
        return MCPClient([sys.executable, '-I', '-B', str(self.script), mode],
                         cwd=self.folder, **options)

    def assert_reaped(self):
        pid = int((self.folder / 'pid').read_text())
        with self.assertRaises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)

    def test_negotiation_pages_original_schema_and_calls(self):
        with patch('external_tools.subprocess.Popen', wraps=subprocess.Popen) as launch:
            with self.client('ping') as client:
                tools = client.list_tools()
                self.assertEqual([tool['name'] for tool in tools], ['one', 'two'])
                self.assertEqual(tools[0]['inputSchema'], {'type': 'object', '$defs': {'原样': {}}})
                self.assertEqual(tools[0]['annotations'], {'readOnlyHint': True})
                text = '两行\n"; $() 🪐'
                self.assertEqual(client.call('echo', {'text': text}), 'echo\n' + text)
                self.assertEqual(json.loads(client.call('structured', {'value': '星'})), {'value': '星'})
            self.assertEqual(launch.call_args.kwargs['env'], {})
            self.assertEqual(launch.call_count, 1)
        self.assertTrue((self.folder / 'closed').exists())
        self.assert_reaped()
        client.close()
        with self.assertRaises(MCPError):
            client.list_tools()

    def test_remote_errors_remain_distinct_from_success_and_allow_another_call(self):
        with self.client('stderr') as client:
            for name, message in [('tool-error', 'fixture failure'), ('rpc-error', 'invalid arguments')]:
                with self.subTest(name=name), self.assertRaisesRegex(MCPError, message):
                    client.call(name, {})
            self.assertEqual(client.call('echo', {'text':'ok'}), 'echo\nok')
        self.assert_reaped()

    def test_fatal_responses_and_oversize_across_pages_close_client(self):
        for mode, hint in [('wrong-id', 'id'), ('partial', '关闭'), ('exit', '关闭'),
                           ('malformed', 'JSON'), ('huge', '大小'), ('pages', '大小'),
                           ('bad-schema', '工具定义'), ('loop', '游标')]:
            with self.subTest(mode=mode), self.client(mode, max_bytes=1024) as client:
                with self.assertRaisesRegex(MCPError, hint):
                    client.list_tools()
                self.assert_reaped()

    def test_media_only_is_not_an_empty_success(self):
        with self.client() as client, self.assertRaisesRegex(MCPError, '文本'):
            client.call('image', {})

    def test_failed_negotiation_reaps_process(self):
        for mode, hint in [('version', '版本'), ('capability', '能力')]:
            with self.subTest(mode=mode), self.assertRaisesRegex(MCPError, hint):
                with self.client(mode):
                    self.fail('incompatible session entered')
            self.assert_reaped()

    def test_deadline_includes_init_read_and_blocked_write(self):
        for mode in ('init-hang', 'hang', 'blocked-write'):
            start = time.monotonic()
            with self.subTest(mode=mode), self.assertRaises(TimeoutError):
                with self.client(mode, timeout=0.35, max_bytes=2**21) as client:
                    client.call('echo', {'text':'x' * 2**20})
            self.assertLess(time.monotonic() - start, 3)
            self.assert_reaped()

    def test_execution_budget_is_shared_and_caller_exception_closes(self):
        with self.assertRaises(TimeoutError):
            with self.client(timeout=0.4) as client:
                client.list_tools()
                time.sleep(0.4)
                client.call('echo', {'text':'late'})
        self.assert_reaped()
        with self.assertRaisesRegex(RuntimeError, 'caller'):
            with self.client():
                raise RuntimeError('caller')
        self.assert_reaped()


if __name__ == '__main__':
    unittest.main()
