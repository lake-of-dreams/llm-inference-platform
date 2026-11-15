# ADR-0013: Tracing is opt-in and never carries the prompt

**Status:** Accepted

## Context
The ledger says what each team spent. It does not show one request: which backend it went to, why,
how long the first word took. The usual tool for that is a trace, a timed record of one request
that a trace viewer can display.

OpenTelemetry publishes attribute names for calls to language models, under `gen_ai.*`. They are
still marked Development and moved to their own repository this year, so a name can change. The
names for the model, the operation and the token counts have kept their shape for several releases.
`server.address`, `server.port` and `error.type` are stable.

Two constraints come from this repository. The gateway installs with no dependencies. And the
people who can read a trace store are usually a wider group than the people cleared to read a
Restricted prompt.

## Decision
* `Gateway.handle` opens one span per request that reaches a backend, named `chat <model>`.
* The span carries the model, the requested token limit and temperature, the token counts, the
  server address and port, the backend chosen, the router's reason, the tenant, the classification,
  whether the answer came from the cache, the cost and the time to first token.
* The span never carries the prompt or the answer. This is a rule, not a default.
* A failed backend call sets `error.type` to the exception's class name and marks the span as an
  error.
* Tracing needs only the OpenTelemetry API package, as an optional extra. Without it the gateway
  uses a stand-in span that does nothing. With it and no exporter configured, OpenTelemetry's own
  no-op applies.

## Consequences
* Four tests, using OpenTelemetry's in-memory exporter. One asserts the attributes. One sends a
  prompt holding a salary figure, once to the backend and once from the cache, and searches every
  recorded name, attribute and event for it. That test was checked by adding the prompt as an
  attribute and watching it fail.
* A refused request has no span. The router raises before any backend is chosen, so there is no
  call to time. The refusal is an exception the caller sees.

## Open questions
* If the `gen_ai.*` names change before they are declared stable, the span's attributes need
  renaming. The `gateway.*` names are this repository's own.
* The tenant is on the span. That suits a firm tracing its own teams. A platform serving outside
  customers may want it hashed.
