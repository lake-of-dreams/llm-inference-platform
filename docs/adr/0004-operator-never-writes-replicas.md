# ADR-0004: The operator never writes .spec.replicas after creation

**Status:** Accepted · **Date:** 2026-09

## Context
Two controllers owning one field. Reconciler writes desired `replicas`, KEDA
scales to 3, next reconcile writes 1 back, KEDA scales up again. Neither one is
wrong in isolation and the Deployment oscillates.

## Decision
Set `replicas` once at creation from `spec.autoscaling.minReplicas`, then leave
it to KEDA. `specEquivalent()` in `internal/controller/deployment.go` keeps
replicas out of the drift comparison and the drift patch touches only the pod
template and selector. Argo CD needs `ignoreDifferences` on `/spec/replicas`.

## Consequences
* Changing `minReplicas` on a live InferenceService does not scale it down.
  Surprises people exactly once.
* An out-of-band replica edit is not reverted.
