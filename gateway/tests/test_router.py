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


def _paid(name, per_hour, tps=0.0, ttft=500):
    return Backend(name, "http://x/v1", "m", max_context=8192,
                   max_classification=Classification.RESTRICTED,
                   cost_per_hour_milli_usd=per_hour, ttft_p95_ms=ttft,
                   tokens_per_second=tps)


def test_same_hourly_price_faster_replica_is_cheaper_per_answer():
    slow, fast = _paid("slow", 2000, tps=300), _paid("fast", 2000, tps=1200)
    d = route(Request("x"), [slow, fast])
    assert d.backend.name == "fast"
    assert "m$/Mtok" in d.reason


def test_dearer_by_the_hour_can_be_cheaper_per_token():
    # 520 m$/h at 100 tok/s is 1444 m$ per million tokens.
    # 800 m$/h at 400 tok/s is 556.
    cheap_hour = _paid("cheap-hour", 520, tps=100)
    cheap_token = _paid("cheap-token", 800, tps=400)
    assert cheap_hour.cost_per_mtok_milli_usd == pytest.approx(1444.4, abs=0.1)
    assert cheap_token.cost_per_mtok_milli_usd == pytest.approx(555.6, abs=0.1)
    assert route(Request("x"), [cheap_hour, cheap_token]).backend.name == "cheap-token"


def test_unknown_throughput_ranks_after_a_priced_backend():
    unknown = _paid("unknown", 100)            # cheap by the hour, cost per answer unknown
    priced = _paid("priced", 3000, tps=2000)
    assert unknown.cost_per_mtok_milli_usd is None
    assert route(Request("x"), [unknown, priced]).backend.name == "priced"


def test_hourly_price_still_decides_when_nothing_is_priced_per_token():
    d = route(Request("x"), [_paid("a", 900), _paid("b", 400)])
    assert d.backend.name == "b"
    assert "throughput unknown" in d.reason


def test_local_backend_is_free_per_token_too():
    assert OLLAMA.cost_per_mtok_milli_usd == 0.0
