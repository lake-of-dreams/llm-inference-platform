# Guide 2. Prefix caching and the timing leak, from zero

## How to read this guide

**Who it is for.** Engineers running one model server for several teams or customers, and the
security reviewers who sign that off. You do not need to know how a language model works inside.
Each term is explained when it first appears.

**What you will know at the end.** What a model server remembers between requests, why that memory
makes answers faster, and how the speed itself can leak one team's text to another. You will know
how a cache salt closes the leak, why the salt in this repository is a keyed fingerprint rather than
a team name, and how the live check proves both the leak and the fix on a real GPU.

**How it is organised.**

1. Part 1 explains the model's working notes: the KV cache.
2. Part 2 explains prefix caching: reusing those notes for a later request.
3. Part 3 explains the timing leak.
4. Part 4 explains the cache salt, and how vLLM uses it.
5. Part 5 explains why the salt is keyed, not a name.
6. Part 6 shows how the gateway picks and sends the salt.
7. Part 7 shows the live measurement, including the control.
8. Part 8 lists the design principles.
9. Part 9 lists what is unresolved.

A glossary at the end defines every term.

You can read it front to back like a short book. Each part builds on the one before it.

## The idea in one paragraph

When a model server reads a long piece of text, it keeps its notes on that text for a while. If the
next request starts with the same text, the server reuses the notes and answers sooner. That is a
real saving, but anyone who sends a request can time the answer. A fast answer to a guessed opening
means somebody else recently sent that exact text. So one team can learn what another team is
sending, without ever seeing a reply. The fix is to label each request with a secret value per team,
called a salt. Notes made under one salt are only reused by requests carrying the same salt. Each
team keeps its saving, and nobody can probe anyone else's.

## A note on accuracy

The gateway code is in `gateway/app/cache.py` (`prefix_cache_salt`) and `gateway/app/client.py`.
The tests are in `gateway/tests/test_cache.py` and `gateway/tests/test_client.py`, and the live
check is section 5 of `hack/verify.py`. The decision record is ADR-0008.

vLLM's behaviour was checked in its source. The `cache_salt` field and its guidance on length are in
`vllm/entrypoints/openai/chat_completion/protocol.py` in v0.31.0. The live numbers in Part 7 were
measured on an NVIDIA RTX 500 Ada laptop GPU with 4 GB of memory, serving Qwen2.5-0.5B-Instruct.

Simplified for teaching: the guide describes the cache at the level of blocks of tokens, and does not
go into how attention works.

## The running example

Fernhill Legal is a law firm in Leeds. Its HR team and its contracts team both send requests to the
same vLLM server. HR's payroll requests all start with the same 1,500-token instruction on how to
summarise salary files. The contracts team has no business knowing what that instruction says, or
when HR is using it.

The figures in the examples are illustrative, except in Part 7, where they are measured.

---

## Part 1. The model's working notes

A model reads text in pieces called **tokens**, a word or part of a word each. For every token it
reads, it computes some intermediate values that later tokens need to refer back to. Think of them
as working notes: what each earlier word was, and how it relates to the rest.

These notes are called the **KV cache**, after the two kinds of value stored, keys and values. They
live in GPU memory. The server fills the cache while it reads a request, which is called
**prefill**, and adds to it with every token it writes.

Reading is expensive. For a long instruction, building the notes is most of the work done before the
first word of the answer appears.

## Part 2. Prefix caching

### 2.1 The idea

Many requests start the same way. Every one of HR's payroll requests begins with the same
1,500-token instruction. Building the notes for that instruction again on every request repeats the
same work.

**Prefix caching** keeps the notes after a request finishes. When a new request starts with the same
text, its **prefix**, the server reuses the notes for that part and only reads the rest.

### 2.2 How vLLM finds a matching prefix

vLLM stores the notes in **blocks** of a fixed number of tokens, 16 by default. Each block is labelled
with a **hash**, a fingerprint of the tokens in it and of every token before it. When a request
arrives, vLLM computes the hashes of its blocks in order and looks each one up. A run of matching
hashes from the start is a cached prefix.

Only whole blocks are cached. A prompt of 403 tokens shares at most 25 full blocks, or 400 tokens,
and less if it differs before the end.

### 2.3 What it saves

**Example.** HR's instruction is 1,500 tokens. The server reads it at 20,000 tokens a second, so
prefill takes 75 ms. The first payroll request pays that. The second, starting with the same
instruction, reuses 1,488 tokens of notes (93 whole blocks) and reads only the rest. Its first word
appears about 70 ms sooner.

## Part 3. The timing leak

### 3.1 Speed is visible

The caller always sees how long the first word took. That is the **time to first token**, or TTFT.
With prefix caching, a short TTFT means the prefix was already cached, which means someone sent the
same text recently.

