import json
import time

import pytest
from app.client import BackendError, InFlight, complete
from app.router import Backend, Classification


def backend(url: str, cost: int = 3600) -> Backend:
    return Backend("vllm-test", url + "/v1", "qwen-small", max_context=2048,
                   max_classification=Classification.RESTRICTED, cost_per_hour_milli_usd=cost)


def sse_handler(chunks, delay_before_first=0.0, captured=None):
    def handle(h):
        body = json.loads(h.rfile.read(int(h.headers["Content-Length"])))
        if captured is not None:
            captured.append(body)
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.end_headers()
        time.sleep(delay_before_first)
        for c in chunks:
            h.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            h.wfile.flush()
        h.wfile.write(b"data: [DONE]\n\n")
    return handle


def delta(text):
    return {"choices": [{"index": 0, "delta": {"content": text}}]}


def test_streams_text_and_measures_ttft(http_server):
    sent = []
    url = http_server(sse_handler(
        [delta(""), delta("O"), delta("K"),
         {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 2}}],
        delay_before_first=0.05, captured=sent))
    text, rec = complete(backend(url), "Reply with OK", max_tokens=4)

    assert text == "OK"
    assert sent[0]["stream"] is True and sent[0]["stream_options"] == {"include_usage": True}
    assert rec.prompt_tokens == 9 and rec.completion_tokens == 2
    assert rec.ttft_ms is not None and 40 <= rec.ttft_ms <= rec.latency_ms


def test_cost_is_shared_between_overlapping_calls(http_server):
    url = http_server(sse_handler([delta("x")]))
    flight = InFlight()
    # three other calls already hold the replica and are still running
    for _ in range(3):
        flight.enter("vllm-test")
    _, rec = complete(backend(url), "x", in_flight=flight)

    assert rec.concurrency == 4.0
    alone = 3600 * (rec.latency_ms / 3_600_000.0)
    assert rec.cost_milli_usd == pytest.approx(alone / 4)
    assert flight.current("vllm-test") == 3     # this call has left


def test_free_backend_costs_nothing(http_server):
    url = http_server(sse_handler([delta("x")]))
    _, rec = complete(backend(url, cost=0), "x")
    assert rec.cost_milli_usd == 0.0


def test_http_error_names_the_backend(http_server):
    def handle(h):
        h.send_response(503)
        h.end_headers()
        h.wfile.write(b"overloaded")
    url = http_server(handle)
    flight = InFlight()
    with pytest.raises(BackendError, match="vllm-test returned HTTP 503"):
        complete(backend(url), "x", in_flight=flight)
    assert flight.current("vllm-test") == 0
