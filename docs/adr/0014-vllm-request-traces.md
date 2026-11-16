# ADR-0014: The operator can tell vLLM where to send its traces

**Status:** Accepted

## Context
The metrics the autoscaler reads are averages over many requests: waiting requests, cache fill,
the 95th percentile of time to first token. They do not show one request, and they do not show
what a model server spends its start-up time on.

vLLM can send traces, timed records of its own work, to any collector that speaks OTLP, the
OpenTelemetry wire format. The `vllm/vllm-openai:v0.33.0` image already contains the OpenTelemetry
packages this needs, which was checked by importing them inside the running container. The flag is
`--otlp-traces-endpoint`.

Where traces go is a property of the cluster, not of a model. One collector serves every model.

## Decision
* The operator takes a start-up flag, `--otlp-traces-endpoint`. It is empty by default.
* When it is set, every vLLM container gets `--otlp-traces-endpoint` with that address, and the
  environment variable `OTEL_SERVICE_NAME` set to the InferenceService's name, so a trace viewer
  groups traces by model.
* Ollama containers are unchanged. Ollama has no such flag.
* The setting is on the operator, not in the InferenceService spec. A tenant cannot point a model's
  traces at a collector of their own choosing.

## Consequences
* One controller test: with no endpoint the flag is absent; with one, the vLLM container carries the
  flag and the service name, and the Ollama container carries neither.
* On the laptop cluster, with Grafana Tempo as the collector, the first trace to arrive was the
  model's own start-up, named `Overall Loading`: 83 seconds, with a span for each stage, including
  downloading the weights, loading them, allocating the KV cache and warming up the GPU. That
  breakdown is what ADR-0002's start-up budget has to cover, now measured per stage.
* Changing the flag changes every vLLM Deployment's pod template, so every vLLM service rolls. On a
  cluster with one GPU that rollout is what exposed ADR-0015.

## Open questions
* The gateway's own spans (ADR-0013) and vLLM's were not joined into one trace. ADR-0018 joins them.
* If the collector is down, vLLM logs export errors and keeps serving. That was not tested here.
