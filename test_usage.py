import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

from ollama_chat import chat, loaded_context_length
from session_layer import dispatch
from web import Handler


def response(payload, status=200):
    raw = json.dumps(payload) if not isinstance(payload, bytes) else payload
    reply = io.BytesIO(raw.encode() if isinstance(raw, str) else raw)
    reply.status = status
    return reply


def completion(message, *, prompt, output, generation_ns, total_ns):
    return dict(model='actual', done=True, message=dict(role='assistant', **message),
                prompt_eval_count=prompt, eval_count=output,
                eval_duration=generation_ns, total_duration=total_ns)


def action(tool_name, **arguments):
    return dict(tool_calls=[dict(function=dict(name=tool_name, arguments=arguments))])


class UsageTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.sid = str(uuid4())
        http = patch('http.client.HTTPConnection', autospec=True)
        self.connect = http.start()
        self.addCleanup(http.stop)
        self.conn = self.connect.return_value
        self.models = {'models': [dict(name='other', context_length=999),
                                  dict(model='actual:latest', context_length=8192)]}

    def post(self, route, emit=None, **fields):
        return dispatch(self.root, route, dict(session=self.sid, **fields),
                        chat, model_name='requested', emit=emit)

    def test_parent_and_child_weighted_totals_and_restoration(self):
        def measured(message, prompt, output, seconds):
            return response(completion(message, prompt=prompt, output=output,
                                       generation_ns=seconds * 10**9, total_ns=seconds * 10**9 + 500000))
        self.conn.getresponse.side_effect = [
            measured(action('delegate', task='子任务'), 10, 3, 1),
            measured(dict(content='子答复'), 20, 40, 2),
            measured(dict(content='主答复'), 30, 7, 1), response(self.models),
        ]
        final = self.post('/chat', message='模拟任务')
        expected = dict(input_tokens=60, output_tokens=50, output_tps=12.5,
                        last_input_tokens=30, context_length=8192, elapsed_ms=4001.5, calls=3)
        self.assertEqual(final['usage'], expected)
        self.assertEqual(self.post('/session')['turn_usage'], [expected])
        self.assertEqual([c.args[:2] for c in self.conn.request.call_args_list],
                         [('POST', '/api/chat')] * 3 + [('GET', '/api/ps')])

    def test_waiting_approval_and_streaming_resume_count_each_call_once(self):
        first = completion(action('write', name='demo', text='ok'), prompt=5, output=3,
                           generation_ns=10**9, total_ns=2 * 10**9)
        second = completion(dict(content='好'), prompt=8, output=2,
                            generation_ns=10**9, total_ns=10**9)
        self.conn.getresponse.side_effect = [response(first), response(self.models),
                                             response(second), response(self.models)]
        events = []
        pending = self.post('/chat', emit=events.append, message='模拟写入')
        self.assertEqual(pending['turn_usage'], [])
        self.assertEqual(self.post('/session')['usage'], pending['usage'])
        self.assertEqual((pending['usage']['calls'], pending['usage']['input_tokens']), (1, 5))
        result = self.post('/approve', emit=events.append, allow=True)
        self.assertEqual(result['usage'], dict(input_tokens=13, output_tokens=5, output_tps=2.5,
                                               last_input_tokens=8, context_length=8192,
                                               elapsed_ms=3000, calls=2))
        self.assertEqual(self.post('/session')['turn_usage'], [result['usage']])

    def test_legacy_turns_align_and_new_turn_does_not_inherit_counts(self):
        folder = self.root / 'default' / '.mini-harness' / self.sid
        folder.mkdir(parents=True)
        turns = [['旧问题', '旧回复']]
        (folder / 'session.json').write_text(json.dumps(dict(turns=turns, files={}, active=None)))
        self.assertEqual(self.post('/session')['turn_usage'], [None])
        payload = dict(model='actual', done=True, message=dict(role='assistant', content='ok'),
                       eval_count=0, total_duration=0, eval_duration=0)
        self.conn.getresponse.side_effect = [response(payload), response(self.models, 503),
                                             response(payload), response(self.models)]
        for i in range(2):
            usage = self.post('/chat', message='新问题')['usage']
            self.assertEqual(usage, dict(input_tokens=None, output_tokens=0, output_tps=None,
                                         last_input_tokens=None, context_length=8192 if i else None,
                                         elapsed_ms=0, calls=1))
        view = self.post('/session')
        self.assertEqual(len(view['turn_usage']), len(view['turns']))
        self.assertEqual(view['turn_usage'][0], None)
        self.assertEqual([u['calls'] for u in view['turn_usage'][1:]], [1, 1])

    def test_a_missing_measurement_does_not_turn_into_zero(self):
        first = completion(action('list'), prompt=4, output=5, generation_ns=10**9, total_ns=10**9)
        first.pop('prompt_eval_count')
        second = completion(dict(content='ok'), prompt=7, output=2, generation_ns=10**9, total_ns=10**9)
        second['eval_duration'] = False
        self.conn.getresponse.side_effect = [response(first), response(second), response(self.models)]
        self.assertEqual(self.post('/chat', message='test')['usage'], dict(
            input_tokens=None, output_tokens=7, output_tps=None, last_input_tokens=7,
            context_length=8192, elapsed_ms=2000, calls=2))

    def test_truncated_stream_reports_final_frame_measurements(self):
        payload = completion(dict(content='x'), prompt=4, output=6, generation_ns=2*10**9, total_ns=3*10**9)
        payload['done_reason'] = 'length'
        self.conn.getresponse.side_effect = [response(payload), response(self.models)]
        result = self.post('/chat', emit=lambda _: None, message='test')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['usage'], dict(input_tokens=4, output_tokens=6, output_tps=3,
                                               last_input_tokens=4, context_length=8192,
                                               elapsed_ms=3000, calls=1))

    def test_long_message_cannot_displace_usage_from_the_trace(self):
        payload = completion(dict(content='ok', extra=['x' * 4096] * 12), prompt=15,
                             output=11, generation_ns=10**9, total_ns=10**9)
        self.conn.getresponse.side_effect = [response(payload), response(self.models)]
        result = self.post('/chat', message='test')
        self.assertEqual((result['usage']['input_tokens'], result['usage']['output_tps']), (15, 11))

    def test_http_json_and_ndjson_final_result_expose_the_same_usage(self):
        payload = completion(dict(content='ok'), prompt=4, output=2, generation_ns=10**9, total_ns=10**9)
        self.conn.getresponse.side_effect = [response(payload), response(self.models),
                                             response(payload), response(self.models)]
        for accept in ('application/json', 'application/x-ndjson'):
            with self.subTest(accept=accept):
                handler = object.__new__(Handler)
                handler.server = Mock(server_port=8765, root=self.root, model=chat, default_model='requested')
                body = json.dumps(dict(session=self.sid, message='test')).encode()
                handler.path = '/chat'
                handler.headers = Mock()
                handler.headers.get.side_effect = {'Host': '127.0.0.1:8765', 'Accept': accept,
                                                   'Content-Length': str(len(body))}.get
                handler.headers.get_content_type.return_value = 'application/json'
                handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
                handler.send_response = handler.send_header = handler.end_headers = Mock()
                handler.do_POST()
                result = json.loads(handler.wfile.getvalue().splitlines()[-1])
                if accept == 'application/x-ndjson':
                    self.assertEqual(result['type'], 'result')
                    result = result['data']
                self.assertEqual(result['usage'], dict(input_tokens=4, output_tokens=2,
                    output_tps=2, last_input_tokens=4, context_length=8192, elapsed_ms=1000, calls=1))

    def test_missing_model_or_invalid_capacity_is_unknown(self):
        for data in ({'models': [dict(name='actual:other', context_length=999)]},
                     {'models': [dict(name='actual', context_length=True)]}, {}, None):
            with self.subTest(data=data):
                self.conn.getresponse.return_value = response(data)
                self.assertIsNone(loaded_context_length('actual'))
        self.connect.side_effect = ConnectionRefusedError
        self.assertIsNone(loaded_context_length('actual'))
