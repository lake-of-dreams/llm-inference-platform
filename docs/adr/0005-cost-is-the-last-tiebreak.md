# ADR-0005: Route on classification, then capability, then latency, then cost

**Status:** Accepted

## Context
The tempting selection rule for a gateway fronting several models is
cheapest-that-works, evaluated cheapest first. Two things then go wrong and
neither one raises an error, so you get a plausible answer back either way:
restricted data routed to the cheapest endpoint, which may be a third party; and
a task needing a large context routed to a model that cannot hold it.

## Decision
Filter in order: classification → capability → latency headroom → cost. Cost
ranks the backends that are already eligible. It never removes one.

If nothing survives, the gate raises `NoEligibleBackend`. It does not downgrade
to a weaker model: a caller that asked for a capability and got a quiet
substitution has no way to know its results no longer compare with yesterday's.

## Consequences
* Some requests fail that a downgrading gateway would have served. Intended.
* Every rejection carries its reason (`RoutingDecision.rejected`), so "why did
  this route here" is answerable after the fact.
* The latency filter is only as good as its p95 figure. `app.metrics.with_observed_ttft` replaces
  the configured figure with the one Prometheus observed. A backend with no recent traffic keeps
  its configured figure, because zero would make an idle backend look fastest.
* A cache in front of the router can undo the classification filter by serving an answer across
  classifications. ADR-0006 closes that.

## Revised: cost per token

Cost ranked backends by hourly price. Two backends at the same price and different throughput cost
different amounts per answer, and a backend dearer by the hour can be cheaper per answer: 800
thousandths of a dollar an hour at 400 tokens a second is 556 per million tokens, against 1,444
for 520 an hour at 100 tokens a second.

Each backend now has `tokens_per_second`, the generated tokens one replica sustains when busy. Cost
ranks by `cost_per_mtok_milli_usd`, the hourly price divided by an hour of that throughput. A local
backend costs zero per token. A paid backend with no throughput figure ranks after every backend
that has one, and those without one rank by hourly price among themselves. The routing reason says
which basis decided.

The throughput is a figure from a load test, such as `vllm bench serve`, not the rate Prometheus
observes. Observed throughput falls towards zero when a replica is idle, which would make the
replica with the most spare room look the most expensive.

Evidence: five tests in `gateway/tests/test_router.py`, from
`test_same_hourly_price_faster_replica_is_cheaper_per_answer` to
`test_local_backend_is_free_per_token_too`. Ranking by hourly price again fails three of them.

## Open questions

* The throughput figure is entered by hand. It goes stale when the model, the card or the vLLM
  version changes, and nothing warns when it does. Comparing it with the throughput observed under
  load would catch that.
