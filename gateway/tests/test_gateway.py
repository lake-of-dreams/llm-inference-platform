import json

import pytest
from app.cache import prefix_cache_salt
from app.client import InFlight
from app.gateway import Gateway
from app.router import Backend, Classification, NoEligibleBackend, Request

KEY = b"gateway-secret-for-tests"


def backend_at(url: str, salt: bool = True) -> Backend:
    return Backend("vllm-test", url + "/v1", "qwen-small", max_context=4096,
                   max_classification=Classification.RESTRICTED, cost_per_hour_milli_usd=3600,
                   prefix_cache_salt=salt)


def answering(text: str, seen: list):
    def handle(h):
        seen.append(json.loads(h.rfile.read(int(h.headers["Content-Length"]))))
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.end_headers()
        for event in ({"choices": [{"index": 0, "delta": {"content": text}}]},
                      {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 3}}):
            h.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
        h.wfile.write(b"data: [DONE]\n\n")
    return handle


def gateway(url: str, **kw) -> Gateway:
    return Gateway([backend_at(url, **kw)], salt_key=KEY, in_flight=InFlight())


def test_a_request_without_a_tenant_is_refused_before_any_call(http_server):
    seen = []
    gw = gateway(http_server(answering("x", seen)))
    with pytest.raises(ValueError, match="tenant"):
        gw.handle(Request("hello"), tenant="")
    assert seen == [] and gw.ledger.records == []
    # refused before routing, not after: a request the router would also refuse
    # still fails on the missing tenant
    with pytest.raises(ValueError, match="tenant"):
        gw.handle(Request("x", required_capabilities=frozenset({"vision"})), tenant="")


def test_the_tenants_salt_reaches_the_model_server(http_server):
    seen = []
    gw = gateway(http_server(answering("x", seen)))
    gw.handle(Request("hello", classification=Classification.RESTRICTED), tenant="hr")
    assert seen[0]["cache_salt"] == prefix_cache_salt(KEY, Classification.RESTRICTED, "hr")


def test_a_repeat_from_the_same_tenant_is_served_from_cache_at_no_cost(http_server):
    seen = []
    gw = gateway(http_server(answering("Payroll is 42", seen)))
    first = gw.handle(Request("summarise the payroll file"), tenant="hr")
    again = gw.handle(Request("summarise the payroll file"), tenant="hr")
    assert len(seen) == 1                        # the model was asked once
    assert again.text == first.text and again.cached
    assert again.record.cost_milli_usd == 0 and again.record.tenant == "hr"


def test_another_tenants_cached_answer_is_never_served(http_server):
    """The answer cache was scoped by classification and model, not by tenant.
    HR's Internal answer was served to a contracts user who sent the same words."""
    seen = []
    gw = gateway(http_server(answering("Payroll is 42", seen)))
    gw.handle(Request("summarise the payroll file"), tenant="hr")
    other = gw.handle(Request("summarise the payroll file"), tenant="contracts")
    assert not other.cached
    assert len(seen) == 2                        # contracts' request went to the model


def test_cost_is_attributed_to_the_tenant(http_server):
    seen = []
    gw = gateway(http_server(answering("x", seen)))
    gw.handle(Request("one"), tenant="hr")
    gw.handle(Request("two"), tenant="contracts")
    gw.handle(Request("two"), tenant="contracts")   # cached, free
    by = gw.ledger.by_tenant()
    assert by["hr"]["calls"] == 1 and by["contracts"]["calls"] == 2
    assert by["contracts"]["cached"] == 1


def test_routing_refusal_records_nothing(http_server):
    seen = []
    gw = gateway(http_server(answering("x", seen)))
    with pytest.raises(NoEligibleBackend):
        gw.handle(Request("x", required_capabilities=frozenset({"vision"})), tenant="hr")
    assert seen == [] and gw.ledger.records == []
