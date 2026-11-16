import json

import pytest
from app.client import BackendError, InFlight
from app.gateway import Gateway
from app.router import Backend, Classification, Request
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

KEY = b"gateway-secret-for-tests"
SECRET = "the claimant's salary is 61,250"

_exporter = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(_exporter))
trace.set_tracer_provider(_provider)


@pytest.fixture
def spans():
    _exporter.clear()
    return _exporter.get_finished_spans


def answering(text: str, seen: list):
    def handle(h):
        seen.append((dict(h.headers), json.loads(h.rfile.read(int(h.headers["Content-Length"])))))
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.end_headers()
        for event in ({"choices": [{"index": 0, "delta": {"content": text}}]},
                      {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 4}}):
            h.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
        h.wfile.write(b"data: [DONE]\n\n")
    return handle


def gateway(url: str, objective: str = "") -> Gateway:
    b = Backend("vllm-hr", url + "/v1", "qwen-small", max_context=4096,
                max_classification=Classification.RESTRICTED, cost_per_hour_milli_usd=3600,
                prefix_cache_salt=True, objective=objective)
    return Gateway([b], salt_key=KEY, in_flight=InFlight())


def test_a_backend_call_is_one_span_with_tokens_backend_and_tenant(http_server, spans):
    gw = gateway(http_server(answering("It is.", [])))
    gw.handle(Request(SECRET, Classification.RESTRICTED), tenant="hr")

    (span,) = spans()
    a = span.attributes
    assert span.name == "chat qwen-small"
    assert a["gen_ai.operation.name"] == "chat" and a["gen_ai.request.model"] == "qwen-small"
    assert a["gen_ai.usage.input_tokens"] == 9 and a["gen_ai.usage.output_tokens"] == 4
    assert a["server.address"] == "127.0.0.1" and a["server.port"] > 0
    assert a["gateway.backend"] == "vllm-hr" and a["gateway.tenant"] == "hr"
    assert a["gateway.classification"] == "RESTRICTED" and a["gateway.cached"] is False


def test_neither_the_prompt_nor_the_answer_reaches_the_trace(http_server, spans):
    gw = gateway(http_server(answering("Their salary is 61,250.", [])))
    gw.handle(Request(SECRET, Classification.RESTRICTED), tenant="hr")
    gw.handle(Request(SECRET, Classification.RESTRICTED), tenant="hr")  # served from cache

    recorded = json.dumps([[s.name, dict(s.attributes), [e.name for e in s.events]]
                           for s in spans()])
    assert "61,250" not in recorded and "salary" not in recorded


def test_a_cache_hit_is_a_span_marked_cached_with_no_tokens(http_server, spans):
    gw = gateway(http_server(answering("It is.", [])))
    gw.handle(Request("opening hours?"), tenant="web")
    gw.handle(Request("opening hours?"), tenant="web")

    first, second = spans()
    assert first.attributes["gateway.cached"] is False
    assert second.attributes["gateway.cached"] is True
    assert "gen_ai.usage.output_tokens" not in second.attributes


def test_a_failed_backend_call_sets_error_type(http_server, spans):
    def broken(h):
        h.rfile.read(int(h.headers["Content-Length"]))
        h.send_response(503)
        h.end_headers()
    gw = gateway(http_server(broken))
    with pytest.raises(BackendError):
        gw.handle(Request("hello"), tenant="web")

    (span,) = spans()
    assert span.attributes["error.type"] == "BackendError"
    assert span.status.status_code.name == "ERROR"


def test_objective_and_fairness_headers_go_only_to_a_backend_behind_llm_d(http_server):
    seen: list = []
    url = http_server(answering("ok", seen))
    gateway(url, objective="qwen-small").handle(Request("a"), tenant="contracts")
    gateway(url).handle(Request("b"), tenant="contracts")

    with_llmd = {k.lower(): v for k, v in seen[0][0].items()}
    plain = {k.lower(): v for k, v in seen[1][0].items()}
    assert with_llmd["x-llm-d-inference-objective"] == "qwen-small"
    assert with_llmd["x-llm-d-inference-fairness-id"] == "contracts"
    assert not any(k.startswith("x-llm-d-") for k in plain)


def test_the_backend_is_told_which_trace_the_call_belongs_to(http_server, spans):
    seen: list = []
    gw = gateway(http_server(answering("ok", seen)))
    gw.handle(Request("hello"), tenant="web")

    (span,) = spans()
    ctx = span.get_span_context()
    sent = {k.lower(): v for k, v in seen[0][0].items()}
    # version, trace id, parent span id, flags. vLLM parents its spans on this.
    version, trace_id, parent_id, flags = sent["traceparent"].split("-")
    assert (version, trace_id, parent_id) == ("00", f"{ctx.trace_id:032x}", f"{ctx.span_id:016x}")
    assert int(flags, 16) & 1, "the sampled bit tells the backend to record its side too"


def test_a_call_outside_any_span_sends_no_trace_header(http_server):
    from app.client import complete
    seen: list = []
    url = http_server(answering("ok", seen))
    b = Backend("vllm", url + "/v1", "qwen-small", max_context=4096,
                max_classification=Classification.INTERNAL, cost_per_hour_milli_usd=0)
    complete(b, "hello", in_flight=InFlight())
    assert "traceparent" not in {k.lower() for k in seen[0][0]}