### 3.2 The attack, step by step

1. A contracts user suspects HR is running payroll summaries this week.
2. They obtain, or guess, the opening of HR's instruction.
3. They send a request that starts with that opening.
4. They time the first token.
5. A fast answer tells them HR sent that text recently. A slow one tells them it did not.

No reply ever contained HR's data. The user learned it from how long the server took. Learning a
secret from timing rather than from content is called a **timing side channel**.

### 3.3 Why it matters even for guesses

The attacker can only confirm a guess. They cannot read text they have not guessed. That is still a
leak. Confirming that a particular contract template, client name or policy is in use is often the
sensitive part. A guess can also be built up a block at a time, testing variations and keeping the
fast ones.

## Part 4. The cache salt

### 4.1 What a salt does

A **salt** is an extra value mixed into a fingerprint. vLLM's chat API accepts a field called
`cache_salt`. When a request carries one, vLLM mixes it into the hash of the first block. Because
every later block's hash depends on the blocks before it, the whole chain of fingerprints changes.

So two requests with identical text but different salts produce different fingerprints, and neither
finds the other's notes. Two requests with the same salt produce the same fingerprints, and share
notes as before.

### 4.2 Trust groups

Requests that share a salt form a **trust group**: they can reuse each other's notes, and so they
could time each other. Requests in different groups cannot. Choosing the groups is choosing who may
learn what about whom.

This repository makes one group per pair of tenant and data classification. A **tenant** is the team
or customer a caller belongs to. HR's Restricted requests form one group, contracts' Internal
requests another.

**Example.** HR's requests all carry the same salt, so the second payroll request still reuses the
first one's 1,500 tokens of notes. A contracts user sending the same instruction carries a
different salt, finds nothing cached, and waits the full 75 ms like any new text. Their timing now
says nothing about HR.

### 4.3 What vLLM says the salt should be

vLLM's own description of the field says the salt should be "random, protected from access by 3rd
parties, and long enough to be unpredictable (e.g., 43 characters base64-encoded, corresponding to
256 bit)". Part 5 explains why each of those matters.

## Part 5. Why the salt is keyed, not a name

### 5.1 The tempting choice

The simplest salt is the group's name: `"hr:Restricted"`. It is stable, so HR's requests share notes.
It differs from `"contracts:Internal"`, so the groups are apart.

### 5.2 Why it fails

The gateway is not the only way to reach the model server. Anyone with network access to vLLM can
send a request directly, with any `cache_salt` they like. If the salt is `"hr:Restricted"`, a
contracts user who reaches vLLM directly sends that salt, joins HR's group, and the leak is back.

An unkeyed hash of the name, such as SHA-256 of `"hr:Restricted"`, fails the same way: anyone can
compute it.

### 5.3 A keyed fingerprint

The gateway computes the salt as an **HMAC**: a fingerprint of the tenant and classification under a
secret key that only the gateway holds. Without the key, nobody can compute any group's salt, even
knowing exactly how it is built.

```
salt = base64url( HMAC-SHA256( gateway key, tenant + separator + classification ) )
```

The result is 32 bytes, written as 43 characters, which matches vLLM's guidance in Part 4.3.

Tenant and classification are joined with a separator character. A tenant name could contain that
character, but a classification never does: it is always one of three fixed names. So the input can
always be split back at its last separator, and two different pairs of tenant and classification can
never produce the same input.

### 5.4 Failing closed

`prefix_cache_salt` refuses to run without a key or without a tenant. A missing tenant must not
quietly become a salt shared by every caller who has none.

## Part 6. How the gateway sends it

### 6.1 Only to backends that understand it

Each backend in the gateway is marked as accepting a cache salt or not. vLLM does. Ollama, the other
backend in this repository, does not take the field. `complete` adds `cache_salt` to the request
body only for a backend marked as accepting it.

### 6.2 Step by step

1. A request arrives at the gateway from a caller in the HR tenant, classified Restricted.
2. The router chooses vLLM (Guide 1, Part 5).
3. The gateway computes the salt for HR and Restricted under its key.
4. `complete` builds the request body and adds `cache_salt`, because vLLM accepts it.
5. vLLM mixes the salt into the first block's fingerprint and looks up the prefix.
6. Notes are found only if an earlier HR Restricted request made them.

### 6.3 The unit tests

| Test | What it proves |
|---|---|
| `test_prefix_salt_separates_tenants_and_classifications` | The salt is stable for one group and different for another tenant or classification |
| `test_prefix_salt_cannot_be_guessed_without_the_key` | 43 characters, contains neither name, and changes with the key |
| `test_prefix_salt_refuses_to_run_without_a_key_or_tenant` | No key or no tenant raises |
| `test_cache_salt_reaches_a_backend_that_takes_one` | vLLM receives the field |
| `test_cache_salt_is_withheld_from_a_backend_that_does_not` | Ollama does not |

