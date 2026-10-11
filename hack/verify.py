#!/usr/bin/env python3
"""End-to-end verification against real backends. No mocks.

Needs vLLM on :8001 (hack/serve-vllm.sh) and Ollama on :11434, both live.
"""
from __future__ import annotations

import dataclasses
import pathlib
import secrets
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "gateway"))

from app.cache import CacheScope, SemanticCache, cosine, embed, prefix_cache_salt
from app.client import Ledger, complete
from app.router import Backend, Classification, NoEligibleBackend, Request, route

VLLM = Backend(
    name="vllm-qwen", base_url="http://localhost:8001/v1", model="qwen-small",
    max_context=2048, max_classification=Classification.RESTRICTED,
    cost_per_hour_milli_usd=520,           # ~$0.52/h for this GPU class
    ttft_p95_ms=800, capabilities=frozenset({"chat", "json"}), prefix_cache_salt=True)

OLLAMA = Backend(
    name="ollama-phi4", base_url="http://localhost:11434/v1", model="phi4-mini",
    max_context=4096, max_classification=Classification.INTERNAL,
    cost_per_hour_milli_usd=0,             # already-owned local capacity
    ttft_p95_ms=1500, capabilities=frozenset({"chat"}))

POOL = [VLLM, OLLAMA]
ledger = Ledger()
cache = SemanticCache(threshold=0.93)
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))
    if not ok:
        failures.append(name)


def reachable(url: str) -> bool:
    try:
        urllib.request.urlopen(url, timeout=3).read()
        return True
    except Exception:
        return False


print("\n=== 0. Backend reachability (no mocks) ===")
v_up = reachable("http://localhost:8001/v1/models")
o_up = reachable("http://localhost:11434/api/tags")
check("vLLM reachable on :8001", v_up)
check("Ollama reachable on :11434", o_up)
if not (v_up and o_up):
    print("\nBoth backends must be live. Aborting.")
    sys.exit(1)

print("\n=== 1. Routing decisions ===")
d = route(Request("Summarise this ticket.", classification=Classification.INTERNAL), POOL)
check("internal traffic routes to free backend", d.backend.name == "ollama-phi4", d.reason)

d = route(Request("Payroll record.", classification=Classification.RESTRICTED), POOL)
check("restricted traffic never leaves approved backend",
      d.backend.name == "vllm-qwen", d.rejected.get("ollama-phi4", ""))

d = route(Request("Emit JSON.", required_capabilities=frozenset({"json"})), POOL)
check("capability outranks cost", d.backend.name == "vllm-qwen", d.reason)

try:
    route(Request("x", required_capabilities=frozenset({"vision"})), POOL)
    check("gate raises rather than downgrading", False)
except NoEligibleBackend:
    check("gate raises rather than downgrading", True, "NoEligibleBackend")

print("\n=== 2. Real inference through the router ===")
for label, req in [
    ("internal", Request("Reply with exactly: OK", classification=Classification.INTERNAL)),
    ("restricted", Request("Reply with exactly: OK", classification=Classification.RESTRICTED)),
]:
    dec = route(req, POOL)
    text, rec = complete(dec.backend, req.prompt, max_tokens=16)
    ledger.add(rec)
    check(f"{label} -> {dec.backend.name} returned text",
          len(text) > 0, f"{rec.latency_ms}ms, {rec.completion_tokens} tok, '{text[:40]}'")
    check(f"{label} -> {dec.backend.name} measured time to first token",
          rec.ttft_ms is not None and rec.ttft_ms <= rec.latency_ms,
          f"ttft={rec.ttft_ms}ms of {rec.latency_ms}ms")

print("\n=== 3. Semantic cache false-hit guard (live) ===")
# Measure the pair first. If the threshold rejects the near-miss on its own then
# this check passes for the wrong reason and proves nothing about the guard.
q1 = "what is the balance for account 118"
q2 = "what is the balance for account 118 please"   # same account, above threshold
q3 = "what is the balance for account 811"          # different account

dec = route(Request(q1), POOL)
a1, rec = complete(dec.backend, q1, max_tokens=24)
ledger.add(rec)
scope = CacheScope.of(Classification.INTERNAL, dec.backend.model, temperature=0.2, max_tokens=24)
cache.put(q1, a1, scope)

check("identical prompt served from cache", cache.get(q1, scope) == a1)
check("the same prompt from a Public caller is NOT served a cached Internal answer",
      cache.get(q1, CacheScope.of(Classification.PUBLIC, dec.backend.model,
                                  temperature=0.2, max_tokens=24)) is None)

sim_same = cosine(embed(q1), embed(q2))
check("paraphrase sits above threshold (guard is the active control)",
      sim_same >= cache.threshold, f"cosine={sim_same:.4f} >= {cache.threshold}")
check("true paraphrase IS served from cache", cache.get(q2, scope) == a1,
      f"cosine={sim_same:.4f}")

# Drop the threshold below the measured similarity of the pair, so the guard is
# the only thing left that can stop the near-miss.
sim_diff = cosine(embed(q1), embed(q3))
probe = SemanticCache(threshold=sim_diff - 0.01)
probe.put(q1, a1, scope)
before = probe.false_hits_blocked
served = probe.get(q3, scope)
check("numeric near-miss blocked BY THE GUARD, not the threshold",
      served is None and probe.false_hits_blocked == before + 1,
      f"cosine={sim_diff:.4f} > threshold={probe.threshold:.4f}, "
      f"false_hits_blocked={probe.false_hits_blocked}")

