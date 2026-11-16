# ADR-0015: A service that holds a device takes a pod down before starting its replacement

**Status:** Accepted

## Context
The operator first ran on a cluster with a real GPU: one 4 GB card, published by the NVIDIA DRA
driver as `gpu-0`. The vLLM service started, claimed the card and served requests.

Then its spec changed, and the rollout never finished. The new pod stayed `Pending` and the old
one kept serving. The scheduler's message was that no device was available.

Kubernetes replaces a Deployment's pods by starting the new pod first and removing the old one once
the new one is ready. That is the right default for ordinary software, because capacity never
drops. A device claimed through DRA is held by one pod at a time. With every device in use, the new
pod waits for a device, and the only device belongs to a pod that will not be removed until the new
pod is ready. Neither moves.

No test had shown this. The envtest suite runs no scheduler, and the earlier laptop cluster had no
device to hold.

## Decision
* When `deviceClass` is set, the Deployment's rolling update uses `maxSurge: 0` and
  `maxUnavailable: 1`. One old pod is removed, its device is released, and then one new pod starts.
* A service with no `deviceClass` keeps the Kubernetes default, which never drops capacity.

## Consequences
* A rollout of a GPU service now lowers its capacity by one replica at a time. With one replica,
  the service is down for as long as the weights take to load. That is stated here because it is a
  real cost: the alternative was a rollout that never completes.
* A service that needs to stay up through a rollout needs at least two replicas and, on a full
  cluster, one spare device.
* One controller test checks both strategies. On the cluster, the stuck rollout completed as soon
  as the operator applied the new strategy.

## Open questions
* While a rollout was stuck or a replica was down for a rollout, status still read `Ready` if any
  old replica served. Status now reads `Degraded`, and the `ReplicasReady` condition gives the
  reason `RolloutInProgress`.
* Where spare devices exist, starting the new pod first would be better. The operator cannot see
  free devices today; the scheduler can.
