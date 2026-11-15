# ADR-0012: Status names the device each pod was given

**Status:** Accepted

## Context
A service asks for "one device of this class with at least this much memory". The scheduler picks a
device that fits. Which one it picked was recorded only on the pod's ResourceClaim, an object named
after the pod with a random suffix, in a status field few people know to read.

The question comes up as soon as something is slow or failing on one replica: which card is that
replica on? With two device types in one class, the answer also decides what an answer costs
(ADR-0005).

For each pod, Kubernetes creates a ResourceClaim from the service's template. The claim is owned by
the pod, and the scheduler writes the chosen driver, pool and device into the claim's
`status.allocation`.

## Decision
* `status.devices` lists one entry per allocated device: the pod, the driver, the pool and the
  device name.
* The operator lists the ResourceClaims in the namespace and keeps those whose controlling owner is
  one of the service's pods. A claim owned by another workload's pod is ignored.
* A service with no `deviceClass` has no `devices` list.
* The operator's ClusterRole gains `get`, `list` and `watch` on resourceclaims. It never writes one.

## Consequences
* One controller test creates two pods and two allocated claims, one belonging to another workload,
  and checks that only the service's own device is reported.
* On the laptop kind cluster the Kubernetes example DRA driver was installed. It publishes pretend
  devices under the class `gpu.example.com`. An Ollama service that named that class was scheduled,
  and its status read `gpu-0` from driver `gpu.example.com` in pool `pa-pipeline-worker` within the
  same reconcile that reported `Loading`.
* The list is as fresh as the last reconcile. A claim's allocation changes no object the operator
  watches. While no replica is ready the operator looks every 30 seconds, and a pod becoming ready
  changes the Deployment, which it does watch.

## Open questions
* Not seen with the NVIDIA DRA driver, only with the example driver. The fields read are part of the
  ResourceClaim API, not of any driver, so the shape is the same.
* The claim's `status.devices`, where a driver can report each device's health and network details,
  is not copied. It is the natural next field once a real driver is present to fill it.
