# ADR-0002: A startupProbe is mandatory for model containers

**Status:** Accepted, revised 2026-10 · **Date:** 2026-09

## Context

Kubernetes checks a running container with probes. A probe is a small request the kubelet, the
agent on each node, sends on a timer. If the liveness probe fails a few times in a row, the
kubelet restarts the container.

A model server cannot answer probes until it has loaded its weights, the billions of numbers that
make up the model. That takes anything from tens of seconds to several minutes. A container with
only a liveness probe is killed part-way through loading, restarts, and is killed again. What you
see is CrashLoopBackOff and a clean application log. That combination sends people hunting a bug
that is not there.

## Decision

Every model container gets three probes, each with its own job.

| Probe | Plain meaning | Tuning |
|---|---|---|
| `startupProbe` | Waits out weight loading; the other two probes do not start until it passes | Checks every 10 s, gives up after `weightLoadSeconds / 10 + 1` failures |
| `readinessProbe` | Takes a replica out of the Service quickly when it stops answering, without restarting it | Every 5 s, 3 failures |
| `livenessProbe` | Restarts a process that has hung | Every 20 s, 3 failures |

**Example.** `weightLoadSeconds: 300` gives 300 / 10 + 1 = 31 allowed failures at 10 s each. The
container has 310 seconds to load before Kubernetes gives up on it.

`weightLoadSeconds` is a spec field rather than an annotation, so the budget shows up in review.

What the probes check depends on the backend.

* **vLLM** answers `GET /health` once the engine is up. All three probes use it.
* **Ollama** has no `/health` endpoint. The first version of this repository probed one anyway, so
  Ollama pods never passed a probe. Readiness and liveness now use `GET /`, which answers "Ollama is
  running". The startup probe runs `ollama show <model>`, which succeeds only once the model has
  been pulled. Ollama's HTTP listener comes up in a second, long before the weights arrive, so an
  HTTP startup probe would pass too early.

Ollama can only pull a model through a running server. The container therefore starts the server
in the background, waits for it, pulls the model, and then waits on the server process. If the
pull fails the container exits and Kubernetes restarts it.

## Consequences

* A broken model is not restarted until the startup budget runs out. A slow, honest failure beats
  a restart loop that never ends.
* The budget needs revisiting whenever the model size changes.
* With no `modelCacheClaimName`, weights live in a scratch volume and every new pod downloads them
  again. The download counts against the startup budget.

## Open questions

* vLLM can stream weights from object storage with `--load-format runai_streamer`, which shortens
  loading. The operator does not expose it yet.
