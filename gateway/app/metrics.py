"""Live latency figures for the router, read from Prometheus.

The router drops a backend whose p95 time to first token is over the caller's
budget (ADR-0005). A hard-coded p95 goes stale the first time load changes, so
this module reads the observed figure from the same vLLM histogram the
autoscaler uses.
"""

from __future__ import annotations

import dataclasses
import json
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable

from .router import Backend

TTFT_P95_QUERY = (
    "histogram_quantile(0.95, sum(rate("
    'vllm:time_to_first_token_seconds_bucket{{model_name="{model}"}}[{window}])) by (le)) * 1000'
)


def query_scalar(prometheus_url: str, promql: str, timeout: float = 3.0) -> float | None:
    """Runs an instant query and returns its single value, or None if there is none.

    No value is a normal answer: a backend with no recent traffic has no rate
    to compute a quantile from. "NaN" means the same thing.
    """
    url = f"{prometheus_url.rstrip('/')}/api/v1/query?" + urllib.parse.urlencode({"query": promql})
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        body = json.load(resp)
    if body.get("status") != "success":
        raise RuntimeError(f"prometheus query failed: {body.get('error', body)}")
    result = body["data"]["result"]
    if not result:
        return None
    value = float(result[0]["value"][1])
    return None if value != value else value  # NaN check


def with_observed_ttft(backends: Iterable[Backend], prometheus_url: str, window: str = "5m",
                       query: Callable[[str, str], float | None] = query_scalar) -> list[Backend]:
    """Returns the backends with ttft_p95_ms replaced by the observed figure.

    Only backends that report vLLM metrics get a reading. A backend with no
    reading keeps its configured value rather than dropping to zero, because
    zero would make an idle backend look like the fastest one.
    """
    out = []
    for b in backends:
        ms = query(prometheus_url, TTFT_P95_QUERY.format(model=b.model, window=window))
        out.append(dataclasses.replace(b, ttft_p95_ms=int(ms)) if ms is not None else b)
    return out
