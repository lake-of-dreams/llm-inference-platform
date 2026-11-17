"""Trains a small language model from scratch on the GPU, to watch on the dashboard.

Inference only reads a model's weights. Training changes them, and the card
behaves differently: the GPU stays busy between requests, memory holds
gradients and optimiser state as well as weights, and the disk is written to
when a checkpoint is saved.

The model is a 10-million-parameter transformer that predicts the next byte of
text. Its text is the Python standard library's own source, already inside the
container image, so the job needs no download. Nothing here is tuned to be a
good model. It is sized to finish in a few minutes on a 4 GB laptop card that
two model servers are also using.

Progress is published at :8000/metrics in the Prometheus text format, so the
dashboard draws loss and tokens per second beside the GPU's own readings.
"""
from __future__ import annotations

import math
import os
import pathlib
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import torch
import torch.nn as nn
import torch.nn.functional as F

STEPS = int(os.getenv("STEPS", "1500"))
BATCH = int(os.getenv("BATCH", "24"))
BLOCK = int(os.getenv("BLOCK", "256"))          # bytes of context
LAYERS, HEADS, WIDTH = 6, 6, 384
EVAL_EVERY = 100
# The card is shared and nothing fences one process's memory from another's
# (ADR-0017). This is the job fencing itself: past this share of the card,
# PyTorch raises an out-of-memory error here instead of taking memory a model
# server is about to need.
MEMORY_SHARE = float(os.getenv("GPU_MEMORY_SHARE", "0.33"))
CHECKPOINT = pathlib.Path(os.getenv("CHECKPOINT_DIR", "/checkpoints")) / "model.pt"

metrics = {"train_step": 0.0, "train_steps_total": float(STEPS), "train_loss": math.nan,
           "train_validation_loss": math.nan, "train_tokens_per_second": 0.0,
           "train_gpu_memory_allocated_bytes": 0.0, "train_gpu_memory_limit_bytes": 0.0,
           "train_checkpoints_written_total": 0.0, "train_finished": 0.0}


