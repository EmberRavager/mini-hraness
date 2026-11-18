import copy
import http.client
import io
import json
import unittest
from unittest.mock import Mock, call, patch

from ollama_chat import AssistantMessage, ModelCallError, chat
from trace_snapshot import snapshot


class FrameStream:
    status = 200

    def __init__(self, frames):
        self.frames = frames
        self.closed = False

    def close(self):
        self.closed = True

    def __iter__(self):
        for frame in self.frames:
            if isinstance(frame, Exception):
                raise frame
            yield frame if isinstance(frame, bytes) else json.dumps(frame).encode() + b"\n"


class OllamaTraceTest(unittest.TestCase):
    def setUp(self):
        factory = patch("http.client.HTTPConnection", autospec=True)
        self.conn = factory.start().return_value
        self.addCleanup(factory.stop)

    def reply(self, status, body):
        stream = io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode())
        stream.status = status
        self.conn.getresponse.return_value = stream

    def test_success_records_actual_exchange_and_usage(self):
        arguments = {"name": "a", "apiKey": "private-value"}
        message = {"role": "assistant", "thinking": "private-thought", "content": "读取",
                   "tool_calls": [{"function": {"name": "read", "arguments": arguments}}]}
        payload = {"model": "resolved", "message": message, "done": True, "eval_count": 2,
                   "prompt_eval_count": 17, "total_duration": 897}
        history = [{"exchange": [message, {"role": "tool", "content": "结果"}]}]
        original = copy.deepcopy(history)
        self.reply(200, payload)
        answer = chat("requested", "继续", history, ["read"], 3)
        sent = json.loads(self.conn.request.call_args.args[2])
        self.assertEqual(sent["messages"][2:], original[0]["exchange"])
        self.assertEqual(history, original)
        self.assertIsInstance(answer, AssistantMessage)
        self.assertIsInstance(answer, dict)
        self.assertEqual(json.loads(json.dumps(answer)), message)
        self.assertEqual(answer.wire, {"name": "requested", "status": "ok",
                                       "request": snapshot(sent), "response": snapshot(payload)})
        self.assertEqual(answer.wire["response"]["prompt_eval_count"], 17)
        public = answer.wire["response"]["message"]
        self.assertEqual(public["content"], "读取")
        self.assertEqual(public["tool_calls"][0]["function"]["arguments"]["name"], "a")
        self.assertNotIn("private-", json.dumps(answer.wire))
        answer["tool_calls"].clear()
        self.assertTrue(public["tool_calls"])

    def test_http_and_invalid_replies(self):
        for status, body, detail in [
            (403, {"error": "denied Authorization: Bearer private-auth"}, "denied"),
            (502, b"bad gateway", "bad gateway"),
            (200, b"broken", "JSON"),
            (200, {"message": {"role": "user"}}, "assistant"),
            (200, {"message": {"role": "assistant", "op": "done"}}, "op"),
        ]:
            with self.subTest(status=status, body=body):
                self.reply(status, body)
                with self.assertRaises(ModelCallError) as caught:
                    chat("model", "task", [], [], 3)
                exc = caught.exception
                self.assertIsInstance(exc, OSError)
                self.assertEqual(exc.wire["response"], {"http_status": status, "error": str(exc)})
                self.assertIn(detail, str(exc))
                self.assertNotIn("private-auth", str(exc))
                self.assertEqual(exc.wire["name"], "model")
                self.assertEqual(exc.wire["status"], "error")
                self.assertEqual(exc.wire["request"], json.loads(self.conn.request.call_args.args[2]))

    def test_transport_failures(self):
        for error in [TimeoutError("timed out"), http.client.BadStatusLine("protocol error")]:
            with self.subTest(error=error):
                self.conn.getresponse.side_effect = error
                with self.assertRaises(ModelCallError) as caught:
                    chat("model", "task", [], [], 3)
                self.assertEqual(caught.exception.wire["response"], {"error": str(caught.exception)})
                self.assertIn(str(error), str(caught.exception))
                self.assertIs(caught.exception.__cause__, error)

    def test_stream_delivers_only_content_before_reading_next_frame(self):
        received = []
        first = {"id": "a", "function": {"index": 0, "name": "read", "arguments": {"name": "a"}}}
        second = {"id": "b", "function": {"index": 1, "name": "read", "arguments": {"name": "b"}}}
        usage = {"prompt_eval_count": 27, "eval_count": 9, "total_duration": 123}

        def frames():
            yield b"\n"
            yield {"model": "actual", "message": {"role": "assistant", "thinking": "隐藏"}}
            yield {"message": {"role": "assistant", "content": "第一", "tool_calls": [first]}}
            self.assertEqual(received, ["第一"])
            yield {"message": {"role": "assistant", "thinking": "推理", "content": "段"}, "done": False}
            self.assertEqual(received, ["第一", "段"])
            yield {"done": True, "message": {"role": "assistant", "content": "。",
                                             "tool_calls": [second, first]}, **usage}
            self.fail("不应在完成帧后继续等待数据")

        self.conn.getresponse.return_value = FrameStream(frames())
        answer = chat("model", "task", [], ["read"], 3, on_text=received.append)
        self.assertTrue(json.loads(self.conn.request.call_args.args[2])["stream"])
        self.assertEqual(received, ["第一", "段", "。"])
        self.assertEqual(answer, {"role": "assistant", "thinking": "隐藏推理", "content": "第一段。",
                                  "tool_calls": [first, second, first]})
        self.assertEqual(answer.wire["response"], {"done": True, "model": "actual", **usage,
                                                   "message": snapshot(dict(answer))})

    def test_stream_failure_discards_pending_calls(self):
        start = {"done": False, "message": {"role": "assistant", "content": "开始", "tool_calls": [
            {"function": {"name": "read", "arguments": {"name": "pending-tool-arg"}}}]}}
        for ending in [[], [b'{"mess'], [ConnectionResetError("disconnected")],
                       [http.client.IncompleteRead(b"")], [{"done": True, "error": "overloaded"}],
                       [{"done": True, "done_reason": "length"}], [{"done": "true"}],
                       [{"done": True, "message": {"tool_calls": [{"function": {"name": "read"}}]}}]]:
            with self.subTest(ending=ending):
                self.conn.getresponse.return_value = FrameStream([start, *ending])
                seen = []
                with self.assertRaises(ModelCallError) as caught:
                    chat("model", "task", [], ["read"], 3, on_text=seen.append)
                self.assertEqual(seen, ["开始"])
                self.assertEqual(caught.exception.wire["status"], "error")
                response = caught.exception.wire["response"]
                self.assertEqual(response["http_status"], 200)
                self.assertEqual(set(response), {"http_status", "error"})
                self.assertNotIn("pending-tool-arg", response["error"])

    def test_thinking_signal_precedes_text_once_and_carries_no_payload(self):
        events = Mock()

        def frames():
            yield {"message": {"thinking": "", "content": ""}}
            self.assertEqual(events.mock_calls, [])
            yield {"message": {"thinking": "私有片段一", "content": "开始"}}
            self.assertEqual(events.mock_calls, [call.thinking(), call.text("开始")])
            yield {"message": {"thinking": "私有片段二", "content": "读取"}, "done": True}

        self.conn.getresponse.return_value = FrameStream(frames())
        answer = chat("model", "task", [], [], 3, on_text=events.text, on_thinking=events.thinking)
        self.assertEqual(events.mock_calls, [call.thinking(), call.text("开始"), call.text("读取")])
        self.assertEqual(answer["thinking"], "私有片段一私有片段二")
        self.assertNotIn("thinking", answer.wire["response"]["message"])

    def test_thinking_callback_alone_streams_and_fires_on_first_thinking_frame(self):
        signal = Mock()

        def frames():
            yield {"message": {"thinking": "一"}}
            signal.assert_called_once_with()
            yield {"message": {"thinking": "二"}}
            yield {"message": {"content": "答案"}, "done": True}

        self.conn.getresponse.return_value = FrameStream(frames())
        answer = chat("model", "task", [], [], 3, on_thinking=signal)
        self.assertTrue(answer.wire["request"]["stream"])
        self.assertEqual(answer["content"], "答案")
        signal.assert_called_once_with()

    def test_absent_empty_or_late_thinking_does_not_signal(self):
        for tail in [{}, {"thinking": ""}, {"thinking": "正文之后"}]:
            with self.subTest(tail=tail):
                self.conn.getresponse.return_value = FrameStream([
                    {"message": {"content": "答案"}}, {"message": tail, "done": True},
                ])
                signal = Mock()
                chat("model", "task", [], [], 3, on_thinking=signal)
                signal.assert_not_called()

    def test_tool_only_stream_with_metadata_only_terminal_frame(self):
        call = {"function": {"name": "web_search", "arguments": {"query": "ollama"}}}
        self.conn.getresponse.return_value = FrameStream([
            {"message": {"role": "assistant", "tool_calls": [call]}, "done": False},
            {"done": True, "done_reason": "stop", "eval_count": 7},
        ])
        answer = chat("model", "联网搜索 ollama", [], ["web_search"], 3,
                      on_text=self.fail, on_thinking=self.fail)
        self.assertEqual(answer["tool_calls"], [call])
        self.assertEqual(answer.wire["response"]["eval_count"], 7)
        tool, = json.loads(self.conn.request.call_args.args[2])["tools"]
        self.assertEqual(tool["function"]["name"], "web_search")
        schema = tool["function"]["parameters"]
        self.assertEqual(schema["required"], ["query"])
        self.assertEqual(set(schema["properties"]), {"query"})
        self.assertEqual(schema["properties"]["query"]["type"], "string")

    def test_continuous_frames_respect_the_total_deadline(self):
        now = 10

        def frames():
            nonlocal now
            for text in ("a", "b", "c", "d"):
                now += 1
                yield {"message": {"role": "assistant", "content": text}, "done": text == "d"}

        response = FrameStream(frames())
        self.conn.getresponse.return_value = response
        seen = []
        with patch("ollama_chat.time.monotonic", side_effect=lambda: now):
            with self.assertRaisesRegex(ModelCallError, "时限") as caught:
                chat("model", "task", [], [], 3, on_text=seen.append)
        self.assertEqual(seen, ["a", "b"])
        self.assertTrue(response.closed)
        self.assertTrue(caught.exception.wire["request"]["stream"])
        self.assertEqual(caught.exception.wire["response"]["http_status"], 200)
        self.conn.close.assert_called_once()