## Part 7. Measured on a real GPU

The unit tests check what the gateway sends. They cannot check what vLLM does with it. Section 5 of
`hack/verify.py` does that against the live server.

### 7.1 Counting hits, not timing them

vLLM publishes a counter, `vllm:prefix_cache_hits`, of how many tokens were served from cached
notes. The check reads it before and after each request. The difference is that request's hits.

Counting is used instead of timing because, on this small model, timing barely moves. The model
reads 400 tokens in a few tens of milliseconds, so a cached prefix saves too little to see reliably
next to normal variation. The counter shows the reuse exactly.

### 7.2 What was measured

Each run starts the instruction with a fresh random tag, so nothing cached by an earlier run can hit.
The instruction is about 403 tokens.

| Request | Salt | Tokens served from cache |
|---|---|---|
| HR, first | HR's salt | 0 |
| HR, second | HR's salt | 336 |
| Contracts, same text | contracts' salt | **0** |
| Control: first, no salt | none | 0 |
| Control: second, no salt | none | **336** |

Read the table in two halves.

The first three rows show the fix working. HR's second request reused 336 tokens: 21 whole blocks
of 16. The contracts request with the same text reused none.

The last two rows are the control. Without salts, the second request reused 336 tokens of the first
one's notes. That proves the leak is real on this server. Without the control, "0 hits for
contracts" could have meant vLLM never shares notes at all, and the check would have passed for the
wrong reason.

### 7.3 Why 336 and not 403

Only whole blocks are cached and shared (Part 2.2). The instruction and the different question at
the end share 21 full blocks; the last partial block, and the question itself, are read fresh. The
403 is also an estimate, at four characters per token, so the true prompt length differs a little.

## Part 8. Design principles

**1. Every cache stops at the same boundary.** The gateway's own answer cache is divided by
classification (README Part 7.2). The model server's prefix cache is divided by tenant and
classification. A cache that ignores the boundary undoes it.

**2. Timing is output.** Anything a caller can measure is something the server tells them.

**3. A salt must be secret, not just different.** A guessable salt lets anyone who reaches the server
join a group.

**4. Prove the leak, then prove the fix.** The control shows the check can fail.

**5. Count instead of timing when you can.** A counter is exact. Timing is noisy and depends on the
model and the prompt.

## Part 9. What is unresolved

1. **Nothing sets the tenant yet.** The gateway has no sign-in step to read a caller's tenant from.
   Until it does, whoever calls `complete` must compute and pass the salt.
2. **Where the key lives.** The key needs a secret store and a rotation plan. Rotating it changes
   every salt, so every group's prefix cache starts empty once.
3. **Direct access to vLLM.** The salt stops a direct caller joining a group. It does not stop them
   using vLLM with no salt at all, where they share notes with every other unsalted caller. vLLM
   should only be reachable from the gateway.
4. **Within a group, timing still leaks.** HR's users can time each other. That is the trade the
   group boundary makes, and it is a decision about how fine the groups should be.
5. **Measured on v0.29.0 so far.** The live numbers above were taken on vLLM v0.29.0. The operator
   runs v0.31.0, whose source documents the same field. The live check is to be repeated there.

## Glossary

| Term | Plain meaning | Explained in |
|---|---|---|
| Block | A fixed run of tokens, 16 by default, that vLLM stores and fingerprints as one unit | Part 2.2 |
| `cache_salt` | The request field vLLM mixes into the first block's fingerprint | Part 4.1 |
| Control | A run that shows the effect exists, so the real check cannot pass for the wrong reason | Part 7.2 |
| Hash | A short fingerprint of some data that changes completely if the data changes | Part 2.2 |
| HMAC | A fingerprint computed with a secret key; nobody without the key can compute it | Part 5.3 |
| KV cache | The model's working notes on the tokens it has read, held in GPU memory | Part 1 |
| Prefill | The phase where the model reads the whole request at once | Part 1 |
| Prefix | The opening part of a request, shared with an earlier one | Part 2.1 |
| Prefix caching | Reusing the notes for a prefix that an earlier request already had read | Part 2.1 |
| Salt | An extra value mixed into a fingerprint so identical data gives different fingerprints | Part 4.1 |
| Tenant | The team or customer a caller belongs to | Part 4.2 |
| Time to first token (TTFT) | How long a caller waits for the first word of an answer | Part 3.1 |
| Timing side channel | Learning a secret from how long something takes rather than from what it returns | Part 3.2 |
| Token | A word or part of a word; models read and write in tokens | Part 1 |
| Trust group | Requests that share a salt, and so share cached notes and can time each other | Part 4.2 |
