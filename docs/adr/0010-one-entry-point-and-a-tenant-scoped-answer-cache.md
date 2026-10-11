# ADR-0010: One entry point for a request, and an answer cache scoped by tenant

**Status:** Accepted

## Context
The gateway had a router, an answer cache, a prefix cache salt and a ledger. Each was tested on its
own. Nothing ran them together, so the order they run in, and what each needs from the request, was
left to whoever wired them up. Two gaps followed.

* **Nothing required a tenant.** The prefix cache salt (ADR-0008) needs the caller's tenant, the
  team or customer they belong to, and nothing supplied one. The ledger recorded cost per backend,
  so "what did this team spend" could not be answered.
* **The answer cache crossed tenants.** ADR-0006 scoped cached answers by classification, model and
  sampling settings. Two teams at the same classification shared one scope. HR asks "summarise the
  payroll file" at Internal and the answer is cached. A contracts user sends the same words at
  Internal and receives HR's answer. ADR-0008 closed a timing channel between tenants. This was the
  whole answer.

Writing the entry point showed the second gap. Its test sent the same prompt from two tenants, and
the second was served from the first's cache.

## Decision
* `Gateway.handle(request, tenant)` in `gateway/app/gateway.py` is the one entry point. In order:
  refuse a request with no tenant; route; search the answer cache in the request's scope; on a miss,
  call the backend with the tenant's salt and cache the answer; record the call under the tenant.
* The tenant is refused before routing, so a request without one fails the same way whatever else
  is wrong with it.
* The cache is searched only after the router has chosen a backend, so a cached answer never skips
  the classification check.
* `CacheScope` gains `tenant`. A lookup searches only its own tenant's entries.
* `CallRecord` gains `tenant`, and `Ledger.by_tenant()` reports calls, cached calls, tokens and cost
  per tenant. A cache hit is recorded at zero cost and marked cached.

## Consequences
* Six tests in `gateway/tests/test_gateway.py`. With the cache scope as it was, the cross-tenant test
  fails: the contracts user is served HR's answer. Disabling the tenant check, the salt or the
  ledger's tenant each fails a test.
* The hit rate falls, because teams no longer share cached answers. The same trade ADR-0006 made
  for classifications.
* `hack/verify.py` and the existing tests that call the pieces directly still work: `tenant`
  defaults to empty in `CacheScope` and `CallRecord`. The entry point never passes empty.

## Open questions
* The gateway has no network front end and no sign-in. Something must still read the tenant from a
  verified identity and call `handle`. Taking a tenant from a request header the caller writes would
  let any caller claim any tenant.
