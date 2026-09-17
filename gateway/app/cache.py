"""Semantic cache for prompt/response pairs.

Two prompts can embed close together and still need different answers, and
raising the threshold does not fix that class of near-miss. Entries carry the
numerals from the prompt alongside the vector, and a hit whose numerals differ
is thrown away. Tune the threshold against false_hits_blocked.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field

_NUM = re.compile(r"\d+(?:\.\d+)?")


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def embed(text: str) -> dict[str, float]:
    """Bag-of-words vector. No model to load, so the cache is testable on its own.

    TODO: pluggable embedder before this sees real traffic.
    """
    toks = _tokens(text)
    if not toks:
        return {}
    tf: dict[str, float] = {}
    for t in toks:
        tf[t] = tf.get(t, 0.0) + 1.0
    norm = math.sqrt(sum(v * v for v in tf.values()))
    return {k: v / norm for k, v in tf.items()}


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    small, large = (a, b) if len(a) < len(b) else (b, a)
    return sum(v * large.get(k, 0.0) for k, v in small.items())


@dataclass
class Entry:
    prompt: str
    response: str
    vector: dict[str, float]
    numbers: frozenset[str]


@dataclass
class SemanticCache:
    threshold: float = 0.93
    entries: list[Entry] = field(default_factory=list)
    hits: int = 0
    misses: int = 0
    # near-misses the threshold alone would have served
    false_hits_blocked: int = 0

    def _key(self, prompt: str) -> frozenset[str]:
        # "account 118" and "account 811" embed almost identically and mean
        # entirely different things. numerals are the worst near-misses.
        return frozenset(_NUM.findall(prompt))

    def get(self, prompt: str) -> str | None:
        vec = embed(prompt)
        nums = self._key(prompt)
        best, best_score = None, 0.0
        for e in self.entries:
            s = cosine(vec, e.vector)
            if s > best_score:
                best, best_score = e, s
        if best is None or best_score < self.threshold:
            self.misses += 1
            return None
        if best.numbers != nums:
            # similar enough to serve, but the numbers differ. don't.
            self.false_hits_blocked += 1
            self.misses += 1
            return None
        self.hits += 1
        return best.response

    def put(self, prompt: str, response: str) -> None:
        self.entries.append(Entry(prompt, response, embed(prompt), self._key(prompt)))

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def fingerprint(self, prompt: str) -> str:
        return hashlib.sha256(prompt.encode()).hexdigest()[:16]
