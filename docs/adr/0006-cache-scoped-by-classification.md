# ADR-0006: The semantic cache is scoped by classification, model and sampling settings

**Status:** Accepted · **Date:** 2026-10

## Context

A semantic cache stores past answers and serves one again when a new prompt means nearly the same
as an old prompt. Nearness is measured by comparing the two prompts as vectors, lists of numbers
that place similar text close together.

The first version stored every answer in one list and searched all of it. The router (ADR-0005)
keeps Restricted prompts off any backend not approved for them. The cache sat in front of that
decision and ignored it. A Restricted caller asks "summarise the payroll file" and the answer is
cached. A Public caller sends the same words and gets the payroll summary from the cache, without
the router being asked.

The same flaw applies, with lower stakes, to the model and the sampling settings. An answer from
one model is not an answer from another. An answer produced at temperature 0.9, where temperature
is the amount of randomness in the output, is not one the caller at temperature 0 asked for.

## Decision

Every entry is stored under a `CacheScope`: classification, model, temperature, the maximum
answer length, and a hash of the system prompt. A lookup searches only entries in its own scope.
There is no fallback to a neighbouring scope.

The cache is also bounded. It keeps at most `max_entries` entries across all scopes and drops the
least recently used first. Entries expire after `ttl_seconds`. The embedder, the function that
turns text into a vector, is a parameter, so a real embedding model can replace the bag-of-words
default.

The numeral guard stays. A hit whose prompt carries different numbers is refused, because
"account 118" and "account 811" sit almost on top of each other as vectors.

## Consequences

* The hit rate falls, because answers are no longer shared across scopes. That is the price of
  not leaking them.
* A scope is an exact match. A Restricted caller does not receive a cached Public answer either,
  even though the classification rules would allow it. Allowing it would mean two code paths for
  one guard, and the saving is small.
* A miss scans one scope linearly. `max_entries` bounds that cost.

## Evidence

`test_restricted_answer_never_reaches_a_public_caller` in `gateway/tests/test_cache.py`, and the
live check of the same property in `hack/verify.py`.

## Open questions

* The default embedder is bag-of-words. It has not been compared with an embedding model on real
  traffic, so the threshold of 0.93 is untested beyond the examples in `hack/verify.py`.
