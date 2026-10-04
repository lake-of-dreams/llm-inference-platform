# ADR-0005: Route on classification, then capability, then latency, then cost

**Status:** Accepted · **Date:** 2026-09

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
