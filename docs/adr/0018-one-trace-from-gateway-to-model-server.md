# ADR-0018: The gateway passes its trace identity to the backend

**Status:** Accepted

## Context
The gateway records one span per request (ADR-0013) and vLLM records its own (ADR-0014). They
arrived in the trace store as two unrelated traces. To see why one request was slow, a person had
to find both by timestamp and guess that they matched.

The W3C Trace Context standard defines a request header, `traceparent`, for exactly this. It
carries the trace's id, the id of the calling span and a flag saying whether the trace is being
recorded. vLLM reads the header when its tracing is on and records its spans under that parent.

## Decision
* When a gateway span is open, the client adds `traceparent` to the request it sends the backend,
  through OpenTelemetry's standard propagator.
* With no span open, or with OpenTelemetry not installed, no header is added. The gateway keeps
  its install with no dependencies.
* The header is sent to every backend. One that does not trace ignores it.

## Consequences
* Two tests: the header a backend receives names the gateway span's trace and span ids with the
  recorded flag set, and a call made outside any span carries no such header.
* On the cluster, one request through the gateway produced one trace in Tempo holding two spans:
  `chat qwen-small` from the gateway, and under it `llm_request` from vLLM.
* The header holds two random ids and a flag. It says nothing about the prompt, the tenant or the
  classification, so it adds nothing to what a backend already receives.

## Open questions
* Ollama records no traces, so a request routed to it still ends at the gateway's span.
* A caller's own `traceparent` is not read. The gateway has no network front end yet, so there is
  no incoming header to continue from (ADR-0010).
