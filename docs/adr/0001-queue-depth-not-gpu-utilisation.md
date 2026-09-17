# ADR-0001: Autoscale on queue depth, not GPU utilisation

**Status:** Accepted · **Date:** 2026-09

## Context
GPU utilisation looks like the obvious signal for a GPU workload. For LLM
serving it is close to the worst one on offer.

Inference has two phases. Prefill is compute-bound and does saturate the SMs.
Decode, where most of the wall clock goes on chat traffic, is
*memory-bandwidth* bound: the device spends its time moving KV-cache bytes, so
`nvidia-smi` reports modest utilisation while requests sit in the queue behind
it.

The failure is not that it scales late. It scales the wrong way. Queues build,
per-request latency rises, measured utilisation *falls*, and an HPA on
utilisation removes a replica at peak load.

## Decision
Primary trigger is `vllm:num_requests_waiting`. Second trigger is p95
time-to-first-token, acting as the SLO guardrail. KEDA takes the max across
triggers, so either can scale up on its own.

## Consequences
* Needs a metrics pipeline. Queue depth is not a core Kubernetes metric.
* The two triggers can disagree. Taking the max is what we want: a latency
  breach with a shallow queue is usually prefill saturation from long prompts,
  and that still needs capacity.
* `cooldownPeriod` has to exceed weight-load time (ADR-0002), or the scaler
  removes a replica while its replacement is still loading and the service never
  leaves cold start.

## Evidence
Both metrics confirmed present on the running server,
`vllm:num_requests_waiting` and `vllm:time_to_first_token_seconds_bucket`. See
`hack/verify.py` output.
