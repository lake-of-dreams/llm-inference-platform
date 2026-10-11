#!/usr/bin/env bash
# Serve a small instruct model on a consumer GPU. Every flag below is here to
# fit a 4 GB device:
#   --gpu-memory-utilization 0.85  headroom for the CUDA context itself
#   --max-model-len 2048           caps the KV cache; the default 32k will OOM
#   --max-num-seqs 16              bounds concurrent sequences -> bounds KV cache
#   --enforce-eager                no CUDA graph capture. costs throughput, but
#                                  capture needs memory we do not have.
set -euo pipefail

# VLLM_BIN points at a vllm installed elsewhere, such as a shared GPU venv.
VLLM_BIN="${VLLM_BIN:-$(dirname "$0")/../.venv/bin/vllm}"

# FlashInfer compiles its sampling kernels the first time they run, and the
# compile shells out to `ninja`. pip installs ninja into the venv's bin, but
# running vllm by its full path does not put that directory on PATH, so the
# compile died with "No such file or directory: 'ninja'" after the model and
# KV cache had loaded fine. ADR-0003 first blamed the card and turned the
# sampler off. Put the venv on PATH instead, and say so up front if ninja is
# really missing.
PATH="$(cd "$(dirname "$VLLM_BIN")" && pwd):$PATH"
export PATH
if [ "${VLLM_USE_FLASHINFER_SAMPLER:-1}" != "0" ] && ! command -v ninja >/dev/null; then
  echo "ninja not found on PATH: FlashInfer's first compile will fail." >&2
  echo "Install it in the vllm venv (pip install ninja) or set VLLM_USE_FLASHINFER_SAMPLER=0." >&2
  exit 1
fi

exec "$VLLM_BIN" serve "${MODEL:-Qwen/Qwen2.5-0.5B-Instruct}" \
  --host 0.0.0.0 --port "${PORT:-8001}" \
  --max-model-len "${MAX_LEN:-2048}" \
  --gpu-memory-utilization "${GPU_UTIL:-0.85}" \
  --max-num-seqs 16 \
  --enforce-eager \
  --served-model-name qwen-small
