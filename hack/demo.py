#!/usr/bin/env python3
"""The gateway's decisions on a laptop: no cluster, no GPU, no model.

Starts two stand-in model servers on local ports and sends five requests
through the real gateway code. The stand-ins answer with a fixed sentence; the
routing, the answer cache, the prefix cache salt and the ledger are the real
ones. Each line shows which backend got the request and why.
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gateway"))

from app.client import InFlight  # noqa: E402
from app.gateway import Gateway  # noqa: E402
from app.router import Backend, Classification, NoEligibleBackend, Request  # noqa: E402


def stand_in(name: str, calls: list) -> str:
    """A server that speaks enough of the OpenAI streaming API to answer once."""
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((name, body.get("cache_salt")))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for event in ({"choices": [{"index": 0, "delta": {"content": f"answer from {name}"}}]},
                          {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 6}}):
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}/v1"


def main() -> int:
    calls: list = []
    backends = [
        # A small model on a cheap card: Public and Internal data only.
        Backend("phi4-mini", stand_in("phi4-mini", calls), "phi4-mini", max_context=4096,
                max_classification=Classification.INTERNAL, cost_per_hour_milli_usd=600,
                ttft_p95_ms=300, tokens_per_second=400),
        # A larger model on approved machines: may see Restricted data.
        Backend("qwen-restricted", stand_in("qwen-restricted", calls), "qwen-small",
                max_context=8192, max_classification=Classification.RESTRICTED,
                cost_per_hour_milli_usd=3600, ttft_p95_ms=450, tokens_per_second=1200,
                prefix_cache_salt=True),
    ]
    gw = Gateway(backends, salt_key=b"demo-key-not-a-secret", in_flight=InFlight())

    steps = [
        ("website", Request("What are your opening hours?", Classification.PUBLIC),
         "a Public question: both backends may take it, so cost decides"),
        ("hr", Request("Summarise this grievance letter.", Classification.RESTRICTED),
         "Restricted data: only the approved backend may see it"),
        ("hr", Request("Summarise this grievance letter.", Classification.RESTRICTED),
         "the same team asks again: answered from the cache, no model call"),
        ("contracts", Request("Summarise this grievance letter.", Classification.RESTRICTED),
         "another team asks the same thing: HR's cached answer is not theirs to see"),
        ("website", Request("Describe this photo.", Classification.PUBLIC,
                            required_capabilities=frozenset({"vision"})),
         "needs a capability neither backend has: refused, never downgraded"),
    ]
    for n, (tenant, req, why) in enumerate(steps, 1):
        print(f"{n}. {tenant}: {req.prompt!r}\n   {why}")
        try:
            a = gw.handle(req, tenant=tenant)
        except NoEligibleBackend as exc:
            print(f"   -> REFUSED: {str(exc).split(';')[0]}\n")
            continue
        source = "answer cache" if a.cached else a.backend
        print(f"   -> {source}  ({a.reason})\n")

    salts = {salt for name, salt in calls if name == "qwen-restricted"}
    print(f"Model calls made: {len(calls)} of {len(steps)} requests.")
    print(f"Prefix cache salts sent to qwen-restricted: {len(salts)} "
          "(one per team, so one team's prompts cannot be timed by another).")
    print("\nLedger, by team:")
    for tenant, t in sorted(gw.ledger.by_tenant().items()):
        print(f"  {tenant:<10} calls={t['calls']} cached={t['cached']} tokens={t['tokens']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
