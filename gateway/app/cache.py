"""Semantic cache for prompt/response pairs.

A semantic cache serves a stored answer when a new prompt means nearly the same
as an old one. Nearness is the cosine similarity of the two prompts' vectors.

Two guards keep it from serving the wrong answer.

The first is the scope. Every entry is stored under a scope: the data
classification, the model and the sampling settings of the request that
produced it. A lookup only searches its own scope. Without this, an answer
generated for a Restricted prompt could be served to a Public caller whose
prompt happened to read the same, which is exactly the leak ADR-0005 exists to
prevent. ADR-0006 records the decision.

The second is the numeral check. Two prompts can embed close together and
still need different answers, and raising the threshold does not fix that class
of near-miss. Entries carry the numerals from the prompt alongside the vector,
and a hit whose numerals differ is thrown away. Tune the threshold against
false_hits_blocked.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass, field

from .router import Classification

_NUM = re.compile(r"\d+(?:\.\d+)?")

# A vector is either sparse (term -> weight) or dense (a list of floats). The
# bag-of-words embedder below returns sparse vectors; a model embedder returns
# dense ones.
Vector = Mapping[str, float] | Sequence[float]
Embedder = Callable[[str], Vector]


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def embed(text: str) -> dict[str, float]:
    """Bag-of-words vector, normalised to length one.

    It needs no model, so the cache is testable on its own. Pass a real
    embedder to SemanticCache before this sees production traffic.
    """
    toks = _tokens(text)
    if not toks:
        return {}
    tf: dict[str, float] = {}
    for t in toks:
        tf[t] = tf.get(t, 0.0) + 1.0
    norm = math.sqrt(sum(v * v for v in tf.values()))
    return {k: v / norm for k, v in tf.items()}


def cosine(a: Vector, b: Vector) -> float:
    if not a or not b:
        return 0.0
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        small, large = (a, b) if len(a) < len(b) else (b, a)
        dot = sum(v * large.get(k, 0.0) for k, v in small.items())
        na = math.sqrt(sum(v * v for v in a.values()))
        nb = math.sqrt(sum(v * v for v in b.values()))
    elif not isinstance(a, Mapping) and not isinstance(b, Mapping):
        if len(a) != len(b):
            raise ValueError(f"vector lengths differ: {len(a)} and {len(b)}")
        dot = sum(x * y for x, y in zip(a, b, strict=True))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
    else:
        raise TypeError("cannot compare a sparse vector with a dense one")
    return dot / (na * nb) if na and nb else 0.0


@dataclass(frozen=True)
class CacheScope:
    """Everything besides the prompt that decides what the answer is."""

    classification: Classification
    model: str
    temperature: float = 0.0
    max_tokens: int = 0
    system_prompt_sha: str = ""

    @classmethod
    def of(cls, classification: Classification, model: str, *, temperature: float = 0.0,
           max_tokens: int = 0, system_prompt: str = "") -> CacheScope:
        sha = hashlib.sha256(system_prompt.encode()).hexdigest()[:16] if system_prompt else ""
        return cls(classification, model, temperature, max_tokens, sha)


@dataclass
class Entry:
    prompt: str
    response: str
    vector: Vector
    numbers: frozenset[str]
    stored_at: float


@dataclass
class SemanticCache:
    threshold: float = 0.93
    # Upper bound on entries across all scopes. The least recently used entry
    # goes first. Lookup is a linear scan within one scope, so this also bounds
    # the cost of a miss.
    max_entries: int = 10_000
    # Seconds an entry stays servable. Zero keeps entries until they are evicted.
    ttl_seconds: float = 3600.0
    embedder: Embedder = embed
    clock: Callable[[], float] = time.monotonic

    hits: int = 0
    misses: int = 0
    # near-misses the threshold alone would have served
    false_hits_blocked: int = 0
    expired: int = 0
    evicted: int = 0

    _scopes: dict[Hashable, OrderedDict[int, Entry]] = field(default_factory=dict, repr=False)
    _order: OrderedDict[tuple[Hashable, int], None] = field(default_factory=OrderedDict, repr=False)
    _next_id: int = field(default=0, repr=False)

    def _numbers(self, prompt: str) -> frozenset[str]:
        # "account 118" and "account 811" embed almost identically and mean
        # entirely different things. Numerals are the worst near-misses.
        return frozenset(_NUM.findall(prompt))

    def _expired(self, e: Entry) -> bool:
        return self.ttl_seconds > 0 and self.clock() - e.stored_at > self.ttl_seconds

    def _drop(self, scope: Hashable, entry_id: int) -> None:
        bucket = self._scopes.get(scope)
        if bucket is not None:
            bucket.pop(entry_id, None)
            if not bucket:
                del self._scopes[scope]
        self._order.pop((scope, entry_id), None)

    def get(self, prompt: str, scope: Hashable) -> str | None:
        bucket = self._scopes.get(scope)
        if not bucket:
            self.misses += 1
            return None
        vec = self.embedder(prompt)
        best_id, best, best_score = -1, None, 0.0
        for entry_id, e in list(bucket.items()):
            if self._expired(e):
                self.expired += 1
                self._drop(scope, entry_id)
                continue
            s = cosine(vec, e.vector)
            if s > best_score:
                best_id, best, best_score = entry_id, e, s
        if best is None or best_score < self.threshold:
            self.misses += 1
            return None
        if best.numbers != self._numbers(prompt):
            # similar enough to serve, but the numbers differ. don't.
            self.false_hits_blocked += 1
            self.misses += 1
            return None
        self._order.move_to_end((scope, best_id))
        self.hits += 1
        return best.response

    def put(self, prompt: str, response: str, scope: Hashable) -> None:
        entry_id = self._next_id
        self._next_id += 1
        e = Entry(prompt, response, self.embedder(prompt), self._numbers(prompt), self.clock())
        self._scopes.setdefault(scope, OrderedDict())[entry_id] = e
        self._order[(scope, entry_id)] = None
        while len(self._order) > self.max_entries:
            (old_scope, old_id), _ = self._order.popitem(last=False)
            self._drop(old_scope, old_id)
            self.evicted += 1

    def __len__(self) -> int:
        return len(self._order)

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0
