# ADR-0003: Disable the FlashInfer sampler on this hardware

**Status:** Accepted · **Date:** 2026-09

## Context
First start of vLLM 0.29.0 on an RTX 500 Ada (SM 8.9, 4 GB) died with:

    RuntimeError: Engine core initialization failed. Failed core proc(s): {}

Which points nowhere. On a 4 GB card the first instinct is memory, and I spent
time on that instinct before reading the log properly.

The log had already ruled memory out. The engine gets further than a memory
failure allows:

    Initial free memory: 3.57 GiB; Requested: 0.85 (util), 3.1 GiB
    Available KV cache memory: 1.97 GiB
    GPU KV cache size: 172,272 tokens, Maximum concurrency: 84.12x
    ERROR ... EngineCore failed to start.

Weights loaded. KV cache allocated, with room for 172k tokens. The actual
traceback ends in `flashinfer.sampling.top_k_top_p_sampling_from_logits` inside
`compile_or_warm_up_model`: FlashInfer JIT-compiles its sampling kernels at
warm-up, and that compile fails for this driver/toolkit/SM combination. It fails
*after* a successful load, which is why it reads like an OOM.

## Decision
Export `VLLM_USE_FLASHINFER_SAMPLER=0` and use the native PyTorch sampler. Set
in `hack/serve-vllm.sh`.

## Consequences
* Gives up some sampling throughput. Irrelevant at this scale.
* Revisit when the driver or the FlashInfer version moves.
* A healthy "Available KV cache memory" line immediately before an engine-core
  failure rules memory out, so read forwards from the last success.