class Metrics(BaseHTTPRequestHandler):
    def do_GET(self):
        body = "".join(f"# TYPE {k} gauge\n{k} {v}\n" for k, v in metrics.items()
                       if not math.isnan(v)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class Block(nn.Module):
    def __init__(self):
        super().__init__()
        self.ln1, self.ln2 = nn.LayerNorm(WIDTH), nn.LayerNorm(WIDTH)
        self.attn = nn.MultiheadAttention(WIDTH, HEADS, batch_first=True)
        self.mlp = nn.Sequential(nn.Linear(WIDTH, 4 * WIDTH), nn.GELU(), nn.Linear(4 * WIDTH, WIDTH))

    def forward(self, x, mask):
        h = self.ln1(x)
        x = x + self.attn(h, h, h, attn_mask=mask, need_weights=False)[0]
        return x + self.mlp(self.ln2(x))


class TinyLM(nn.Module):
    def __init__(self):
        super().__init__()
        self.tok, self.pos = nn.Embedding(256, WIDTH), nn.Embedding(BLOCK, WIDTH)
        self.blocks = nn.ModuleList(Block() for _ in range(LAYERS))
        self.ln = nn.LayerNorm(WIDTH)
        self.head = nn.Linear(WIDTH, 256, bias=False)

    def forward(self, idx):
        n = idx.shape[1]
        x = self.tok(idx) + self.pos(torch.arange(n, device=idx.device))
        mask = torch.triu(torch.ones(n, n, dtype=torch.bool, device=idx.device), diagonal=1)
        for b in self.blocks:
            x = b(x, mask)
        return self.head(self.ln(x))


def corpus() -> torch.Tensor:
    root = pathlib.Path(os.__file__).parent
    data = b"".join(p.read_bytes() for p in sorted(root.glob("*.py")))
    return torch.frombuffer(bytearray(data), dtype=torch.uint8).long()


def main() -> None:
    if not torch.cuda.is_available():
        raise SystemExit("no GPU is visible to this container: check the pod's device claim")
    dev = torch.device("cuda")
    torch.cuda.set_per_process_memory_fraction(MEMORY_SHARE)
    total = torch.cuda.get_device_properties(dev).total_memory
    metrics["train_gpu_memory_limit_bytes"] = MEMORY_SHARE * total
    threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", 8000), Metrics).serve_forever,
                     daemon=True).start()

    data = corpus()
    split = int(0.9 * len(data))
    train, held_out = data[:split], data[split:]
    torch.manual_seed(0)
    model = TinyLM().to(dev)
    opt = torch.optim.AdamW(model.parameters(), lr=3e-4)
    params = sum(p.numel() for p in model.parameters())
    print(f"{torch.cuda.get_device_name(dev)}, {total / 2**20:.0f} MiB, this job capped at "
          f"{MEMORY_SHARE:.0%}; {params / 1e6:.1f}M parameters; {len(data) / 1e6:.1f} MB of text",
          flush=True)

    def batch(src: torch.Tensor):
        at = torch.randint(len(src) - BLOCK - 1, (BATCH,))
        x = torch.stack([src[i:i + BLOCK] for i in at]).to(dev)
        y = torch.stack([src[i + 1:i + BLOCK + 1] for i in at]).to(dev)
        return x, y

    def loss_on(src: torch.Tensor) -> torch.Tensor:
        x, y = batch(src)
        # bfloat16 halves the memory and time of the arithmetic, on Tensor Cores
        with torch.autocast("cuda", dtype=torch.bfloat16):
            return F.cross_entropy(model(x).flatten(0, 1), y.flatten())

    CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
    t_window, first_val = time.perf_counter(), None
    for step in range(1, STEPS + 1):
        loss = loss_on(train)
        opt.zero_grad(set_to_none=True)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        opt.step()
        metrics["train_step"] = step
        if step % 10 == 0:
            torch.cuda.synchronize()
            now = time.perf_counter()
            metrics["train_tokens_per_second"] = 10 * BATCH * BLOCK / (now - t_window)
            metrics["train_loss"] = loss.item()
            metrics["train_gpu_memory_allocated_bytes"] = torch.cuda.max_memory_allocated(dev)
            t_window = now
        if step % EVAL_EVERY == 0 or step == STEPS:
            model.eval()
            with torch.no_grad():
                val = sum(loss_on(held_out).item() for _ in range(10)) / 10
            model.train()
            first_val = first_val or val
            metrics["train_validation_loss"] = val
            torch.save({"step": step, "model": model.state_dict()}, CHECKPOINT)
            metrics["train_checkpoints_written_total"] += 1
            print(f"step {step:5d}  loss {loss.item():.3f}  held-out {val:.3f}  "
                  f"{metrics['train_tokens_per_second']:,.0f} tokens/s  "
                  f"{torch.cuda.max_memory_allocated(dev) / 2**20:.0f} MiB", flush=True)
            t_window = time.perf_counter()

    # What it learned to write, from a prompt it has seen the like of.
    model.eval()
    idx = torch.tensor([list(b"def ")], device=dev)
    with torch.no_grad():
        for _ in range(160):
            logits = model(idx[:, -BLOCK:])[:, -1] / 0.8
            idx = torch.cat([idx, torch.multinomial(F.softmax(logits.float(), -1), 1)], 1)
    print("sample:\n" + bytes(idx[0].tolist()).decode("utf-8", "replace"), flush=True)
    print(f"held-out loss {first_val:.3f} -> {metrics['train_validation_loss']:.3f} "
          f"(guessing a byte at random scores {math.log(256):.3f})", flush=True)
    metrics["train_finished"] = 1.0
    # Stay up for two scrapes, so the last readings reach Prometheus.
    time.sleep(float(os.getenv("LINGER_SECONDS", "15")))


if __name__ == "__main__":
    main()
