import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

from session_layer import dispatch
from web import Handler, installed_models


class ConversationTest(unittest.TestCase):
    @patch('web.dispatch')
    @patch('web.installed_models', return_value=['local:4b'])
    def test_http_rejects_unknown_model_before_dispatch(self, catalog, dispatcher):
        handler = Mock(path='/chat')
        payload = b'{"message":"hello","model":"missing"}'
        handler.headers.get.return_value = str(len(payload))
        handler.headers.get_content_type.return_value = 'application/json'
        handler.rfile = BytesIO(payload)
        Handler.do_POST(handler)
        self.assertEqual(handler.send.call_args.args[1], 400)
        dispatcher.assert_not_called()

    @patch('web.http.client.HTTPConnection')
    def test_model_list_uses_local_ollama_and_reports_offline(self, connect):
        response = BytesIO(b'{"models":[{"name":"first:4b"},{"name":"next:8b"}]}')
        response.status = 200
        connect.return_value.getresponse.return_value = response
        self.assertEqual(installed_models(), ['first:4b', 'next:8b'])
        connect.return_value.request.assert_called_once_with('GET', '/api/tags')
        connect.side_effect = ConnectionRefusedError('offline')
        handler = Mock(path='/models')
        Handler.do_GET(handler)
        self.assertEqual(handler.send.call_args.args[1], 502)

    def test_selected_model_stays_with_pending_run_and_sessions_are_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            root, session = Path(directory), str(uuid4())
            actions = iter([dict(op='write', name='a.txt', text='a'), dict(op='done', text='一'),
                            dict(op='done', text='二')])
            used = []
            def model(name, *_):
                used.append(name)
                return next(actions)
            def post(route, workspace='demo', name='alpha', **body):
                return dispatch(root, route, dict(session=session, workspace=workspace, **body),
                                model, model_name=name)
            post('/chat', message='第一条')
            post('/approve', name='beta', allow=True)
            post('/chat', name='beta', message='第二条')
            self.assertEqual(used, ['alpha', 'alpha', 'beta'])
            self.assertEqual(post('/sessions')['sessions'], [dict(id=session, title='第一条')])
            self.assertEqual(post('/sessions', workspace='other')['sessions'], [])
            self.assertEqual(post('/session')['trace'], [['主', 'done', '二']])

    def test_followup_approval_and_session_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            root, session = Path(directory), str(uuid4())
            actions = iter([dict(op='write', name='index.html', text='第一版'),
                            dict(op='done', text='写好了'), dict(op='read', name='index.html'),
                            dict(op='done', text='第一版'), dict(op='done', text='你好')])
            inputs = []
            def model(task, history, *_):
                inputs.append((task, list(history)))
                return next(actions)
            def post(route, **body):
                return dispatch(root, route, {'session': session, **body}, model)
            self.assertEqual(post('/chat', message='写页面')['approval']['text'], '第一版')
            self.assertEqual(post('/approve', allow=True)['files'], {'index.html': '第一版'})
            self.assertEqual(post('/chat', message='刚才写了什么？')['reply'], '第一版')
            self.assertIn('写好了', str(inputs[2][0]))
            self.assertEqual(inputs[3][1][-1]['result'], '第一版')
            session = str(uuid4())
            self.assertEqual(post('/chat', message='你好')['files'], {})
            self.assertNotIn('写好了', str(inputs[-1][0]))

    def test_workspace_isolation_and_context_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root, session = Path(directory), str(uuid4())
            seen = []
            def model(task, *_):
                seen.append(task)
                return dict(op='done', text='收到')
            def post(route, workspace, **body):
                return dispatch(root, route, dict(workspace=workspace, session=session, **body), model)
            for number in range(8):
                post('/chat', 'lab', message=str(number))
            self.assertEqual([turn[0] for turn in seen[-1]['previous_turns']], list('123456'))
            self.assertEqual(len(post('/session', 'lab')['turns']), 8)
            self.assertEqual(post('/session', 'other')['turns'], [])
            with self.assertRaises(ValueError):
                post('/session', '../escape')

    def test_denial_and_stale_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root, session = Path(directory), str(uuid4())
            actions = iter([dict(op='write', name='a', text='no'), dict(op='done', text='取消')])
            def post(route, **body):
                return dispatch(root, route, dict(session=session, **body), lambda *_: next(actions))
            post('/chat', message='试写')
            self.assertEqual(post('/approve', allow=False)['files'], {})
            with self.assertRaises(ValueError):
                post('/approve', allow=True)
