# ADR-0001: Autoscale on queue depth, not GPU utilisation

**Status:** Accepted, revised 2026-10 · **Date:** 2026-09

## Context

GPU utilisation looks like the obvious autoscaling signal for a GPU workload. For serving a
language model it is close to the worst one on offer.

A model answers a request in two phases. Prefill reads the whole prompt at once. It keeps the
GPU's arithmetic units busy, so utilisation goes up. Decode then produces the answer one token at
a time, where a token is a word or a piece of a word. Each step moves the model's saved working
state from memory, and moving bytes is slower than doing the arithmetic on them. Decode is
therefore limited by memory bandwidth, and `nvidia-smi` reports modest utilisation while it runs.

Chat traffic spends most of its time in decode. So requests wait in the queue while the GPU looks
half idle. The failure is not that utilisation scales late. It scales the wrong way: queues
build, latency rises, measured utilisation falls, and an autoscaler on utilisation removes a
replica at peak load.

## Decision

The operator writes a KEDA ScaledObject with three triggers. KEDA is the Kubernetes Event-driven
Autoscaler, which turns a metric query into a replica count for the standard Horizontal Pod
Autoscaler (HPA). KEDA takes the largest count any trigger asks for, so each trigger can scale up
on its own.

| Trigger | Plain meaning | How the count is worked out |
|---|---|---|
| `vllm:num_requests_waiting`, summed | Requests that have arrived and not started | Sum divided by `queueDepthTarget`, the waiting requests one replica should carry |
| `vllm:kv_cache_usage_perc`, summed | How full each replica's working-state memory is, from 0 to 1 | Sum divided by `kvCacheUsagePercent` / 100 |
| p95 time to first token, in ms | How long 95 in 100 callers wait for the first word | Current replicas times observed p95 divided by `ttftP95Milliseconds` |

Queue depth is the primary signal because it moves first. The KV cache trigger catches a replica
whose memory is full. vLLM then pauses requests already running to make room, and that pressure
can show here before the queue grows. The time to first token trigger is the guardrail for the
latency objective.

The first two triggers use KEDA's default `AverageValue` metric type, which divides the summed
metric by the target. The latency trigger uses `metricType: Value`, which compares the metric with
the target directly. A latency does not get smaller when it is divided by the number of replicas,
so the default would scale on a meaningless figure. The first version of this repository made
that mistake.

**Example.** A service runs 2 replicas with `ttftP95Milliseconds: 2000`. The observed p95 is
2,600 ms. With `Value`, the HPA asks for ceil(2 × 2600 / 2000) = ceil(2.6) = 3 replicas. With
`AverageValue` it would have asked for ceil(2600 / 2000) = 2 replicas whatever the current count,
so it could never add a third.

Every query filters on `namespace` and `model_name`. vLLM sets `model_name` from
`--served-model-name`, and the operator sets that flag to the InferenceService's name. The first
version filtered on a name the operator never set, so the queries matched nothing.

## Consequences

* Scaling needs a metrics pipeline. Queue depth is not a core Kubernetes metric. The queries
  assume Prometheus adds a `namespace` label to scraped pods, which the common pod scrape
  configuration does.
* The triggers can disagree. Taking the largest is what we want. A latency breach with a short
  queue is usually prefill saturated by long prompts, and that still needs capacity.
* Scale-down waits at least as long as the weight-load budget (ADR-0002), and at least five
  minutes. Removing a replica while its replacement is still loading leaves the service in cold
  start.
* `minReplicas` cannot be zero. Every signal comes from a running vLLM replica, so at zero
  replicas nothing reports demand and nothing would scale the service back up.
* Ollama exports none of these metrics. An Ollama service gets no ScaledObject and stays at
  `minReplicas`. Its status says so.

## Evidence

The metric names were checked in the vLLM v0.30.0 source (`vllm/v1/metrics/loggers.py`), which
also shows the `model_name` label. The trigger shapes are checked against the KEDA v2.21.0
ScaledObject CRD in `internal/controller/inferenceservice_controller_test.go`.

## Open questions

* Scale-to-zero needs a demand signal that exists with no replicas running, such as a queue
  length reported by the gateway. Nothing in this repository provides one yet.
* Kubernetes 1.37 made the HPA's tolerance configurable per object. Before then a 10% band was
  fixed for the whole cluster. Whether a tighter tolerance helps here has not been measured.
