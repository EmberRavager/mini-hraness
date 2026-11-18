import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from session_layer import dispatch


class Reply(dict):
    def __init__(self, name, **message):
        super().__init__(role='assistant', **message)
        self.wire = dict(name=name, status='ok', request={'model': name}, response={'message': dict(self)})


class TraceEventsTest(unittest.TestCase):
    def test_snapshots_survive_approval_and_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            session = dict(session=str(uuid4()))
            arguments = dict(name='note', text='sample')
            responses = iter([Reply('fake', tool_calls=[dict(function=dict(name='write', arguments=arguments))]),
                              Reply('fake', content='已保存')])
            def post(route, **body):
                return dispatch(Path(directory), route, session | body, lambda *_: next(responses))
            first = post('/chat', message='保存文件')
            self.assertEqual(len(first['events']), 1)
            events = post('/approve', allow=True)['events']
            self.assertEqual([(e['kind'], e['call_id']) for e in events],
                             [('model', 1), ('tool', 1), ('model', 2)])
            self.assertEqual(events[1]['request'], arguments)
            self.assertEqual(events[1]['response'], '已写入 note')
            self.assertEqual(events, post('/session')['events'])

    def test_failed_model_call_is_inspectable(self):
        with tempfile.TemporaryDirectory() as directory:
            def fails(*_):
                error = OSError('offline')
                error.wire = dict(name='fake', request=dict(model='fake'), response=dict(error='offline'), status='error')
                raise error
            result = dispatch(Path(directory), '/chat', dict(session=str(uuid4()), message='你好'), fails)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['events'][0]['response']['error'], 'offline')

    def test_child_and_parent_have_separate_model_call_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            responses = iter([Reply('local', tool_calls=[dict(function=dict(name='delegate', arguments={'task':'检查'}))]),
                              Reply('local', content='子结果'), Reply('local', content='结束')])
            result = dispatch(Path(directory), '/chat', dict(session=str(uuid4()), message='委派检查'),
                              lambda *_: next(responses))
            self.assertEqual([(e['role'], e['kind'], e['call_id']) for e in result['events']],
                             [('主','model',1), ('子','model',2), ('主','tool',1), ('主','model',3)])
            self.assertEqual(result['events'][2]['response'], '子结果')
