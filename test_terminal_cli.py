import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import cli
from harness import start


HERE = Path(__file__).resolve().parent


class TerminalCliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)

    def conversation(self, root, messages, actions):
        out, err = io.StringIO(), io.StringIO()
        argv = ["cli.py", "--model", "offline", "--workspace", str(root)]
        with patch.object(sys, "argv", argv), patch("builtins.input", side_effect=messages), \
                patch.object(cli, "chat", side_effect=actions) as model, \
                redirect_stdout(out), redirect_stderr(err):
            cli.main()
        return [call.args[1] for call in model.call_args_list], out.getvalue(), err.getvalue()

    def test_continues_across_entries_and_new_survives_restart(self):
        root = self.folder / "甲"
        done = dict(op="done", text="蓝色")
        self.conversation(root, ["选个颜色", "/exit"], [done])
        tasks, output, _ = self.conversation(root, ["刚才选了什么", "/new", "新问题", "/exit"], [done, done])
        self.assertIn("蓝色", str(tasks[0]))
        self.assertEqual(tasks[1], "新问题")
        self.assertIn("蓝色", output)
        tasks, _, _ = self.conversation(root, ["继续", "/exit"], [done])
        self.assertIn("新问题", str(tasks[0]))
        self.assertNotIn("选个颜色", str(tasks[0]))
        tasks, _, _ = self.conversation(self.folder / "乙", ["另一工作区", "/exit"], [done])
        self.assertEqual(tasks, ["另一工作区"])

    def test_pending_write_previews_again_and_each_write_needs_approval(self):
        root = self.folder / "审批"
        first = dict(op="write", name="first.txt", text="第一份预览")
        second = dict(op="write", name="second.txt", text="第二份预览")
        _, output, _ = self.conversation(root, ["写两个文件", "/exit"], [first])
        self.assertIn("第一份预览", output)
        self.assertFalse(list(root.rglob("first.txt")))
        _, output, _ = self.conversation(root, ["y", "", "/exit"], [second, dict(op="done", text="结束")])
        self.assertLess(output.index("第一份预览"), output.index("第二份预览"))
        self.assertEqual([p.read_text() for p in root.rglob("first.txt")], ["第一份预览"])
        self.assertFalse(list(root.rglob("second.txt")))

    def test_chat_error_allows_another_turn(self):
        _, output, errors = self.conversation(self.folder, ["第一次", "再试", "/exit"], [
            ConnectionRefusedError(), dict(op="done", text="重试成功")])
        self.assertIn("Ollama", errors)
        self.assertIn("重试成功", output)

    def launch(self, *args, stdin="", path=os.defpath, check=True):
        return subprocess.run([HERE / "start.sh", *args], cwd=self.folder, text=True,
                              input=stdin, capture_output=True, timeout=10, check=check,
                              env={"PATH": path, "PYTHONDONTWRITEBYTECODE": "1"})

    def stub_python(self, script):
        python = self.folder / "python3"
        python.write_text("#!/bin/sh\n" + script)
        python.chmod(0o755)
        return str(self.folder) + os.pathsep + os.defpath

    def test_launcher_starts_interactive_default_and_custom_roots(self):
        for args, root in [((), self.folder / ".mini-harness/workspaces"),
                           (("--workspace", "my workspace"), self.folder / "my workspace")]:
            with self.subTest(args=args):
                result = self.launch(*args, stdin="/exit\n")
                self.assertIn("/exit", result.stdout)
                self.assertTrue(root.is_dir())
                self.assertFalse((self.folder / ".mini-harness/run.json").exists())

    def test_launcher_forwards_server_arguments(self):
        # 替身只记录 Python 参数，不启动服务器或占用本地端口。
        path = self.stub_python('printf "%s\\n" "$@"\n')
        for flag in ["--web", "--api"]:
            args = ["--port", "9001", "--workspace", "my workspace"]
            command = self.launch(flag, *args, path=path).stdout.splitlines()
            self.assertEqual(command[:2], [str(HERE / "web.py"), "--model"])
            self.assertTrue(command[2])
            self.assertEqual(command[3:], args)
        self.assertEqual(self.launch("--ui", path=path).stdout.splitlines(),
                         ["-m", "http.server", "8766", "--bind", "127.0.0.1",
                          "--directory", str(HERE / "frontend")])

    def assert_app_stops_on_failure(self, failing, *args):
        (self.folder / ("fail-" + failing)).touch()
        path = self.stub_python(r'''
case "$1" in -m) role=ui ;; *) role=api ;; esac
printf '%s\n' "$@" > "$role.args"
if [ -f "fail-$role" ]; then
    # 等另一个替身装好信号处理器，再模拟启动失败。
    for attempt in 1 2 3 4 5; do
        [ -f ready ] && exit 7
        sleep 1
    done
    exit 8
fi
trap 'echo "$role" > stopped; exit 0' TERM
touch ready
for attempt in 1 2 3 4 5; do sleep 1; done
''')
        result = self.launch("--app", *args, path=path, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.folder / "stopped").read_text().strip(),
                         "ui" if failing == "api" else "api")
        api = (self.folder / "api.args").read_text().splitlines()
        self.assertEqual(api[:2], [str(HERE / "web.py"), "--model"])
        self.assertTrue(api[2])
        self.assertEqual(api[3:], list(args))
        self.assertEqual((self.folder / "ui.args").read_text().splitlines(),
                         ["-m", "http.server", "8766", "--bind", "127.0.0.1",
                          "--directory", str(HERE / "frontend")])

    def test_app_stops_ui_when_api_fails(self):
        self.assert_app_stops_on_failure("api", "--workspace", "my workspace")

    def test_app_stops_api_when_ui_fails(self):
        self.assert_app_stops_on_failure("ui")

    def test_app_rejects_invalid_arguments_before_launch(self):
        path = self.stub_python("touch launched\nexit 7\n")
        for args in [("--workspace",), ("--port", "8767"), ("--workspace", "lab", "extra")]:
            with self.subTest(args=args):
                result = self.launch("--app", *args, path=path, check=False)
                self.assertEqual(result.returncode, 2)
                self.assertFalse((self.folder / "launched").exists())

    def test_launcher_one_shot_and_resume(self):
        result = self.launch("单次任务", "--steps", "0", "--model", "offline")
        self.assertIn("stopped", result.stdout)
        checkpoint = self.folder / ".mini-harness/run.json"
        self.assertEqual(json.loads(checkpoint.read_text())["_cli_model"], "offline")
        state = start("继续任务")
        state["_cli_model"] = "checkpoint-model"
        state["stack"][0]["pending"] = dict(op="done", text="恢复成功")
        checkpoint.write_text(json.dumps(state))
        self.assertIn("恢复成功", self.launch("--resume").stdout)
        self.assertEqual(json.loads(checkpoint.read_text())["_cli_model"], "checkpoint-model")
