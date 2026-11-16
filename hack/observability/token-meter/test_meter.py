"""The meter against a stand-in upstream: it forwards unchanged and counts what the upstream reports."""
import importlib
import json
import os
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SECRET = "the salary is 61,250"
seen: list[dict] = []


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        seen.append(body)
        self.send_response(200)
        if body.get("stream"):
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            events = [{"choices": [{"delta": {"content": "Hel"}}]}, {"choices": [{"delta": {"content": "lo"}}]}]
            if (body.get("stream_options") or {}).get("include_usage"):
                events.append({"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 2}})
            for e in events:
                self.wfile.write(f"data: {json.dumps(e)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            out = json.dumps({"choices": [{"message": {"content": "Hello"}}],
                              "usage": {"prompt_tokens": 7, "completion_tokens": 3}}).encode()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(out)


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


_up, UP = serve(Upstream)
os.environ["UPSTREAM"] = UP
meter = importlib.import_module("meter")
_m, URL = serve(meter.Handler)


def post(payload: dict) -> bytes:
    req = urllib.request.Request(URL + "/v1/chat/completions", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=10).read()


def metric(name: str, model: str = "phi4-mini") -> float:
    for line in urllib.request.urlopen(URL + "/metrics", timeout=10).read().decode().splitlines():
        if line.startswith(f'{name}{{model_name="{model}"}}'):
            return float(line.split()[-1])
    return 0.0


def test_a_plain_answer_is_forwarded_and_its_tokens_counted():
    out = json.loads(post({"model": "phi4-mini:latest", "messages": [{"role": "user", "content": "hi"}]}))
    assert out["choices"][0]["message"]["content"] == "Hello"
    assert metric("token_meter_prompt_tokens_total") == 7
    assert metric("token_meter_generation_tokens_total") == 3


def test_a_streamed_answer_is_asked_for_usage_and_counted():
    before = metric("token_meter_generation_tokens_total")
    raw = post({"model": "phi4-mini", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
    assert b'"content": "Hel"' in raw and raw.strip().endswith(b"data: [DONE]")
    assert seen[-1]["stream_options"]["include_usage"] is True
    assert metric("token_meter_generation_tokens_total") - before == 2
    assert metric("token_meter_first_token_count_total") >= 1


def test_no_prompt_or_answer_text_reaches_the_metrics():
    post({"model": "phi4-mini", "messages": [{"role": "user", "content": SECRET}]})
    text = urllib.request.urlopen(URL + "/metrics", timeout=10).read().decode()
    assert "61,250" not in text and "salary" not in text and "Hello" not in text
