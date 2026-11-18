import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from session_layer import dispatch
from tool_registry import DEFAULT_CONFIG


class FilesServer:
    def __init__(self, argv, *, cwd, timeout):
        self.directory = cwd

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def list_tools(self):
        return [dict(name=name, inputSchema=dict(type='object'), description='test')
                for name in ('write_file', 'list_directory', 'read_text_file')]

    def call(self, name, arguments):
        path = self.directory / arguments['path']
        if name == 'write_file':
            path.write_text(arguments['content'])
            return '已写入'
        return path.read_text()


def tool(name, **arguments):
    return dict(role='assistant', tool_calls=[dict(function=dict(name=name, arguments=arguments))])


class RegistryIntegrationTest(unittest.TestCase):
    @patch('external_tools.MCPClient', FilesServer)
    def test_external_tools_role_history_and_page_followup(self):
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(StringIO()):
            root = Path(temp)
            session = dict(session=str(uuid4()), workspace='demo')
            decisions = iter([tool('delegate', task='检查文件'), tool('write_file', path='wrong.html', content='x'),
                              dict(role='assistant', content='已检查'), tool('write_file', path='index.html', content='第一版'),
                              dict(role='assistant', content='写好了'), tool('read_text_file', path='index.html'),
                              tool('write_file', path='index.html', content='第二版'), dict(role='assistant', content='已修改')])
            captures = []
            def model(task, history, actions, timeout):
                captures.append((task, json.dumps(history, ensure_ascii=False), list(actions)))
                return next(decisions)
            def post(route, **body):
                return dispatch(root, route, session | body, model, tool_config=DEFAULT_CONFIG, emit=lambda frame: None)
            self.assertEqual(post('/chat', message='写一个页面')['files'], {'index.html':'第一版'})
            self.assertNotIn('write_file', captures[1][2])
            self.assertNotIn('delegate', captures[1][2])
            self.assertIn('无权限', captures[2][1])
            self.assertIn('write_file', captures[0][2])
            self.assertNotIn('search', captures[0][2])
            self.assertEqual(post('/chat', message='改这页')['files'], {'index.html':'第二版'})
            self.assertIn('第一版', captures[-2][1])
            restored = post('/session')
            self.assertEqual(restored['turns'][-1], ['改这页', '已修改'])
            actions = restored['turn_actions'][-1]
            self.assertEqual([event['name'] for event in actions], ['read_text_file', 'write_file'])
            self.assertEqual(actions[-1]['request'], {'path':'index.html'})
