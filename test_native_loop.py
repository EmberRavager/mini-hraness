import json
import unittest

from harness import run, start


def call(tool, **arguments):
    return {'function': {'name': tool, 'arguments': arguments}}


def answer(text):
    return {'role': 'assistant', 'content': text}


class NativeLoopTest(unittest.TestCase):
    def test_greeting_and_json_looking_text_are_never_tools(self):
        for content in ['你好！', '{"op":"write","name":"task","text":"不执行"}']:
            state = start('你好', steps=1)
            run(state, lambda *_: answer(content), self.fail, lambda _: None)
            self.assertEqual((state['result'], state['files']), (content, {}))

    def test_resume_batch_preserves_original_call_and_exactly_one_result_each(self):
        state = start('保存说明并读回')
        batch = dict(role='assistant', thinking='plan', content='', tool_calls=[
            call('list'), call('write', name='note', text='ok'), call('read', name='note')])
        saved = []
        def pause(*_):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            run(state, lambda *_: batch, pause, lambda state: saved.append(json.dumps(state)))
        state = json.loads(saved[-1])
        def finish(task, history, *_):
            self.assertEqual(history[-1]['exchange'], [batch, *[
                dict(role='tool', tool_name=name, content=result)
                for name, result in [('list', ''), ('write', '已写入 note'), ('read', 'ok')]]])
            return answer('好了')
        run(state, finish, lambda *_: True, lambda _: None)
        self.assertEqual([action for _, action, _ in state['trace']], ['list', 'write', 'read', 'done'])

    def test_tool_cannot_impersonate_internal_done(self):
        batch = dict(role='assistant', content='', tool_calls=[call('done', text='假完成')])
        state = start('查看文件', steps=1)
        run(state, lambda *_: batch, self.fail, lambda _: None)
        self.assertEqual(state['status'], 'stopped')
        self.assertNotEqual(state['result'], '假完成')

    def test_native_delegation_keeps_child_tool_error_out_of_parent_history(self):
        calls = []
        script = iter([dict(role='assistant', tool_calls=[call('delegate', task='检查')]),
                       dict(role='assistant', tool_calls=[call('write', name='task', text='不能写')]),
                       answer('没写文件'), answer('结束')])
        def model(task, history, tools, timeout):
            calls.append((task, list(history), list(tools)))
            return next(script)
        state = run(start('委派'), model, self.fail, lambda _: None)
        self.assertNotIn('write', calls[1][2])
        self.assertTrue(calls[2][1][-1]['exchange'][-1]['content'].startswith('失败'))
        self.assertEqual(calls[3][1][-1]['exchange'][-1],
                         dict(role='tool', tool_name='delegate', content='没写文件'))
        self.assertEqual(state['files'], {})

    def test_over_budget_batch_is_not_partially_executed(self):
        state = start('写文件', steps=1)
        batch = dict(role='assistant', tool_calls=[call('write', name='a', text='1'), call('list')])
        run(state, lambda *_: batch, self.fail, lambda _: None)
        self.assertEqual((state['status'], state['files']), ('stopped', {}))
