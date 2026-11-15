# ADR-0011: A model's priority is an llm-d InferenceObjective on its pool

**Status:** Accepted

## Context
When a pool of replicas is saturated, every request waits in the model server's own queue in the
order it arrived. A clerk's contract question and a nightly batch summary wait in the same line.
Nothing in the InferenceService said which should go first.

llm-d 1.0 added flow control to its endpoint picker, the component that chooses a replica for each
request. With flow control on, requests that cannot be served yet wait in the picker, leave in
priority order, and are shared fairly between callers. Requests with a negative priority are the
first to be shed. The priority comes from an `InferenceObjective`, an object in the `llm-d.ai`
group that names one or more InferencePools and an integer. A request picks its objective with the
`x-llm-d-inference-objective` header and its fairness group with `x-llm-d-inference-fairness-id`.
Both names were read from the llm-d router v1.0.0 source.

The operator already creates an InferencePool for every service (ADR-0007).

## Decision
* `spec.priority` is an optional integer. Higher is more important; negative may be shed.
* When it is set, the operator applies an `InferenceObjective` named after the service, pointing at
  the service's own pool. When it is cleared, the operator deletes the objective.
* The object is written as unstructured data, like the ScaledObject and the InferencePool, so the
  operator does not import llm-d's Go module.
* A new condition, `Prioritised`, reports the result. `True` with reason `ObjectiveApplied` when the
  API server stored it. `False` with reason `InferenceObjectiveNotInstalled` when the CRD is absent,
  because a priority that nothing reads should not look as if it works. The condition is removed
  when no priority is set.
* The gateway sends the objective header to a backend whose `objective` is set, and the tenant as
  the fairness id. A backend with no objective gets neither header.

## Consequences
* Three controller tests: the objective is created and removed with the field, its pool reference
  is the service's own pool, and a missing CRD is reported. One gateway test checks that the two
  headers go only to a backend behind llm-d.
* On the laptop kind cluster, with only the llm-d CRD installed, `qwen-small` with `priority: 10`
  produced an objective that `kubectl` lists against pool `qwen-small` at priority 10.
* This also bears on scale-to-zero (ADR-0001). The floor of one replica exists because every
  scaling signal comes from a running replica. A picker that holds requests while nothing is running
  is a demand signal that exists at zero replicas.

## Open questions
* The objective is stored; nothing here has queued or shed a request by it. That needs llm-d's
  endpoint picker running with the `flowControl` feature gate, which is off by default, in front of
  a pool with real load.
* `Prioritised=True` means the API server stored the object. Whether a picker is reading it is not
  visible from the objective, which has no status the operator can quote. This is the kind of claim
  ADR-0009 warns about, so the condition's message says where it takes effect.
* Scale-to-zero through the picker's queue has not been tried.
