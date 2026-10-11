# ADR-0008: Salt vLLM's prefix cache per tenant and classification, with a keyed hash

**Status:** Accepted

## Context
ADR-0006 divided the gateway's semantic cache by classification. vLLM keeps a second cache below
the gateway. With prefix caching, the attention state of text it has already read is kept, and a
request that starts with the same text reuses it. Reuse shortens the time to first token, and the
caller can measure that time.

That makes the prefix cache a timing side channel. A caller who guesses the opening of another
group's system prompt or document, sends it, and sees a fast first token learns that someone sent
that text recently. Nothing in the router or the semantic cache stops this, because nothing is
served from another group's cache. Only the timing changes.

vLLM's chat API accepts a `cache_salt` field. The salt is mixed into the hash of the first block,
so only requests carrying the same salt can reuse each other's blocks. The v0.31.0 source
describes it in `vllm/entrypoints/openai/chat_completion/protocol.py`: 1 to 1,024 characters, and
"random, protected from access by 3rd parties, and long enough to be unpredictable (e.g., 43
characters base64-encoded, corresponding to 256 bit)". v0.31.0 also tags prefix-cache extra keys by
source, so a salt can no longer collide with a LoRA adapter's name.

## Decision
* `prefix_cache_salt(key, classification, tenant)` in `gateway/app/cache.py` returns
  HMAC-SHA256 of the tenant and classification under a gateway secret, base64url without padding:
  43 characters.
* Requests from the same tenant and classification share a salt, so they still share cached work.
  Any other pair gets a different salt.
* The salt is keyed. A plain group name, or an unkeyed hash of one, could be computed by anyone who
  reaches vLLM directly, and sent to join that group's cache.
* `complete` sends the salt only to a backend with `prefix_cache_salt=True`. Ollama does not take
  the field.
* An empty key or an empty tenant raises. A missing tenant must not quietly become a salt shared by
  everyone without one.

## Consequences
* Prefix reuse across tenants stops. A system prompt common to every tenant is read once per
  tenant, not once overall. That is the cost of closing the channel.
* Rotating the gateway key changes every salt, so every group's prefix cache starts empty once.
* Five tests cover it: three in `gateway/tests/test_cache.py` for the salt and two in
  `gateway/tests/test_client.py` for sending it. Sending the salt to every backend fails
  `test_cache_salt_is_withheld_from_a_backend_that_does_not`.

## Open questions
* The gateway has no sign-in step, so nothing yet supplies the tenant. Whoever calls `complete`
  must compute and pass the salt.
* Where the key lives and how it rotates is not decided.
* Measured on a running vLLM v0.29.0, not yet v0.31.0. `hack/verify.py` section 5 reads vLLM's
  `vllm:prefix_cache_hits` counter around each call: the same group reused 336 tokens of a
  403-token prompt, a different group sending the same text reused 0, and a control with no salt
  reused 336, which shows the leak is real on that server. Timing barely moved on a 0.5B model, so
  the counter is the evidence, not the latency.
