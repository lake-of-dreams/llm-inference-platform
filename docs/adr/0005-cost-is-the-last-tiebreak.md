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

## Open questions

* Cost ranks backends by hourly price. Two backends with the same price and different throughput
  cost different amounts per answer. Ranking by cost per token needs measured throughput, which
  the gateway does not collect yet.
