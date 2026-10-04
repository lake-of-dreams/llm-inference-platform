"""OpenAI-compatible client with per-call cost and latency attribution.

vLLM and Ollama both speak that API, so one client does both. Every call lands
in the ledger, otherwise "what did this feature cost last month" is a
reconstruction job off provider invoices.

Calls stream. The first piece of generated text marks the time to first token
(TTFT), which is the latency a person waiting on a chat reply actually feels.
A non-streaming call can only measure the whole response.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field

from .router import Backend


class BackendError(RuntimeError):
    """A backend answered with an error or an unreadable response."""


@dataclass
class CallRecord:
    backend: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    ttft_ms: int | None
    cost_milli_usd: float
    # mean number of calls sharing the replica while this one ran
    concurrency: float = 1.0
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


class InFlight:
    """Counts calls in progress per backend, so cost can be shared between them.

    A self-hosted replica is paid for by the hour whether it serves one request
    or sixteen. Charging each call the replica's full rate for its wall-clock
    time bills the hour sixteen times over when sixteen calls overlap.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    def enter(self, backend: str) -> int:
        with self._lock:
            n = self._counts.get(backend, 0) + 1
            self._counts[backend] = n
            return n

    def leave(self, backend: str) -> int:
        """Returns the count including the call that is leaving."""
        with self._lock:
            n = self._counts.get(backend, 0)
            self._counts[backend] = max(0, n - 1)
            return n

    def current(self, backend: str) -> int:
        with self._lock:
            return self._counts.get(backend, 0)


IN_FLIGHT = InFlight()


def _sse_events(resp) -> Iterator[dict]:
    """Yields the JSON payload of each server-sent event until [DONE]."""
    for raw in resp:
        line = raw.decode("utf-8").strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            return
        try:
            yield json.loads(data)
        except json.JSONDecodeError as e:
            raise BackendError(f"unreadable stream event: {data[:80]!r}") from e


def complete(backend: Backend, prompt: str, max_tokens: int = 96, timeout: int = 120,
             temperature: float = 0.2, in_flight: InFlight = IN_FLIGHT) -> tuple[str, CallRecord]:
    payload = json.dumps({
        "model": backend.model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
        # vLLM and Ollama both send a final usage event when asked.
        "stream_options": {"include_usage": True},
    }).encode()
    req = urllib.request.Request(
        f"{backend.base_url.rstrip('/')}/chat/completions",
        data=payload, headers={"Content-Type": "application/json"}, method="POST")

    started_with = in_flight.enter(backend.name)
    t0 = time.perf_counter()
    ttft_ms: int | None = None
    parts: list[str] = []
    usage: dict = {}
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for event in _sse_events(resp):
                if event.get("usage"):
                    usage = event["usage"]
                for choice in event.get("choices") or []:
                    piece = (choice.get("delta") or {}).get("content") or ""
                    if piece and ttft_ms is None:
                        ttft_ms = int((time.perf_counter() - t0) * 1000)
                    parts.append(piece)
    except urllib.error.HTTPError as e:
        raise BackendError(f"{backend.name} returned HTTP {e.code}: {e.read()[:200]!r}") from e
    finally:
        ended_with = in_flight.leave(backend.name)
    latency_ms = int((time.perf_counter() - t0) * 1000)

    # Self-hosted is billed in GPU-hours, not tokens. Charge this call the
    # replica's hourly rate for the time it held the replica, divided by how
    # many calls held it at once. The mean of the counts at start and end is a
    # rough measure of that sharing; it is exact when no call starts or ends
    # part-way through this one.
    concurrency = max(1.0, (started_with + ended_with) / 2)
    cost = backend.cost_per_hour_milli_usd * (latency_ms / 3_600_000.0) / concurrency

    return "".join(parts).strip(), CallRecord(
        backend=backend.name, model=backend.model,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        latency_ms=latency_ms, ttft_ms=ttft_ms, cost_milli_usd=cost,
        concurrency=concurrency)