print("\n=== 4. Per-call cost attribution ===")
for name, agg in ledger.by_backend().items():
    print(f"  {name:<14} calls={int(agg['calls'])} tokens={int(agg['tokens'])} "
          f"ms={int(agg['ms'])} cost={agg['milli_usd']:.4f} m$")
check("ledger attributed every call", len(ledger.records) >= 3, f"{len(ledger.records)} records")
check("free backend attributed zero cost",
      ledger.by_backend().get("ollama-phi4", {}).get("milli_usd", 1) == 0.0)

print("\n=== 5. Prefix cache salt (live, vLLM) ===")
# vLLM counts prefix-cache lookups and hits in tokens. Read the counters around
# each call: a hit count is objective, a faster first token is only suggestive.
def prefix_hits() -> float:
    with urllib.request.urlopen("http://localhost:8001/metrics", timeout=5) as resp:
        text = resp.read().decode()
    return sum(float(line.rsplit(" ", 1)[1]) for line in text.splitlines()
               if line.startswith("vllm:prefix_cache_hits_total"))


key = secrets.token_bytes(32)
salt_hr = prefix_cache_salt(key, Classification.RESTRICTED, "hr")
salt_contracts = prefix_cache_salt(key, Classification.RESTRICTED, "contracts")
# a fresh nonce, so nothing cached by an earlier run can hit
shared = (f"[{secrets.token_hex(8)}] " + "Payroll summary instructions: list each employee's "
          "grade, base salary, overtime hours and deductions, then total them by department. " * 12)
hits = []
for salt, ask in [(salt_hr, "Summarise for March."), (salt_hr, "Summarise for April."),
                  (salt_contracts, "Summarise for May.")]:
    before = prefix_hits()
    _, rec = complete(VLLM, shared + ask, max_tokens=8, cache_salt=salt)
    hits.append((prefix_hits() - before, rec.ttft_ms))
    ledger.add(rec)
(first, t1), (same, t2), (other, t3) = hits
print(f"  prompt about {len(shared) // 4} tokens; hits per call (tokens): "
      f"first={first:.0f} same-group={same:.0f} other-group={other:.0f}; "
      f"TTFT ms {t1}, {t2}, {t3}")
check("the same group reuses its cached prefix", same >= len(shared) // 8,
      f"{same:.0f} tokens served from cache")
check("another group with the same text gets no cache hit", other == 0,
      f"{other:.0f} tokens served from cache")

# The control. If vLLM never shared a prefix between requests at all, the check
# above would pass for the wrong reason. Unsalted, the same text must hit.
plain = f"[{secrets.token_hex(8)}] " + shared.split("] ", 1)[1]
complete(VLLM, plain + "Summarise for June.", max_tokens=8)
before = prefix_hits()
complete(VLLM, plain + "Summarise for July.", max_tokens=8)
unsalted = prefix_hits() - before
check("control: without a salt, a second caller's identical text does hit", unsalted > 0,
      f"{unsalted:.0f} tokens served from cache, so the leak the salt closes is real")

print("\n=== 6. Cost per token from measured throughput (live, vLLM) ===")
# A small load test: 8 calls at once, 64 tokens each. The busy rate is what one
# replica sustains, which is what cost per token needs (ADR-0005).
#
# The first burst on a fresh server measured 92 tokens/s and the next 434: the
# first concurrent batch pays one-off start-up work. One cold burst would have
# priced this GPU nearly five times too high. So: one warm-up burst, not
# counted, then three measured, and the median is the figure.
def burst(n: int = 8, max_tokens: int = 64):
    qs = [f"Write a short paragraph about filing deadline number {i}." for i in range(n)]
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=n) as pool:
        rs = [r for _, r in pool.map(lambda q: complete(VLLM, q, max_tokens=max_tokens), qs)]
    return rs, time.perf_counter() - t0


warm, warm_s = burst()
print(f"  warm-up, not counted: {sum(r.completion_tokens for r in warm)} tokens in {warm_s:.1f}s")
rates, recs = [], []
for _ in range(3):
    rs, secs = burst()
    recs += rs
    rates.append(sum(r.completion_tokens for r in rs) / secs)
for r in warm + recs:
    ledger.add(r)
tps = sorted(rates)[1]
measured = dataclasses.replace(VLLM, tokens_per_second=tps)
print(f"  three measured bursts: {', '.join(f'{x:.0f}' for x in rates)} tokens/s; median {tps:.0f}")
print(f"  at {VLLM.cost_per_hour_milli_usd} m$/h that is "
      f"{measured.cost_per_mtok_milli_usd:.0f} m$ per million tokens")
check("busy throughput measured", tps > 0, f"{tps:.0f} tokens/s")
check("measured bursts agree within a factor of two", max(rates) <= 2 * min(rates),
      f"{min(rates):.0f} to {max(rates):.0f} tokens/s")
d = route(Request("Payroll record.", classification=Classification.RESTRICTED), [measured, OLLAMA])
check("router prices vLLM per token once throughput is known", "m$/Mtok" in d.reason, d.reason)
check("shared calls were charged a share of the hour",
      max(r.concurrency for r in recs) > 1.0,
      f"max concurrency {max(r.concurrency for r in recs):.1f}")

print("\n" + "=" * 62)
if failures:
    print(f"FAILED: {len(failures)} check(s): {failures}")
    sys.exit(1)
print("All checks passed against real vLLM and real Ollama.")
