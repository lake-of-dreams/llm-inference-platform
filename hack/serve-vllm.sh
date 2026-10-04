#!/usr/bin/env bash
# Serve a small instruct model on a consumer GPU. Every flag below is here to
# fit a 4 GB device:
#   --gpu-memory-utilization 0.85  headroom for the CUDA context itself
#   --max-model-len 2048           caps the KV cache; the default 32k will OOM
#   --max-num-seqs 16              bounds concurrent sequences -> bounds KV cache
#   --enforce-eager                no CUDA graph capture. costs throughput, but
#                                  capture needs memory we do not have.
set -euo pipefail

# FlashInfer JIT-compiles its sampling kernels at warm-up. On some
# driver/toolkit/SM combinations that compile fails and takes the engine core
# with it, but only after the model and KV cache have loaded fine. So the logs
# read like a memory problem and it is not one. The give-away is "Available KV
# cache memory" showing a healthy figure right before the traceback. Native
# PyTorch sampler instead. See docs/adr/0003-flashinfer-sampler.md
export VLLM_USE_FLASHINFER_SAMPLER="${VLLM_USE_FLASHINFER_SAMPLER:-0}"

exec "$(dirname "$0")/../.venv/bin/vllm" serve "${MODEL:-Qwen/Qwen2.5-0.5B-Instruct}" \
  --host 0.0.0.0 --port "${PORT:-8001}" \
  --max-model-len "${MAX_LEN:-2048}" \
  --gpu-memory-utilization "${GPU_UTIL:-0.85}" \
  --max-num-seqs 16 \
  --enforce-eager \
  --served-model-name qwen-small
