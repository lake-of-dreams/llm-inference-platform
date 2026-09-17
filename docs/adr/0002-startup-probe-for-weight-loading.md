# ADR-0002: A startupProbe is mandatory for model containers

**Status:** Accepted · **Date:** 2026-09

## Context
Loading weights takes anything from tens of seconds to several minutes. A
container with only a `livenessProbe` gets killed mid-load, restarts, and gets
killed again. What you see is CrashLoopBackOff and a clean application log, which
sends people hunting a bug that is not there.

## Decision
All three probes, each with its own job:

| Probe | Job | Tuning |
|---|---|---|
| `startupProbe` | survive weight loading | `failureThreshold = weightLoadSeconds / 10 + 1` |
| `readinessProbe` | pull an unhealthy replica from the Service fast | 5s period, 3 failures |
| `livenessProbe` | restart a wedged process | 20s period, only active after startup succeeds |

`weightLoadSeconds` is a CRD spec field rather than an annotation, so the budget
turns up in review.

## Consequences
* A genuinely broken model is not restarted until the startup budget expires.
  Fine. A slow honest failure beats an infinite restart loop.
* Budget needs revisiting whenever the model size changes.
