#!/usr/bin/env python3
"""Send a ramp of chat requests at a model server, to give the dashboard something to show.

    hack/load.py                      # vLLM on localhost:8001, three minutes
    URL=http://localhost:8001 MODEL=qwen-small SECONDS_PER_STEP=30 hack/load.py

Concurrency steps through 1, 4, 8, 16 and 24 callers. The model server here
admits 16 sequences at once, so the last step makes requests wait: the queue
panel, time to first token and the autoscaler's signal all move. Half the
prompts share a long opening, so the prefix cache has something to hit.

Not a benchmark. For measurements use a tool built for it, such as GuideLLM.
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
import urllib.request

URL = os.getenv("URL", "http://localhost:8001").rstrip("/")
MODEL = os.getenv("MODEL", "qwen-small")
STEP = int(os.getenv("SECONDS_PER_STEP", "36"))
STEPS = [1, 4, 8, 16, 24]

SHARED = ("You are a clerk at a law firm. Answer briefly and plainly. The firm's office is open "
          "from nine to five on weekdays. Holiday requests go to the practice manager. ") * 6
QUESTIONS = ["When is the office open?", "Who approves holiday?", "Summarise the rules above.",
             "Write two sentences about filing deadlines.", "List three things a clerk does.",
             "Explain what a contract is in one paragraph."]

lock = threading.Lock()
done = {"requests": 0, "tokens": 0, "errors": 0}


def one_call() -> None:
    q = random.choice(QUESTIONS)
    prompt = SHARED + q if random.random() < 0.5 else q
    body = json.dumps({"model": MODEL, "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": random.choice([32, 64, 128, 200]),
                       "temperature": 0.7}).encode()
    req = urllib.request.Request(f"{URL}/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            usage = json.load(resp).get("usage", {})
        with lock:
            done["requests"] += 1
            done["tokens"] += usage.get("completion_tokens", 0)
    except Exception:
        with lock:
            done["errors"] += 1


def caller(stop: threading.Event) -> None:
    while not stop.is_set():
        one_call()


def main() -> None:
    for n in STEPS:
        stop = threading.Event()
        before = dict(done)
        threads = [threading.Thread(target=caller, args=(stop,), daemon=True) for _ in range(n)]
        t0 = time.time()
        for t in threads:
            t.start()
        time.sleep(STEP)
        stop.set()
        for t in threads:
            t.join(timeout=130)
        took = time.time() - t0
        reqs, toks = done["requests"] - before["requests"], done["tokens"] - before["tokens"]
        print(f"{n:>2} callers: {reqs:>4} requests, {toks / took:6.0f} tokens/s, "
              f"{done['errors'] - before['errors']} errors", flush=True)


if __name__ == "__main__":
    main()
