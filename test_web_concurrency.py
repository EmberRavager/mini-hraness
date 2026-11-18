import http.client
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

from session_layer import _write_locks
from web import create_server


class WebConcurrencyTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.started, self.release = Event(), Event()
        self.session = str(uuid4())
        self.server = create_server(Path(folder.name), 0, 'fake', self.model)
        self.addCleanup(self.server.server_close)
        thread = Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.05})
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(self.pool.shutdown)
        self.addCleanup(self.release.set)

    def model(self, name, task, history, actions, timeout, **callbacks):
        if task == '写入' and not history:
            self.started.set()
            if not self.release.wait(10):
                raise TimeoutError('测试未释放模型')
            return dict(op='write', name='note.txt', text='示例')
        return dict(op='done', text='完成')

    def connection(self):
        return http.client.HTTPConnection(*self.server.server_address, timeout=4)

    def post(self, route, **fields):
        with closing(self.connection()) as conn:
            body = {'session': self.session, **fields}
            conn.request('POST', route, json.dumps(body), {'Content-Type': 'application/json'})
            response = conn.getresponse()
            return response.status, json.load(response)

    def test_idle_connection_and_busy_model_leave_other_requests_independent(self):
        with closing(self.connection()) as idle:
            idle.connect()
            writer = self.pool.submit(self.post, '/chat', message='写入')
            self.assertTrue(self.started.wait(3))
            self.assertEqual(self.post('/session')[1]['turns'], [])
            self.assertEqual(self.post('/sessions')[1]['sessions'], [])
            for fields in (dict(session=str(uuid4())), dict(workspace='other')):
                self.assertEqual(self.post('/chat', message='独立请求', **fields)[1]['reply'], '完成')
            self.release.set()
            self.assertEqual(writer.result()[1]['approval']['name'], 'note.txt')

    def test_approval_using_uuid_alias_waits_for_chat_commit(self):
        writer = self.pool.submit(self.post, '/chat', message='写入')
        self.assertTrue(self.started.wait(3))
        # 收到流式响应头说明审批请求已抵达服务器，此时生成请求仍未提交。
        with closing(self.connection()) as conn:
            alias = self.session.replace('-', '').upper()
            conn.request('POST', '/approve', json.dumps(dict(session=alias, allow=True)),
                         {'Content-Type': 'application/json', 'Accept': 'application/x-ndjson'})
            stream = conn.getresponse()
            self.assertEqual(stream.status, 200)
            self.assertEqual(self.post('/session')[1]['turns'], [])
            self.release.set()
            self.assertIn('approval', writer.result()[1])
            result = json.loads(stream.read().splitlines()[-1])
        self.assertEqual(result['type'], 'result')
        self.assertEqual(result['data']['files'], {'note.txt': '示例'})
        self.assertEqual(self.post('/session')[1]['turns'], [['写入', '完成']])
        self.assertEqual(self.post('/approve', allow=True)[0], 400)
        self.assertEqual(len(_write_locks), 0)
