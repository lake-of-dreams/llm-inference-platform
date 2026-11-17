# ADR-0020: llm-d's picker runs in front of the pool with flow control on

**Status:** Accepted

## Context
The operator writes an InferenceObjective for a service that sets a priority (ADR-0011). Nothing
read it. Requests went straight to vLLM, which serves them in the order they arrive.

llm-d 1.0 ships the endpoint picker as a chart, `llm-d-router-standalone`, with an Envoy proxy
beside it. Flow control is a feature gate in the picker's configuration, off by default.

## Decision
* `make flow-control` installs the picker for the `qwen-small` pool from the chart at v1.0.0, with
  the `flowControl` gate on.
* The chart renders an InferencePool named after the release. The operator already owns that pool
  (ADR-0007), so the chart's copy is dropped from the rendered output and the picker reads the
  operator's. Two owners of one object would overwrite each other.
* The pool counts as full at four requests in flight per replica, through the picker's
  `concurrency-detector`. vLLM would accept sixteen. Four keeps time to first token short for the
  requests let through and makes the queue visible on one small card.
* A second objective, `qwen-small-batch` at priority -10, is added by hand for work that can wait.
  The operator makes one objective per service, from the service's own priority.
* The picker has its own hostname. The existing one still reaches vLLM directly, so the chat window
  and the earlier checks are unchanged.

## Consequences
* `make priority` sends 20 batch requests, then 8 interactive ones a second later. The interactive
  requests were answered fifth to twelfth, ahead of 16 batch requests that had arrived before them.
  Their median wait in the queue was 0.7 seconds against 4.5 for batch.
* With each batch request willing to wait 2 seconds at most, 15 of 20 were turned away with HTTP
  429 and the reason `rejected-ttl-expired`. All 8 interactive requests were answered.
* Each answer carries `x-llm-d-flow-queue-duration-ms`, the time it waited. The script reads it.
* The gateway already sends the objective and the tenant as headers to a backend that names an
  objective (ADR-0011). Pointing that backend's address at the picker is all it needs.

## Open questions
* Fairness between tenants was not measured. The script sends a fairness id per class, and both
  classes differ in priority as well, so the result shows priority only.
* Scale to zero through the picker's queue was not tried. The CRD still requires one replica.
* The picker scores replicas on queue depth, cache use and shared prefixes. With one replica there
  is nothing to choose between, so that part did nothing here.
* The limit of four is typed in. The right figure depends on the model and the card, like the
  throughput figures in ADR-0005.
