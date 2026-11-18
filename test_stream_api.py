import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

from web import Handler
from session_layer import dispatch


class StreamApiTest(unittest.TestCase):
    def test_live_write_waits_and_resumes_under_same_event_id(self):
        with tempfile.TemporaryDirectory() as directory:
            frames, session = [], dict(session=str(uuid4()))
            script = iter([dict(role='assistant', tool_calls=[dict(function=dict(name='write',arguments=dict(name='a',text='示例')))]),
                           dict(role='assistant', content='完成')])
            def model(name, task, *_, on_text, on_request, on_thinking):
                on_request(dict(model=name, messages=[dict(role='user',content=task)]))
                return next(script)
            def post(route, **body):
                return dispatch(Path(directory), route, session | body, model, model_name='fake', emit=frames.append)
            pending = post('/chat', message='写文件')
            tool = pending['events'][-1]
            self.assertEqual((tool['name'],tool['status']),('write','waiting'))
            self.assertEqual(pending['files'], {})
            final = post('/approve', allow=True)
            states = [f['event']['status'] for f in frames if f['type']=='activity' and f['event']['id']==tool['id']]
            self.assertEqual(states, ['running','waiting','running','ok'])
            self.assertEqual([e['id'] for e in final['events']].count(tool['id']), 1)
            self.assertEqual(final['files'], {'a':'示例'})

    def test_online_permission_is_per_turn_not_per_session(self):
        with tempfile.TemporaryDirectory() as directory:
            body = dict(session=str(uuid4()), message='示例')
            observed = []
            def model(task, history, actions, timeout):
                observed.append('web_search' in actions)
                return dict(role='assistant', content='收到')
            for fields in [{}, {'online': True}, {}]:
                dispatch(Path(directory), '/chat', {**body, **fields}, model)
            self.assertEqual(observed, [False, True, False])
            with self.assertRaises(ValueError):
                dispatch(Path(directory), '/chat', dict(**body, online='yes'), model)

    def test_browser_disconnect_after_submission_still_commits_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            session = dict(session=str(uuid4()), message='你好')
            payload = json.dumps(session).encode()
            handler = object.__new__(Handler)
            def model(*_, on_request, on_text, on_thinking):
                on_request({'model':'fake'})
                on_text('你好')
                return dict(role='assistant', content='你好')
            handler.server = Mock(root=Path(directory), default_model='fake', server_port=8765, model=model)
            handler.path = '/chat'
            handler.headers = Mock()
            handler.headers.get.side_effect = {'Host':'127.0.0.1:8765', 'Accept':'application/x-ndjson',
                                               'Content-Length':str(len(payload))}.get
            handler.headers.get_content_type.return_value = 'application/json'
            handler.rfile, handler.wfile = io.BytesIO(payload), Mock()
            handler.wfile.write.side_effect = BrokenPipeError()
            handler.send_response = handler.send_header = handler.end_headers = Mock()
            handler.do_POST()
            restored = dispatch(Path(directory), '/session', session, model)
            self.assertEqual(restored['turns'], [['你好', '你好']])
            handler.wfile.write.assert_called_once()

    def test_text_is_flushed_before_model_finishes_and_only_one_final_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            body = json.dumps(dict(session=str(uuid4()), message='你好')).encode()
            handler = object.__new__(Handler)
            handler.server = Mock(root=Path(directory), default_model='fake', server_port=8765)
            handler.headers = Mock()
            handler.headers.get.side_effect = {'Host':'127.0.0.1:8765', 'Accept':'application/x-ndjson',
                                               'Content-Length':str(len(body))}.get
            handler.headers.get_content_type.return_value = 'application/json'
            handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
            handler.path = '/chat'
            handler.send_response = handler.send_header = handler.end_headers = Mock()
            def model(*_, on_text, on_request, on_thinking):
                on_request({'model':'fake', 'messages':[dict(role='user', content='你好')]})
                first = json.loads(handler.wfile.getvalue().splitlines()[-1])
                self.assertEqual((first['type'], first['event']['status']), ('activity', 'running'))
                on_thinking()
                thinking = json.loads(handler.wfile.getvalue().splitlines()[-1])
                self.assertEqual((thinking['event']['id'], thinking['event']['phase']),
                                 (first['event']['id'], 'thinking'))
                self.assertNotIn('thinking', thinking['event'])
                on_text('你')
                self.assertIn('你', handler.wfile.getvalue().decode())
                on_text('好')
                return dict(role='assistant', content='你好')
            handler.server.model = model
            handler.do_POST()
            frames = [json.loads(line) for line in handler.wfile.getvalue().splitlines()]
            self.assertEqual([f['type'] for f in frames], ['reset','activity','activity','delta','delta','activity','result'])
            self.assertEqual(frames[1]['event']['id'], frames[-2]['event']['id'])
            self.assertEqual(frames[-1]['data']['reply'], '你好')
