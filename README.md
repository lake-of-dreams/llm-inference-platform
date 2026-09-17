# LLM Inference Platform

A Go operator that reconciles a declarative `InferenceService` into a running
vLLM deployment, autoscales it on queue depth, and puts a cost-aware gateway in
front of it. Everything here runs on a laptop: kind cluster, a 4 GB consumer
GPU, local Ollama.

## Running it

```bash
make cluster      # kind cluster + local registry
make deploy       # CRDs, operator, gateway, KEDA ScaledObject
make verify       # end-to-end: real vLLM, real Ollama, no mocks
```

`make verify` talks to live backends and fails fast if they are not up, so start
vLLM with `hack/serve-vllm.sh` first and have Ollama listening on :11434.

## Why it looks like this

Three things bite when inference first goes onto Kubernetes. The handling for
all three is in the code rather than in somebody's runbook.

CPU and GPU utilisation are the wrong autoscaling signals. Decode is
memory-bandwidth bound, so the device looks unbusy while the queue builds, and
an HPA on CPU scales *down* while latency is climbing. This scales on
`vllm:num_requests_waiting`, with p95 TTFT as a second trigger for the SLO
(ADR-0001).

Weight loading takes minutes. With no `startupProbe` the liveness probe kills
the container mid-load, it restarts, and it gets killed again. CrashLoopBackOff
with nothing in the application log (ADR-0002).

The reconciler and the autoscaler will fight over `replicas` if you let them.
The operator writes it once at creation and never again; GitOps needs
`ignoreDifferences` on `/spec/replicas` (ADR-0004).

## Architecture

    InferenceService (CRD)
      └─ operator reconciles ──► Deployment (vLLM) ──► DRA ResourceClaim (GPU)
                              ├─ Service
                              ├─ ScaledObject (KEDA: queue depth + TTFT p95)
                              └─ InferencePool (Gateway API Inference Extension)

    client ──► model-gateway ──► routing: classification → capability → latency → cost
                              ├─ vLLM      (GPU, self-hosted, per-GPU-hour cost)
                              └─ Ollama    (host, CPU/GPU, zero marginal cost)

## ADRs

In `docs/adr/`:

| ADR | Subject |
|---|---|
| 0001 | queue depth, not GPU utilisation |
| 0002 | startupProbe for weight loading |
| 0003 | FlashInfer sampler off on this hardware |
| 0004 | operator never writes replicas |
| 0005 | cost is the last tiebreak |
