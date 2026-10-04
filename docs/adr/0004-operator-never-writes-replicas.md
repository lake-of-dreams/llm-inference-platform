# ADR-0004: The operator never writes .spec.replicas after creation

**Status:** Accepted, revised

## Context

Two controllers that both own one field will fight over it. The operator writes `replicas: 1`
from the spec. KEDA scales to 3. The next reconcile writes 1 back, and KEDA scales up again.
Neither is wrong on its own terms, and the Deployment swings back and forth.

The first version of this repository kept the operator out of the field by convention. It
compared a hand-picked list of fields before writing, and it never copied `replicas`. The list
missed environment variables, resources, the GPU claim and three of the four probe settings, so
changes to those never reached the Deployment. The CRD also declared a `/scale` subresource that
pointed at `minReplicas`. That let `kubectl scale`, or an autoscaler aimed at the
InferenceService, rewrite `minReplicas` itself.

## Decision

The operator uses server-side apply. With server-side apply the client sends the fields it wants
and the API server records which manager owns each one. A field that no manager lists is left
alone. When a manager stops listing a field it alone owns, the API server removes that field.

The operator applies the Deployment on every reconcile under the field manager
`inference-operator`. `replicas` is never in that object. Creation takes two steps:

1. `inference-operator` applies the full Deployment, without `replicas`.
2. A second field manager, `inference-operator-initial-replicas`, applies `replicas:
   minReplicas` and nothing else.
3. The second manager never writes again. It keeps ownership of `replicas` until the HPA writes
   the field and takes ownership.

The split matters. If `inference-operator` set `replicas` at creation, its next apply would leave
the field out, the API server would remove it, and Kubernetes would reset the count to one. The
first implementation created the Deployment with a plain create. That records every field as
owned through an update, so later applies could not remove anything, and switching backend left
the old container in the pod. The envtest suite caught it.

The `/scale` subresource is gone from the CRD.

## Consequences

* Changing `minReplicas` on a live InferenceService does not change the Deployment's replica
  count. It changes the ScaledObject's floor, and KEDA acts on that.
* An out-of-band replica edit is not reverted.
* Argo CD and other GitOps tools still need `ignoreDifferences` on `/spec/replicas`.
* Deployments created by the first version carry backend and classification in their selector,
  and a selector cannot be edited. The operator deletes such a Deployment and creates it again.
  The service is down for the length of one weight load while that happens.

## Evidence

`TestReplicasSetByAutoscalerSurviveSpecChange` scales a Deployment under another manager, changes
the spec, and checks the count survives. `TestVLLMServiceCreatesEveryOwnedObject` checks the count
stays at `minReplicas` across two reconciles. Both run against a real API server.
