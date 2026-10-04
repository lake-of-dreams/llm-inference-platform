#!/usr/bin/env python3
"""End-to-end verification against real backends. No mocks.

Needs vLLM on :8001 (hack/serve-vllm.sh) and Ollama on :11434, both live.
"""
from __future__ import annotations

import pathlib
import sys
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "gateway"))

from app.cache import SemanticCache, cosine, embed
from app.client import Ledger, complete
from app.router import Backend, Classification, NoEligibleBackend, Request, route

VLLM = Backend(
    name="vllm-qwen", base_url="http://localhost:8001/v1", model="qwen-small",
    max_context=2048, max_classification=Classification.RESTRICTED,
    cost_per_hour_milli_usd=520,           # ~$0.52/h for this GPU class
    ttft_p95_ms=800, capabilities=frozenset({"chat", "json"}))

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

print("\n=== 3. Semantic cache false-hit guard (live) ===")
# Measure the pair first. If the threshold rejects the near-miss on its own then
# this check passes for the wrong reason and proves nothing about the guard.
q1 = "what is the balance for account 118"
q2 = "what is the balance for account 118 please"   # same account, above threshold
q3 = "what is the balance for account 811"          # different account

dec = route(Request(q1), POOL)
a1, rec = complete(dec.backend, q1, max_tokens=24)
ledger.add(rec)
cache.put(q1, a1)

check("identical prompt served from cache", cache.get(q1) == a1)

sim_same = cosine(embed(q1), embed(q2))
check("paraphrase sits above threshold (guard is the active control)",
      sim_same >= cache.threshold, f"cosine={sim_same:.4f} >= {cache.threshold}")
check("true paraphrase IS served from cache", cache.get(q2) == a1,
      f"cosine={sim_same:.4f}")

# Drop the threshold below the measured similarity of the pair, so the guard is
# the only thing left that can stop the near-miss.
sim_diff = cosine(embed(q1), embed(q3))
probe = SemanticCache(threshold=sim_diff - 0.01)
probe.put(q1, a1)
before = probe.false_hits_blocked
served = probe.get(q3)
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

print("\n" + "=" * 62)
if failures:
    print(f"FAILED: {len(failures)} check(s): {failures}")
    sys.exit(1)
print("All checks passed against real vLLM and real Ollama.")
