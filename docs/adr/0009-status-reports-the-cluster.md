# ADR-0009: Status reports what the cluster says, not what the operator did

**Status:** Accepted

## Context
The operator was first run on a real cluster: a three-node kind cluster with KEDA installed, no
Prometheus at the configured address, and no GPU driver. Two conditions in the InferenceService
status were wrong there, and nothing in the envtest suite could have shown it, because envtest runs
no other controllers.

* `Autoscaling` was `True` with reason `ScaledObjectApplied`. KEDA had marked the same ScaledObject
  `Ready=False` with `TriggerError`, because the Prometheus address did not resolve. The Deployment
  would never scale, and the status said it would.
* `Ready` was `False` with reason `WeightsLoading` for the vLLM service. Its pod was `Pending`, and
  the scheduler had marked it `Unschedulable`: "device class gpu.nvidia.com does not exist". Nothing
  was loading or ever would. The next look would have been five minutes later, at the resync,
  because a pod that cannot be scheduled changes no object the operator watches.

Both conditions reported what the operator had done, not what had happened.

A third finding was noise rather than a wrong answer. KEDA's admission webhook warned on every
apply that `pollingInterval` and `cooldownPeriod` only matter when scaling to or from zero. The
operator never does that, since `minReplicas` is at least one.

## Decision
* After applying the ScaledObject, the operator reads it back and reports KEDA's own `Ready`
  condition. `True` gives `Autoscaling=True` with reason `ScalerReady`. `False` gives
  `Autoscaling=False` with reason `ScalerNotReady` and KEDA's reason and message. No condition yet
  gives `Unknown` with reason `WaitingForKEDA`.
* While no replica is ready, the operator lists the service's pods. If the scheduler has marked one
  `Unschedulable`, the phase is `Unschedulable` and `Ready` carries the scheduler's message.
  Otherwise the phase stays `Loading`.
* While no replica is ready, the operator looks again every 30 seconds instead of every five
  minutes.
* `pollingInterval` and `cooldownPeriod` are no longer set. The scale-down window in the HPA
  behaviour already does what `cooldownPeriod` appeared to.
* The operator's ClusterRole gains `get`, `list` and `watch` on pods.

## Consequences
* On the kind cluster, `qwen-small` now shows phase `Unschedulable` with the scheduler's reason, and
  `Autoscaling=False` quoting KEDA's `TriggerError`. The Ollama service, which needs no GPU, reached
  `Ready` and answered a chat request through the operator's Service.
* Three envtest tests hold this: `TestAutoscalingReportsKEDAsVerdict`,
  `TestUnschedulablePodIsReportedNotLoading`, and the updated
  `TestVLLMServiceCreatesEveryOwnedObject`, which now expects `WaitingForKEDA` because envtest runs
  no KEDA. All three fail against the previous controller.
* A service that is not yet ready is reconciled every 30 seconds. Each pass lists pods in one
  namespace by label, which is cheap.

## Open questions
* The operator reports a broken Prometheus address but cannot fix it. The address is a start-up
  flag, `--prometheus-address`. Installing Prometheus where the flag points, or pointing the flag at
  an existing one, is a cluster decision.
* Only the scheduler's verdict is surfaced. A pod that schedules and then crashes while loading
  weights still shows `Loading` until its startup probe gives up (ADR-0002).
