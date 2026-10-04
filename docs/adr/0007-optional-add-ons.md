# ADR-0007: KEDA and the Inference Extension are optional; the operator reports their absence

**Status:** Accepted · **Date:** 2026-10

## Context

The operator creates two kinds of object that belong to other projects. A ScaledObject belongs to
KEDA (ADR-0001). An InferencePool belongs to the Gateway API Inference Extension, the Kubernetes
project that lets a gateway route model traffic to the best replica, for example the one whose
cache already holds the start of a prompt. Neither is installed in a fresh cluster.

A controller that watches a kind whose CRD is missing fails to start. An operator that refuses to
start until every add-on is present makes the first run on a laptop much harder than it needs to
be.

## Decision

The operator writes both objects as unstructured data, so it does not import either project's Go
module. At start-up it watches each kind only if its CRD exists. On each reconcile it applies
both objects. If the API server does not know the kind, the operator sets a condition on the
InferenceService and carries on.

| Condition | Plain meaning | Reason when the add-on is missing |
|---|---|---|
| `Autoscaling` | Whether a ScaledObject now manages the replica count | `KEDANotInstalled` |
| `Routing` | Whether the pods are registered in an InferencePool | `InferencePoolNotInstalled` |

The InferencePool names no endpoint picker. Since Inference Extension v1.6 the reference is
optional, and the full endpoint picker has moved to the llm-d project. A pool without one is valid.
A gateway that supports the extension can still route to it.

## Consequences

* Installing an add-on after the operator starts needs an operator restart before changes to the
  add-on's objects trigger a reconcile. The five-minute resync still repairs drift in the
  meantime.
* The operator's ClusterRole grants access to both kinds whether or not they are installed.

## Evidence

`TestMissingAddOnCRDsAreReportedNotFatal` runs the reconciler against an API server with neither
CRD and checks both conditions and the Deployment.

## Open questions

* Prefix-aware routing through llm-d's endpoint picker has not been tried with this operator.
  Whether it is worth running on one small GPU is untested.
