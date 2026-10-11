# ADR-0003: The FlashInfer sampler failure was a missing PATH entry, not the hardware

**Status:** Superseded by the root cause below. The original decision is kept for the record.

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

## Revisited for vLLM v0.30.0

The operator now runs vLLM v0.30.0. In that release the FlashInfer sampler is still on unless
`VLLM_USE_FLASHINFER_SAMPLER=0` is set (`vllm/envs.py`). The decision therefore still stands for
`hack/serve-vllm.sh`.

## Revisited for vLLM v0.31.0

The operator now runs vLLM v0.31.0. `VLLM_USE_FLASHINFER_SAMPLER` still defaults to on in that
release (`vllm/envs.py`), so the decision stands for `hack/serve-vllm.sh` unchanged.

## Root cause, found by running it again

Run again on the same card with vLLM v0.29.0 and `VLLM_USE_FLASHINFER_SAMPLER=1`, the log matched
the first failure line for line: weights loaded, "Available KV cache memory: 1.97 GiB", then the
traceback in `top_k_top_p_sampling_from_logits`. Reading the traceback to its last line instead of
its first:

    flashinfer/jit/cpp_ext.py, in run_ninja
    FileNotFoundError: [Errno 2] No such file or directory: 'ninja'

FlashInfer compiles its sampling kernels with the `ninja` build tool. `ninja` was installed, in the
venv's `bin` directory, by pip. But `hack/serve-vllm.sh` ran vLLM by its full path without
activating the venv, so that directory was never on `PATH`. The compile could not start. The card,
the driver and the toolkit were never the problem.

With the venv's `bin` on `PATH` and the sampler on, the first compile took about 1 minute 40
seconds and then vLLM served normally. Every check in `hack/verify.py` passed. Throughput was the
same as with the native sampler: a median of 436 tokens a second against 421, within the spread
between bursts.

The original diagnosis read forwards from the last success, which ruled out memory, and then stopped
at the first frame that looked like an explanation: "the compile fails for this SM". The last line
of the traceback said why the compile failed.

## Decision, revised
* `hack/serve-vllm.sh` puts the directory holding `vllm` on `PATH`, so tools installed beside it
  are found.
* The sampler is no longer turned off by default. `VLLM_USE_FLASHINFER_SAMPLER=0` still works as an
  override.
* The script checks for `ninja` before starting and stops with a message naming the fix if the
  sampler is on and `ninja` is missing. A missing build tool should fail in one line at start, not
  in a traceback a minute into loading.

## Open questions

* Whether `ninja` is on `PATH` inside the `vllm/vllm-openai` image the operator runs has not been
  checked here. If it is not, the operator's pods would fail the same way. Running one such pod on
  a GPU node would settle it.
* The first request after start waits for the compile, about 1 minute 40 seconds on this card. A
  startup probe sized only to weight loading (ADR-0002) does not include it. On this card the
  compile happened during warm-up, before the server reported ready, so the probe covered it.
