import pytest
from app.router import Backend, Classification, NoEligibleBackend, Request, route

VLLM = Backend("vllm-qwen", "http://localhost:8001/v1", "Qwen/Qwen2.5-0.5B-Instruct",
               max_context=2048, max_classification=Classification.RESTRICTED,
               cost_per_hour_milli_usd=520, ttft_p95_ms=800,
               capabilities=frozenset({"chat", "json"}))
OLLAMA = Backend("ollama-phi4", "http://localhost:11434/v1", "phi4-mini",
                 max_context=4096, max_classification=Classification.INTERNAL,
                 cost_per_hour_milli_usd=0, ttft_p95_ms=1500,
                 capabilities=frozenset({"chat"}))
POOL = [VLLM, OLLAMA]


def test_cost_is_last_tiebreak_not_first_filter():
    d = route(Request("hello", classification=Classification.INTERNAL), POOL)
    assert d.backend.name == "ollama-phi4"   # free wins at equal capability


def test_restricted_never_reaches_a_lower_ceiling_backend():
    d = route(Request("secret", classification=Classification.RESTRICTED), POOL)
    assert d.backend.name == "vllm-qwen"
    assert "ollama-phi4" in d.rejected


def test_capability_beats_cost():
    d = route(Request("x", required_capabilities=frozenset({"json"})), POOL)
    assert d.backend.name == "vllm-qwen"     # free backend lacks json mode


def test_context_headroom_enforced():
    long_prompt = "word " * 3000          # ~3750 tokens -> needs ~4875 ctx
    with pytest.raises(NoEligibleBackend):
        route(Request(long_prompt), POOL)


def test_latency_budget_excludes_slow_backend():
    d = route(Request("hi", latency_budget_ms=1000), POOL)
    assert d.backend.name == "vllm-qwen"     # ollama p95 1500ms is over budget


def test_gate_raises_never_downgrades():
    with pytest.raises(NoEligibleBackend):
        route(Request("x", required_capabilities=frozenset({"vision"})), POOL)
