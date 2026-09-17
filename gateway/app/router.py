"""Backend selection for the model gateway.

Filter order is classification, capability, latency headroom, then cost. Cost
ranks backends that are already eligible, it never removes one. Nothing eligible
raises instead of downgrading. Reasoning in ADR-0005.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import IntEnum


class Classification(IntEnum):
    PUBLIC = 0
    INTERNAL = 1
    RESTRICTED = 2


class NoEligibleBackend(RuntimeError):
    """No backend satisfies the request. There is no fallback path, see ADR-0005."""


@dataclass(frozen=True)
class Backend:
    name: str
    base_url: str
    model: str
    max_context: int
    # Highest classification this backend may handle.
    max_classification: Classification
    # Thousandths of a USD per hour for a self-hosted replica; 0 for local.
    cost_per_hour_milli_usd: int
    # observed p95 TTFT in ms, fed from metrics
    ttft_p95_ms: int = 0
    capabilities: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_local(self) -> bool:
        return self.cost_per_hour_milli_usd == 0


@dataclass
class Request:
    prompt: str
    classification: Classification = Classification.INTERNAL
    required_capabilities: frozenset[str] = field(default_factory=frozenset)
    latency_budget_ms: int = 30_000
    est_input_tokens: int = 0

    def __post_init__(self) -> None:
        if not self.est_input_tokens:
            # ~4 chars per token. close enough to size a context check.
            self.est_input_tokens = max(1, len(self.prompt) // 4)


@dataclass
class RoutingDecision:
    backend: Backend
    reason: str
    considered: int
    rejected: dict[str, str]


def route(req: Request, backends: Iterable[Backend]) -> RoutingDecision:
    pool = list(backends)
    rejected: dict[str, str] = {}

    # 1. classification ceiling
    eligible = []
    for b in pool:
        if b.max_classification < req.classification:
            rejected[b.name] = (
                f"classification {req.classification.name} exceeds ceiling "
                f"{b.max_classification.name}"
            )
        else:
            eligible.append(b)

    # 2. capability, and context with 1.3x headroom for the completion
    needed_ctx = int(req.est_input_tokens * 1.3)
    survivors = []
    for b in eligible:
        missing = req.required_capabilities - b.capabilities
        if missing:
            rejected[b.name] = f"missing capabilities {sorted(missing)}"
        elif b.max_context < needed_ctx:
            rejected[b.name] = f"context {b.max_context} < required {needed_ctx}"
        else:
            survivors.append(b)

    # 3. latency. over budget at p95 is out, however cheap it is.
    in_budget = [b for b in survivors if b.ttft_p95_ms <= req.latency_budget_ms]
    for b in survivors:
        if b not in in_budget:
            rejected[b.name] = f"p95 TTFT {b.ttft_p95_ms}ms over budget {req.latency_budget_ms}ms"

    if not in_budget:
        raise NoEligibleBackend(
            f"no backend satisfies the request; rejected={rejected}"
        )

    # 4. cost last. zero-cost local backends sort first.
    chosen = min(in_budget, key=lambda b: (b.cost_per_hour_milli_usd, b.ttft_p95_ms))
    reason = (
        f"cheapest of {len(in_budget)} eligible "
        f"(cost={chosen.cost_per_hour_milli_usd}m$/h, p95 TTFT={chosen.ttft_p95_ms}ms)"
    )
    return RoutingDecision(chosen, reason, len(pool), rejected)
