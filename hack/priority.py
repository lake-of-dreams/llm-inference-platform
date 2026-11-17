"""Shows llm-d's flow control choosing who goes first when the pool is full.

Low-priority requests arrive first and fill the pool. High-priority requests
arrive a second later, behind them. Without flow control every request would
be handed to vLLM at once and served in arrival order. With it, a request the
pool has no room for waits in llm-d's picker, and the picker releases the
highest priority first.

The last part sends low-priority requests that are only willing to wait two
seconds. The picker turns those away once their time is up.

    make priority        # needs `make flow-control`
"""
from __future__ import annotations

import json
import os
import statistics
import threading
import time
import urllib.error
import urllib.request

URL = os.getenv("URL", "http://qwen-queued.172.18.255.200.nip.io/v1")
MODEL = os.getenv("MODEL", "qwen-small")
PROMPT = "Write four sentences about how a canal lock works."


def call(objective: str, tenant: str, out: list, t0: float, ttl: str | None = None) -> None:
    headers = {"Content-Type": "application/json", "x-llm-d-inference-objective": objective,
               "x-llm-d-inference-fairness-id": tenant}
    if ttl:
        headers["x-llm-d-inference-ttl"] = ttl
    body = json.dumps({"model": MODEL, "max_tokens": 120,
                       "messages": [{"role": "user", "content": PROMPT}]}).encode()
    req = urllib.request.Request(f"{URL}/chat/completions", data=body, headers=headers)
    sent = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            r.read()
            status, h = r.status, r.headers
    except urllib.error.HTTPError as e:
        e.read()
        status, h = e.code, e.headers
    out.append({"objective": objective, "status": status,
                "queued_ms": int(h.get("x-llm-d-flow-queue-duration-ms") or 0),
                "reason": h.get("x-llm-d-request-dropped-reason", ""),
                "sent_at": sent - t0, "done_at": time.perf_counter() - t0})


def wave(spec: list[tuple[str, str, int, float, str | None]]) -> list[dict]:
    """spec rows: objective, tenant, how many, seconds after the start, queue budget."""
    out: list[dict] = []
    t0 = time.perf_counter()
    threads = []
    for objective, tenant, n, after, ttl in spec:
        for _ in range(n):
            t = threading.Timer(after, call, args=(objective, tenant, out, t0, ttl))
            t.start()
            threads.append(t)
    for t in threads:
        t.join()
    return out


def report(rows: list[dict], names: dict[str, str]) -> None:
    print(f"  {'':24}{'sent':>6}{'answered':>10}{'turned away':>13}"
          f"{'median wait in queue':>23}{'last one done at':>19}")
    for objective, label in names.items():
        mine = [r for r in rows if r["objective"] == objective]
        ok = [r for r in mine if r["status"] == 200]
        wait = f"{statistics.median(r['queued_ms'] for r in ok) / 1000:.1f} s" if ok else "-"
        done = f"{max(r['done_at'] for r in ok):.1f} s" if ok else "-"
        print(f"  {label:24}{len(mine):>6}{len(ok):>10}{len(mine) - len(ok):>13}{wait:>23}{done:>19}")


def main() -> None:
    names = {"qwen-small-batch": "batch (priority -10)", MODEL: "interactive (priority 10)"}
    print("1. Twenty batch requests arrive, then eight interactive ones a second later.")
    rows = wave([("qwen-small-batch", "reports", 20, 0.0, None), (MODEL, "helpdesk", 8, 1.0, None)])
    report(rows, names)
    order = "".join("I" if r["objective"] == MODEL else "b"
                    for r in sorted(rows, key=lambda r: r["done_at"]))
    print(f"  order answered (b batch, I interactive): {order}")

    print("\n2. The same, but each batch request will wait in the queue for two seconds at most.")
    rows = wave([("qwen-small-batch", "reports", 20, 0.0, "2s"), (MODEL, "helpdesk", 8, 1.0, None)])
    report(rows, names)
    turned = sorted({f"HTTP {r['status']} {r['reason']}".strip() for r in rows if r["status"] != 200})
    print(f"  turned away with: {', '.join(turned) or 'nothing'}")


if __name__ == "__main__":
    main()
