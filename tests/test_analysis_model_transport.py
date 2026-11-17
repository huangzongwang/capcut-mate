"""Offline streaming, isolation, failure and compatibility regression tests."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import threading
import time
import unittest
from unittest.mock import Mock, patch

import requests

from src.service.video_analysis import model_client, responses_client as client


def completed(data, **extra):
    return {"type": "response.completed", "response": {
        "id": "resp-test", "status": "completed",
        "output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(data, ensure_ascii=False)},
        ]}], **extra,
    }}


def stream(events, status=200):
    response = Mock(status_code=status, headers={"Content-Type": "text/event-stream"})
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)

    def chunks(**kwargs):
        for event in events:
            if isinstance(event, Exception):
                raise event
            raw = ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode("utf-8")
            for byte in raw:
                yield bytes([byte])
    response.iter_content.side_effect = chunks
    return response


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "id": 13, "base_url": "https://example.invalid/responses",
            "api_key": "secret", "model": "model-a", "purpose": "script",
            "request_timeout_seconds": 60,
        }

    def call(self, events, **kwargs):
        response = stream(events)
        with patch.object(client.requests, "post", return_value=response) as post:
            result = client.configured_request(self.config, {"input": []}, 180, **kwargs)
        response.__exit__.assert_called_once()
        self.assertTrue(post.call_args.kwargs["stream"])
        self.assertTrue(post.call_args.kwargs["json"]["stream"])
        self.assertEqual(post.call_args.kwargs["timeout"][1], 120)
        return result

    def test_final_output_not_duplicated_and_reasoning_not_used(self):
        self.assertEqual(self.call([
            {"type": "response.reasoning_text.delta", "delta": "private reasoning"},
            {"type": "response.output_text.delta", "delta": '{"text":"wrong"}'},
            completed({"text": "中文"}),
        ]), {"text": "中文"})

    def test_delta_order_and_done_fallback(self):
        self.assertEqual(self.call([
            {"type": "response.output_text.delta", "delta": '{"x":'},
            {"type": "response.output_text.delta", "delta": '1}'},
            {"type": "response.output_text.done", "text": '{"x":2}'},
            {"type": "response.completed", "response": {"status": "completed"}},
        ]), {"x": 2})

    def test_disconnect_retries_with_fresh_buffer(self):
        scope = client.Scope(task_id="task-a")
        first = stream([{"type": "response.output_text.delta", "delta": '{"stale":'}])
        second = stream([completed({"ok": True})])
        before = deepcopy(self.config)
        with patch.object(client.requests, "post", side_effect=[first, second]) as post, \
             patch.object(client.time, "sleep"):
            result = client.configured_request(self.config, {}, 180, scope=scope)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(post.call_count, 2)
        self.assertEqual(len(scope.calls), 2)
        self.assertEqual(self.config, before)
        self.assertEqual(scope.calls[0]["outcome"], "StreamInterrupted")

    def test_terminal_errors_do_not_return_partial_json_or_retry(self):
        for terminal in ("response.failed", "response.incomplete", "error"):
            with self.subTest(terminal=terminal), \
                 patch.object(client.requests, "post", return_value=stream([
                     {"type": "response.output_text.delta", "delta": '{"ok":true}'},
                     {"type": terminal},
                 ])) as post:
                with self.assertRaises(client.ModelResponseError):
                    client.configured_request(self.config, {}, 180)
                self.assertEqual(post.call_count, 1)

    def test_read_timeout_retries_bounded(self):
        with patch.object(client.requests, "post", side_effect=requests.ReadTimeout("secret")) as post, \
             patch.object(client.time, "sleep"):
            with self.assertRaises(requests.ReadTimeout) as error:
                client.configured_request(self.config, {}, 180)
        self.assertEqual(post.call_count, 2)
        self.assertNotIn("secret", str(error.exception))

    def test_http_auth_is_not_retried(self):
        with patch.object(client.requests, "post", return_value=stream([], 401)) as post:
            with self.assertRaises(requests.HTTPError):
                client.configured_request(self.config, {}, 180)
        self.assertEqual(post.call_count, 1)

    def test_retryable_http_response_is_closed(self):
        rejected, success = stream([], 429), stream([completed({"ok": 1})])
        with patch.object(client.requests, "post", side_effect=[rejected, success]), \
             patch.object(client.time, "sleep"):
            client.configured_request(self.config, {}, 180)
        rejected.__exit__.assert_called_once()
        success.__exit__.assert_called_once()

    def test_rate_limit_retry_honors_bounded_retry_after(self):
        rejected = stream([], 429)
        rejected.headers["Retry-After"] = "7"
        success = stream([completed({"ok": 1})])
        with patch.object(client.requests, "post", side_effect=[rejected, success]), \
             patch.object(client.time, "sleep") as sleep:
            client.configured_request(self.config, {}, 180)
        sleep.assert_called_once_with(7.0)

    def test_invalid_completed_json_is_not_success(self):
        with self.assertRaises(ValueError):
            self.call([completed([])])

    def test_missing_completed_is_failure(self):
        with patch.object(client.requests, "post", side_effect=lambda *a, **k: stream([])), \
             patch.object(client.time, "sleep"):
            with self.assertRaises(client.StreamInterrupted):
                client.configured_request(self.config, {}, 180)

    def test_expired_scope_does_not_send_request(self):
        with patch.object(client.requests, "post") as post:
            with self.assertRaises(requests.ReadTimeout):
                client.configured_request(self.config, {}, 180, scope=client.Scope(deadline=0))
        post.assert_not_called()

    def test_sse_comments_multiline_crlf_and_utf8(self):
        raw = (
            ': heartbeat\r\n\r\nevent: response.completed\r\n'
            'data: {"type":"response.completed",\r\n'
            'data: "response":{"status":"completed","output_text":"{\\"text\\":\\"中文\\"}"}}\r\n\r\n'
        ).encode("utf-8")
        response = stream([])
        response.iter_content.side_effect = lambda **kw: (bytes([b]) for b in raw)
        with patch.object(client.requests, "post", return_value=response):
            self.assertEqual(client.configured_request(self.config, {}, 180), {"text": "中文"})

    def test_unterminated_event_is_size_limited(self):
        response = stream([])
        response.iter_content.side_effect = lambda **kw: (b"x" for _ in range(100))
        with patch.object(client.requests, "post", return_value=response) as post:
            with self.assertRaises(client.ModelResponseError):
                client.request_json(
                    client.Connection("https://example.invalid", "test", "test"), {},
                    client.Context(), client.Policy(max_bytes=16),
                )
        self.assertEqual(post.call_count, 1)

    def test_total_budget_stops_a_continuing_stream(self):
        response = stream([])
        def slow_chunks(**kwargs):
            time.sleep(0.02)
            yield b":"
        response.iter_content.side_effect = slow_chunks
        with patch.object(client.requests, "post", return_value=response) as post:
            with self.assertRaises(requests.ReadTimeout):
                client.request_json(
                    client.Connection("https://example.invalid", "test", "test"), {},
                    client.Context(), client.Policy(total_seconds=0.01),
                )
        self.assertEqual(post.call_count, 1)
        response.__exit__.assert_called_once()

    def test_parallel_calls_do_not_share_auth_content_metrics_or_callbacks(self):
        barrier = threading.Barrier(2)
        events = [[], []]
        configs = [deepcopy(self.config), {**self.config, "model": "model-b", "api_key": "other"}]
        payload = {"input": [{"role": "user", "content": "shared"}]}
        original = deepcopy(payload)

        def post(url, **kwargs):
            name = kwargs["json"]["model"]
            self.assertEqual(kwargs["headers"]["Authorization"],
                             "Bearer secret" if name == "model-a" else "Bearer other")
            barrier.wait(timeout=3)
            return stream([completed({"name": name})])

        def run(i):
            scope = client.Scope(task_id=f"task-{i}", on_event=events[i].append)
            result = client.configured_request(configs[i], payload, 180, scope=scope,
                                               operation="scene", scene_index=i)
            return result, scope

        with patch.object(client.requests, "post", side_effect=post), ThreadPoolExecutor(2) as pool:
            results = list(pool.map(run, (0, 1)))
        self.assertEqual([r[0]["name"] for r in results], ["model-a", "model-b"])
        self.assertEqual(payload, original)
        self.assertNotEqual(results[0][1].calls[0]["call_id"], results[1][1].calls[0]["call_id"])
        for i in (0, 1):
            self.assertTrue(all(e["task_id"] == f"task-{i}" for e in events[i]))
            self.assertEqual(len(results[i][1].calls), 1)
            self.assertNotIn("_calls", configs[i])
            self.assertNotIn("secret", json.dumps(events[i]))

    def test_video_file_and_global_wrappers_use_common_transport(self):
        with patch.object(client.requests, "post",
                          side_effect=lambda *a, **k: stream([completed({"ok": 1})])) as post:
            model_client.analyze_scene_with_responses(self.config, {"index": 1}, "", "file-1")
            model_client.analyze_global_report_with_responses(self.config, [], "", [])
            model_client.analyze_global_report_from_file(self.config, "file-report")
        self.assertEqual(post.call_count, 3)
        for call in post.call_args_list:
            self.assertTrue(call.kwargs["json"]["stream"])


if __name__ == "__main__":
    unittest.main()
