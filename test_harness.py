import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import run, start


def replay(actions):
    queue = iter(actions)
    return lambda *_: next(queue)


class HarnessTest(unittest.TestCase):
    def test_page(self):
        state = start('写页面', expect='index.html')
        calls = []
        script = replay([dict(op='delegate', task='写标题'), dict(op='write', name='bad', text='x'),
                         dict(op='done', text='<h1>Hi</h1>'), dict(op='done', text='过早完成'),
                         dict(op='write', name='index.html', text='<h1>Hi</h1>'),
                         dict(op='read', name='index.html'), dict(op='done', text='已写入')])

        def model(task, history, actions, timeout):
            calls.append((task, list(history), list(actions)))
            return script()

        run(state, model, lambda *_: True, lambda _: None)
        self.assertEqual((state['status'], state['files']), ('done', {'index.html': '<h1>Hi</h1>'}))
        self.assertEqual(calls[1][:2], ('写标题', []))
        self.assertNotIn('write', calls[1][2])
        self.assertEqual(calls[3][1][-1]['result'], '<h1>Hi</h1>')
        self.assertEqual(calls[-1][1][-1]['result'], '<h1>Hi</h1>')

    def test_resume_pending_write_without_reasking_model(self):
        state = start('记录')
        snapshots = []
        def interrupt(*_):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            run(state, replay([dict(op='write', name='note.txt', text='ok')]), interrupt,
                lambda current: snapshots.append(json.dumps(current)))
        restored = json.loads(snapshots[-1])
        run(restored, replay([dict(op='done', text='ok')]), lambda *_: True, lambda _: None)
        self.assertEqual((restored['files'], restored['status']), ({'note.txt': 'ok'}, 'review'))

    def test_budget_shared_with_child(self):
        state = start('父任务', steps=2)
        run(state, replay([dict(op='delegate', task='子任务'), dict(op='done', text='结果')]),
            lambda *_: True, lambda _: None)
        self.assertEqual(state['status'], 'stopped')

    def test_six_children_per_run_including_after_resume(self):
        for resume in (False, True):
            with self.subTest(resume=resume):
                state = start('parent', steps=40)
                # A rejected request must leave all six slots available.
                requests = [dict(op='delegate', task=' ')]
                requests.extend(dict(op='delegate', task=f'child-{i}') for i in range(1, 8))
                parent = replay([*requests, dict(op='done', text='finished')])
                children = []
                last_slot_counts = []

                def model(task, history, *_):
                    if task == 'parent':
                        if history and history[-1]['action'] in requests[-2:]:
                            last_slot_counts.append(state.get('delegates_left'))
                        return parent()
                    children.append(task)
                    return dict(op='done', text=task)

                if resume:
                    snapshots = []

                    def interrupt(current):
                        if current['stack'][-1]['task'] == 'child-4':
                            snapshots.append(json.dumps(current))
                            raise KeyboardInterrupt

                    with self.assertRaises(KeyboardInterrupt):
                        run(state, model, lambda *_: True, interrupt)
                    state = json.loads(snapshots[-1])

                run(state, model, lambda *_: True, lambda _: None)
                history = state['stack'][0]['history']
                expected = [f'child-{i}' for i in range(1, 7)]
                self.assertEqual([entry['action'] for entry in history[:-1]], requests)
                self.assertEqual([entry['result'] for entry in history[1:7]], expected)
                self.assertTrue(history[0]['result'].startswith('失败'), history[0])
                self.assertTrue(history[7]['result'].startswith('失败'), history[7])
                self.assertEqual(children, expected)
                self.assertEqual(last_slot_counts, [0, 0])

    def test_child_lists_and_reads_virtual_files(self):
        state = start('parent')
        state['files'].update({'note.txt': 'readable content', 'other.txt': 'other'})
        listing, reading = dict(op='list'), dict(op='read', name='note.txt')
        parent = replay([dict(op='delegate', task='child'), dict(op='done', text='finished')])
        child = replay([listing, reading, dict(op='done', text='checked')])

        def model(task, history, actions, *_):
            if task == 'parent':
                return parent()
            if len(history) == 2:
                self.assertEqual([entry['action'] for entry in history], [listing, reading])
                for name in state['files']:
                    self.assertIn(name, history[0]['result'])
                self.assertEqual(history[1]['result'], 'readable content')
                self.assertTrue({'list', 'read'}.issubset(actions))
            return child()

        run(state, model, lambda *_: True, lambda _: None)
        self.assertEqual(state['stack'][0]['history'][0]['result'], 'checked')

    def test_child_cannot_write_or_delegate(self):
        state = start('parent')
        state['files']['note.txt'] = 'original'
        write = dict(op='write', name='note.txt', text='changed')
        delegate = dict(op='delegate', task='grandchild')
        parent = replay([dict(op='delegate', task='child'), dict(op='done', text='finished')])
        child = replay([write, delegate, dict(op='done', text='checked')])

        def model(task, history, actions, *_):
            if task == 'parent':
                return parent()
            self.assertEqual(task, 'child')
            self.assertTrue({'write', 'delegate'}.isdisjoint(actions))
            if len(history) == 2:
                self.assertEqual([entry['action'] for entry in history], [write, delegate])
                for entry in history:
                    self.assertTrue(entry['result'].startswith('失败'), entry)
            return child()

        run(state, model, lambda *_: True, lambda _: None)
        self.assertEqual(state['files'], {'note.txt': 'original'})
        self.assertEqual(state['stack'][0]['history'][0]['result'], 'checked')
        self.assertEqual(state.get('delegates_left'), 5)

    def test_word_count_runs_system_wc_on_virtual_content(self):
        state = start('count')
        state['files'].update({'note.txt': 'one\ttwo\nthree  four\n', 'other.txt': 'five'})
        command = dict(op='command', name='word_count', file='note.txt')
        with patch('subprocess.Popen', wraps=subprocess.Popen) as launch:
            run(state, replay([command, dict(op='done', text='finished')]),
                lambda *_: True, lambda _: None)
        entry = state['stack'][0]['history'][0]
        self.assertEqual(entry['action'], command)
        self.assertRegex(entry['result'], r'\b4\b')
        launch.assert_called_once()
        args, kwargs = launch.call_args
        argv = args[0] if args else kwargs['args']
        self.assertEqual([Path(argv[0]).name, *argv[1:]], ['wc', '-w'])

    def test_unknown_commands_are_denied(self):
        state = start('count')
        state['files']['note.txt'] = 'one two'
        commands = [dict(op='command', name=name, file='note.txt') for name in ('wc', 'other')]
        with patch('subprocess.Popen', side_effect=AssertionError('unexpected process')):
            run(state, replay([*commands, dict(op='done', text='finished')]),
                lambda *_: True, lambda _: None)
        history = state['stack'][0]['history'][:-1]
        self.assertEqual([entry['action'] for entry in history], commands)
        for entry in history:
            self.assertTrue(entry['result'].startswith('失败'), entry)

    def test_denial_invalid_input_and_deadline(self):
        state = start('试错', steps=4)
        run(state, replay([{}, dict(op='write', name='../outside', text='x'),
                           dict(op='write', name='ok', text='拒绝'), dict(op='done', text='结束')]),
            lambda *_: False, lambda _: None)
        self.assertEqual(state['files'], {})
        self.assertEqual(state['status'], 'review')
        with patch('harness.time.time', side_effect=[0, 1, 3, 3, 3, 3]):
            expired = start('慢模型', seconds=2)
            run(expired, replay([dict(op='done', text='太迟')]), lambda *_: True, lambda _: None)
        self.assertEqual(expired['status'], 'stopped')
