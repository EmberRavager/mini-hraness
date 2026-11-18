import time
import unittest
from unittest.mock import patch

from harness import run, start
from tool_registry import connect_tools
from workspace_tools import call_tool, toolbox


class LegacyWebSearchTest(unittest.TestCase):
    def setUp(self):
        self.state = start('查询公开资料')
        self.state['online'] = True
        transport = patch('external_tools.MCPClient')
        self.launch = transport.start()
        self.addCleanup(transport.stop)
        self.search = self.launch.return_value.__enter__.return_value
        self.search.list_tools.return_value = [dict(name='search', inputSchema=dict(type='object'))]
        self.search.call.return_value = '示例标题 https://example.test/'

    def test_only_online_parent_has_permission(self):
        for child in (False, True):
            for flags in ({}, {'online': False}, {'online': 1}, {'online': 'true'}, {'online': True}):
                with self.subTest(child=child, flags=flags):
                    tools = toolbox(dict(files={}, **flags), approve=None, child=child)
                    self.assertEqual('web_search' in tools, not child and flags.get('online') is True)
        self.launch.assert_not_called()

    def test_old_pending_search_uses_only_external_search_and_returns_provider_text(self):
        action = dict(op='web_search', query='  示例  ')
        self.state['stack'][0]['pending'] = action

        def finish(task, history, actions, timeout):
            self.assertEqual(history[-1], dict(action=action, result=self.search.call.return_value))
            return dict(op='done', text='完成')

        with patch('tool_registry.connect_tools', wraps=connect_tools) as connect:
            run(self.state, finish, self.fail, lambda _: None)
        connect.assert_called_once()
        self.assertEqual(connect.call_args.kwargs['provider'], 'search')
        self.launch.assert_called_once()
        self.search.call.assert_called_once_with('search', {'query': '示例', 'limit': 5})
        self.launch.return_value.__exit__.assert_called_once()
        self.assertFalse(self.launch.call_args.kwargs['cwd'].exists())

    def test_expired_deadline_and_invalid_queries_do_not_start_a_server(self):
        tools = toolbox(self.state, approve=None, child=False)
        for query in (' ', 'a\nb', 'x' * 257):
            with self.assertRaises(ValueError):
                call_tool(tools, dict(op='web_search', query=query))
        self.state['deadline'] = time.time() - 1
        with self.assertRaisesRegex(ValueError, '超时'):
            tools['web_search']('示例')
        self.launch.assert_not_called()
