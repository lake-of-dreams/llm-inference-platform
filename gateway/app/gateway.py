"""One request through the gateway, from the caller's tenant to the ledger.

The router, the answer cache, the prefix cache salt and the ledger each work on
their own. This is the order they run in for a real request, and the one place
that requires a tenant. Guide 4 walks through it.

1. A request with no tenant is refused. Every later step needs it.
2. The router chooses a backend, or refuses. The cache is only consulted for a
   backend the router allowed, so a cached answer never skips the
   classification check (ADR-0005, ADR-0006).
3. The answer cache is searched in the request's own scope, which includes the
   tenant: another team's answer is never served (ADR-0010).
4. On a miss, the backend is called with the tenant's prefix cache salt
   (ADR-0008), and the answer is cached.
5. Either way the call lands in the ledger under the tenant.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from .cache import CacheScope, SemanticCache, prefix_cache_salt
from .client import IN_FLIGHT, CallRecord, InFlight, Ledger, complete
from .router import Backend, Request, route


@dataclass
class Answer:
    text: str
    backend: str
    reason: str
    cached: bool
    record: CallRecord


@dataclass
class Gateway:
    backends: Iterable[Backend]
    salt_key: bytes
    cache: SemanticCache = field(default_factory=SemanticCache)
    ledger: Ledger = field(default_factory=Ledger)
    in_flight: InFlight = IN_FLIGHT

    def handle(self, req: Request, tenant: str, *, max_tokens: int = 96,
               temperature: float = 0.2) -> Answer:
        if not tenant:
            raise ValueError("a request needs a tenant: the cache, the salt and the ledger all key on it")
        decision = route(req, list(self.backends))
        b = decision.backend
        scope = CacheScope.of(req.classification, b.model, temperature=temperature,
                              max_tokens=max_tokens, tenant=tenant)

        hit = self.cache.get(req.prompt, scope)
        if hit is not None:
            rec = CallRecord(backend=b.name, model=b.model, prompt_tokens=0, completion_tokens=0,
                             latency_ms=0, ttft_ms=None, cost_milli_usd=0.0, cached=True,
                             tenant=tenant)
            self.ledger.add(rec)
            return Answer(hit, b.name, decision.reason, True, rec)

        text, rec = complete(b, req.prompt, max_tokens=max_tokens, temperature=temperature,
                             in_flight=self.in_flight,
                             cache_salt=prefix_cache_salt(self.salt_key, req.classification, tenant))
        rec.tenant = tenant
        self.ledger.add(rec)
        self.cache.put(req.prompt, text, scope)
        return Answer(text, b.name, decision.reason, False, rec)
