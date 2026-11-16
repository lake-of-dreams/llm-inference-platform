# ADR-0017: Services can share one device by naming an existing claim

**Status:** Accepted

## Context
The laptop cluster has one 4 GB GPU. `deviceClass` gives every pod a device of its own, so the
vLLM service held the whole card and the Ollama service ran on CPU at 18 tokens a second.

Kubernetes lets several pods name the same ResourceClaim. The scheduler allocates the claim's
device once and adds each pod to the claim's `reservedFor` list. The NVIDIA DRA driver then shows
the same GPU to every one of those pods, and their processes take turns on it.

Two things do not come with that. Nothing fences one process's GPU memory from another's: a
process that asks for more than is left fails, and a fault in the driver could expose one model's
memory to the other. And nothing divides the memory: each service has to be sized so the two fit.

## Decision
* A new optional field, `sharedDeviceClaim`, names a ResourceClaim that already exists in the
  namespace. The pod template points at that claim by name. No ResourceClaimTemplate is made.
* The operator does not create or delete the shared claim. Whoever runs the cluster makes it.
  Which services may share a card is a platform decision, and a claim the operator owned would be
  deleted with the first service that named it.
* `sharedDeviceClaim` and `deviceClass` cannot both be set. A vLLM service needs one of them.
* A Restricted service cannot name a shared claim. The rule is in the CRD and repeated in the
  operator, like the other placement rules.
* A service on a shared claim rolls out the same way as one with its own device: the old pod goes
  first (ADR-0015). The scheduler would place a second pod, but the old pod still holds its share
  of the card's memory.
* `status.devices` lists the shared device under each service, for the pods of that service found
  in the claim's `reservedFor`.

## Consequences
* Three controller tests: two services on one claim, the two admission rules checked at the API
  server and in the operator, and the samples in `config/samples/shared-gpu` admitted by the CRD.
* On the cluster both models ran on the one card at once. vLLM held 1,358 MiB and Ollama 1,342 MiB
  of 4,094 MiB. A request to each, sent together, both answered.
* vLLM's share has a floor that is not obvious. At `gpuMemoryUtilization: 30` it loaded 0.93 GiB of
  weights, found less than nothing left for the KV cache, and stopped with
  `No available memory for the cache blocks`. At 40 it started with room for 10,864 tokens.
* Sharing did not make the second model faster here. Ollama fitted 42% of Phi-4 mini onto the GPU
  and ran the rest on CPU. It generated 17 tokens a second, the same as on CPU alone. A model has
  to fit whole to gain much, and Phi-4 mini needs 3.2 GB.
* `make samples-shared` applies the claim, then vLLM, then Ollama. The order matters: vLLM takes a
  fixed share at start-up and fails if it is not free, while Ollama takes what is left.

## Open questions
* Nothing checks that the shares add up. Two vLLM services at 60% each are both admitted, and the
  second crash-loops. Status reports that crash with its exit code, after the fact.
* Time-slicing is the driver's default. NVIDIA's Multi-Process Service, which can cap each
  process's memory, was not tried on this card.
* The autoscaler is told to keep the shared vLLM service at one replica by the sample, not by a
  rule. A second replica would be scheduled onto the same card and fail for lack of memory.
