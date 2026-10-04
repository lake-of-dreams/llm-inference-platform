"""OpenAI-compatible client with per-call cost and latency attribution.

vLLM and Ollama both speak that surface, so one client does both. Every call
lands in the ledger, otherwise "what did this feature cost last month" is a
reconstruction job off provider invoices.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .router import Backend


@dataclass
class CallRecord:
    backend: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    ttft_ms: int | None
    cost_milli_usd: float
    cached: bool = False


@dataclass
class Ledger:
    """Per-call cost records, aggregated per backend."""
    records: list[CallRecord] = field(default_factory=list)

    def add(self, r: CallRecord) -> None:
        self.records.append(r)

    def total_milli_usd(self) -> float:
        return sum(r.cost_milli_usd for r in self.records)

    def by_backend(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for r in self.records:
            b = out.setdefault(r.backend, {"calls": 0, "tokens": 0, "milli_usd": 0.0, "ms": 0})
            b["calls"] += 1
            b["tokens"] += r.prompt_tokens + r.completion_tokens
            b["milli_usd"] += r.cost_milli_usd
            b["ms"] += r.latency_ms
        return out


def complete(backend: Backend, prompt: str, max_tokens: int = 96,
             timeout: int = 120) -> tuple[str, CallRecord]:
    payload = json.dumps({
        "model": backend.model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }).encode()
    req = urllib.request.Request(
        f"{backend.base_url.rstrip('/')}/chat/completions",
        data=payload, headers={"Content-Type": "application/json"}, method="POST")

    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.load(resp)
    latency_ms = int((time.perf_counter() - t0) * 1000)

    text = body["choices"][0]["message"]["content"].strip()
    usage = body.get("usage") or {}
    pt = int(usage.get("prompt_tokens") or 0)
    ct = int(usage.get("completion_tokens") or 0)

    # Self-hosted is billed in GPU-hours, not tokens, so amortise the replica's
    # hourly rate over the wall clock this call held it. Over-counts when several
    # calls share a replica, but it does attribute per call.
    # TODO: divide by in-flight count once the gateway tracks it.
    cost = backend.cost_per_hour_milli_usd * (latency_ms / 3_600_000.0)

    return text, CallRecord(
        backend=backend.name, model=backend.model,
        prompt_tokens=pt, completion_tokens=ct,
        latency_ms=latency_ms, ttft_ms=None, cost_milli_usd=cost)
