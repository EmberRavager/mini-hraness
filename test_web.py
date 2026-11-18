import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from session_layer import dispatch


class ConversationTest(unittest.TestCase):
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
