import io
import json
import unittest
from unittest.mock import patch

from cli import chat


class OllamaChatTest(unittest.TestCase):
    def setUp(self):
        transport = patch("http.client.HTTPConnection", autospec=True)
        self.addCleanup(transport.stop)
        self.factory = transport.start()
        self.connection = self.factory.return_value
        self.respond(200, {"message": {"role": "assistant", "content": "{}"}})

    def respond(self, status, body):
        response = io.BytesIO(json.dumps(body).encode())
        response.status = status
        self.connection.getresponse.return_value = response

    def request_body(self, task="你好", history=(), actions=("done",)):
        chat("local-model", task, history, actions, 3.25)
        method, path, body, headers = self.connection.request.call_args.args
        self.assertEqual((method, path), ("POST", "/api/chat"))
        self.assertEqual(headers["Content-Type"], "application/json")
        self.factory.assert_called_once_with("127.0.0.1", 11434, timeout=3.25)
        return json.loads(body)

    def test_native_request_for_child_only_advertises_read_and_list(self):
        body = self.request_body(actions=["list", "read", "done"])
        self.assertEqual(body["model"], "local-model")
        self.assertIs(body["stream"], False)
        self.assertNotIn("format", body)
        self.assertEqual([msg["role"] for msg in body["messages"]], ["system", "user"])
        self.assertEqual(body["messages"][1]["content"], "你好")
        self.assertEqual([tool["type"] for tool in body["tools"]], ["function", "function"])
        schemas = {t["function"]["name"]: t["function"]["parameters"] for t in body["tools"]}
        self.assertEqual(set(schemas), {"list", "read"})
        self.assertEqual(schemas["list"]["properties"], {})
        self.assertEqual(schemas["read"]["required"], ["name"])
        self.assertEqual(schemas["read"]["properties"]["name"]["type"], "string")

    def test_parent_tools_match_executor_arguments(self):
        body = self.request_body(actions=["delegate", "command", "write", "done"])
        schemas = {t["function"]["name"]: t["function"]["parameters"] for t in body["tools"]}
        self.assertEqual(set(schemas), {"delegate", "command", "write"})
        for name, fields in [("delegate", {"task"}), ("command", {"name", "file"}),
                             ("write", {"name", "text"})]:
            schema = schemas[name]
            self.assertEqual(schema["type"], "object")
            self.assertEqual(set(schema["required"]), fields)
            self.assertEqual(set(schema["properties"]), fields)
            self.assertTrue(all(p["type"] == "string" for p in schema["properties"].values()))
        self.assertEqual(schemas["command"]["properties"]["name"]["enum"], ["word_count"])

    def test_previous_turns_are_messages_and_done_alone_exposes_no_tools(self):
        body = self.request_body({"previous_turns": [["一", "二"], ["三", "四"]], "message": "五"})
        self.assertFalse(body.get("tools"))
        self.assertEqual(body["messages"][1:], [
            {"role": "user", "content": "一"}, {"role": "assistant", "content": "二"},
            {"role": "user", "content": "三"}, {"role": "assistant", "content": "四"},
            {"role": "user", "content": "五"},
        ])

    def test_mixed_history_replays_complete_batch_once_followed_by_legacy_feedback(self):
        calls = [{"function": {"name": "read", "arguments": {"name": name}}}
                 for name in ["a", "b"]]
        exchange = [
            {"role": "assistant", "content": "", "thinking": "先读两个文件", "tool_calls": calls},
            {"role": "tool", "tool_name": "read", "content": "A"},
            {"role": "tool", "tool_name": "read", "content": "B"},
        ]
        history = [
            {"action": {"op": "read", "name": "a"}, "result": "A", "native": True},
            {"action": {"op": "read", "name": "b"}, "result": "B", "native": True,
             "exchange": exchange},
            {"action": {"op": "write", "name": "x"}, "result": "拒绝"},
        ]
        messages = self.request_body(history=history)["messages"][2:]
        self.assertEqual(messages[:-1], exchange)
        self.assertEqual(messages[-1]["role"], "user")
        for detail in ["write", "拒绝"]:
            self.assertIn(detail, messages[-1]["content"])

    def test_assistant_message_passes_through_including_json_looking_text(self):
        for message in [
            {"role": "assistant", "content": "你好！"},
            {"role": "assistant", "content": '{"op":"done","text":"literal"}'},
            {"role": "assistant", "content": "", "thinking": "需要读取文件",
             "tool_calls": [{"function": {"name": "list", "arguments": {}}}], "extra": 7},
        ]:
            with self.subTest(message=message):
                self.respond(200, {"message": message, "done": True})
                self.assertEqual(chat("model", "task", [], ["list"], 3), message)

    def test_tool_rejection_reports_model_status_and_reason_without_retry(self):
        self.respond(400, {"error": "does not support tools"})
        with self.assertRaises(OSError) as error:
            chat("text-only", "读取", [], ["read"], 3)
        for detail in ["400", "text-only", "does not support tools", "工具"]:
            self.assertIn(detail, str(error.exception))
        self.connection.request.assert_called_once()

    def test_rejects_invalid_message_or_top_level_action(self):
        for body in [
            {"op": "write"}, {"message": {"op": "write"}}, {"message": None},
            {"message": {"role": "tool", "content": "{}"}},
            {"message": {"role": "assistant", "content": "", "op": "write"}},
        ]:
            with self.subTest(body=body):
                self.respond(200, body)
                with self.assertRaises(OSError):
                    chat("model", "task", [], ["write"], 3)