class SnapshotTest(unittest.TestCase):
    def test_redaction_and_input_independence(self):
        source = {"args": [{"images": ["private-media"], "reasoning_content": "private-thought",
                            "Authorization": "Basic private-auth", "password": "private-pass",
                            "text": "before data:image/png;base64,cHJpdmF0ZQ== after",
                            "body": "<think>private-thought</think>public"}], "count": 3}
        before = copy.deepcopy(source)
        result = snapshot(source)
        rendered = json.dumps(result, allow_nan=False)
        for secret in ["private-", "cHJpdmF0ZQ"]:
            self.assertNotIn(secret, rendered)
        self.assertIn("public", rendered)
        self.assertIn("after", rendered)
        self.assertEqual(result["count"], 3)
        result["args"].clear()
        self.assertEqual(source, before)

    def test_json_embedded_in_tool_output_and_legacy_feedback(self):
        data = {"source": {"type": "base64", "data": "private-image"}, "note": "public",
                "thinking": "private-thought", "headers": {"X-Test": "private-header"},
                "env": {"DEMO": "private-env"}, "api_key": "private-key"}
        for prefix in ("", "此前反馈\n"):
            rendered = snapshot(prefix + json.dumps(data))
            self.assertNotIn("private-", rendered)
            self.assertTrue(rendered.startswith(prefix))
            self.assertEqual(json.loads(rendered[len(prefix):])["note"], "public")

    def test_long_values_and_collections_mark_truncation(self):
        for value in ["hello " * 20000, list(range(20000)), {str(i): i for i in range(20000)}]:
            with self.subTest(kind=type(value)):
                rendered = json.dumps(snapshot(value), ensure_ascii=False)
                self.assertIn("truncated", rendered)
                self.assertLess(len(rendered), 20000)
