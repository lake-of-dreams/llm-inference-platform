#!/usr/bin/env python3
"""A pass-through proxy that counts tokens for a model server with no metrics of its own.

Ollama publishes nothing Prometheus can read, so a dashboard shows its pod's
CPU and memory and no tokens. This sits in front of its OpenAI-compatible API,
forwards every request unchanged, and reads the token counts out of the
`usage` object each answer ends with. It asks for that object on streamed
answers, where a server sends it only on request.

It counts; it never stores or logs a prompt or an answer.

    UPSTREAM=http://phi4-mini.llm-platform.svc.cluster.local:8000  python meter.py
    GET /metrics on the same port, 8000
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ["UPSTREAM"].rstrip("/")
PORT = int(os.getenv("PORT", "8000"))
HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "content-length", "host",
              "content-encoding", "accept-encoding"}

lock = threading.Lock()
counters: dict[tuple[str, str], float] = defaultdict(float)
running: dict[str, int] = defaultdict(int)


def add(name: str, model: str, value: float = 1.0) -> None:
    with lock:
        counters[(name, model)] += value


def metrics_text() -> bytes:
    help_ = {
        "token_meter_requests_total": "Chat requests forwarded.",
        "token_meter_errors_total": "Requests the upstream failed or refused.",
        "token_meter_prompt_tokens_total": "Prompt tokens the upstream reported reading.",
        "token_meter_generation_tokens_total": "Tokens the upstream reported writing.",
        "token_meter_request_seconds_total": "Seconds spent on finished requests.",
        "token_meter_first_token_seconds_total": "Seconds to the first streamed text, summed.",
        "token_meter_first_token_count_total": "Streamed answers that produced text.",
    }
    out = []
    with lock:
        for name, text in help_.items():
            out += [f"# HELP {name} {text}", f"# TYPE {name} counter"]
            out += [f'{name}{{model_name="{m}"}} {v}' for (n, m), v in sorted(counters.items()) if n == name]
        out += ["# HELP token_meter_requests_running Requests in flight.",
                "# TYPE token_meter_requests_running gauge"]
        out += [f'token_meter_requests_running{{model_name="{m}"}} {v}' for m, v in sorted(running.items())]
    return ("\n".join(out) + "\n").encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"  # one request per connection keeps streaming simple

    def log_message(self, *args) -> None:  # request lines would be the only log; not needed
        pass

    def do_GET(self) -> None:
        if self.path == "/metrics":
            body = metrics_text()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.forward(None)

    def do_POST(self) -> None:
        self.forward(self.rfile.read(int(self.headers.get("Content-Length", 0))))

    def forward(self, body: bytes | None) -> None:
        model, metered = "", False
        if body and self.path.rstrip("/").endswith("/chat/completions"):
            try:
                payload = json.loads(body)
                model = str(payload.get("model", "")).split(":")[0]
                if payload.get("stream"):
                    payload.setdefault("stream_options", {})["include_usage"] = True
                    body = json.dumps(payload).encode()
                metered = True
            except (ValueError, AttributeError):
                pass  # not ours to judge; the upstream will answer it

        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        req = urllib.request.Request(UPSTREAM + self.path, data=body, headers=headers,
                                     method=self.command)
        t0 = time.monotonic()
        if metered:
            add("token_meter_requests_total", model)
            with lock:
                running[model] += 1
        try:
            try:
                resp = urllib.request.urlopen(req, timeout=600)
            except urllib.error.HTTPError as e:
                resp = e
                if metered:
                    add("token_meter_errors_total", model)
            self.send_response(resp.status)
            for k, v in resp.headers.items():
                if k.lower() not in HOP_BY_HOP:
                    self.send_header(k, v)
            self.end_headers()
            first = None
            for line in resp:  # one line at a time, so a streamed answer is not held back
                self.wfile.write(line)
                self.wfile.flush()
                if metered:
                    first = self.read_usage(line, model, t0, first)
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if metered:
                add("token_meter_errors_total", model)
        finally:
            if metered:
                add("token_meter_request_seconds_total", model, time.monotonic() - t0)
                with lock:
                    running[model] -= 1

    @staticmethod
    def read_usage(line: bytes, model: str, t0: float, first: float | None) -> float | None:
        text = line.decode("utf-8", "replace").strip()
        if text.startswith("data:"):
            text = text[5:].strip()
        if not text.startswith("{"):
            return first
        try:
            event = json.loads(text)
        except ValueError:
            return first
        if first is None and any((c.get("delta") or {}).get("content") for c in event.get("choices") or []):
            first = time.monotonic() - t0
            add("token_meter_first_token_seconds_total", model, first)
            add("token_meter_first_token_count_total", model)
        usage = event.get("usage")
        if usage:
            add("token_meter_prompt_tokens_total", model, usage.get("prompt_tokens") or 0)
            add("token_meter_generation_tokens_total", model, usage.get("completion_tokens") or 0)
        return first


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
