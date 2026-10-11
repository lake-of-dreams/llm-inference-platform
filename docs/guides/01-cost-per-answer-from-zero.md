# Guide 1. What an answer costs, from zero

## How to read this guide

**Who it is for.** Engineers and managers who have to say what running a language model costs, and
who need a gateway to pick the cheaper of two places to run it. You do not need to know how a GPU
works or how a model produces text. Each term is explained when it first appears.

**What you will know at the end.** Why the price of a GPU per hour does not tell you what an
answer costs. How to turn an hourly price into a price per answer, and what measurement that needs.
How this repository charges each call its fair share of a GPU that many calls use at once, and how
its router uses cost per token to choose between backends.

**How it is organised.**

1. Part 1 explains how a model produces text, and the unit everything is measured in: the token.
2. Part 2 explains why a GPU is paid for by the hour, and why that is the wrong unit for comparing.
3. Part 3 builds cost per million tokens from an hourly price and throughput.
4. Part 4 explains which throughput to use, and why the obvious one is wrong.
5. Part 5 explains how the router uses cost, and where cost sits in its order of decisions.
6. Part 6 explains how each call is charged its share of the hour.
7. Part 7 shows the numbers measured on a real 4 GB GPU.
8. Part 8 lists the design principles.
9. Part 9 lists what is unresolved.

A glossary at the end defines every term.

You can read it front to back like a short book. Each part builds on the one before it.

## The idea in one paragraph

Renting a GPU is like renting a delivery van by the hour. The hourly price says what the van costs
to keep, not what one delivery costs. That depends on how many deliveries the van makes in the
hour. A cheap van that makes few deliveries can cost more per delivery than a dearer one that makes
many. A language model's "deliveries" are pieces of text called tokens. So to compare two places to
run a model, divide each hourly price by the tokens it produces in an hour when it is busy. This
repository measures that, ranks backends by it, and charges each request its share of the hour.

## A note on accuracy

The code is in `gateway/app/router.py` and `gateway/app/client.py`. The tests are in
`gateway/tests/test_router.py` and `gateway/tests/test_client.py`. The decision record is ADR-0005.

The prices in the running example are illustrative, chosen to make the arithmetic easy. The
throughput in Part 7 was measured on one laptop GPU, an NVIDIA RTX 500 Ada with 4 GB of memory,
running a 0.5-billion-parameter model. It says how this method works, not what any production card
achieves.

## The running example

Fernhill Legal is a law firm in Leeds with 140 staff. It runs its own models rather than send client
documents to an outside provider. It has one office server with a 4 GB GPU, booked internally at
520 thousandths of a dollar an hour ($0.52), and a spare CPU machine that costs nothing extra.

Fernhill is considering renting a second GPU for contract work, and has two offers. Through this
guide, those offers show why the hourly price misleads.

---

## Part 1. Tokens: the unit of work

A language model reads and writes text in pieces called **tokens**. A token is a word or part of a
word: "contract" might be one token, "subcontractor" two or three. As a rough guide, 100 tokens is
about 75 English words.

A model's work on a request has two phases. First it reads the whole request at once; this is
called **prefill**. Then it writes the answer one token at a time; this is called **decode**. The
README's Part 1.1 explains why the two phases stress a GPU differently. For cost, what matters is
that the answer is produced token by token, and the number of tokens written is a fair measure of
the work done.

**Example.** Fernhill's HR manager asks for a payroll summary. The request is 400 tokens and the
answer is 150 tokens. The model reads 400 tokens in one go, then writes 150, one at a time.

## Part 2. Why a GPU is paid for by the hour

### 2.1 Two ways to buy a model

There are two common ways to pay for running a model.

| Way of paying | Plain meaning | Example |
|---|---|---|
| Per token | A provider runs the model and charges for each token read and written | A hosted model priced per million tokens |
| Per hour | You run the model on a GPU you own or rent, and pay for the GPU's time | Fernhill's server at $0.52 an hour |

Fernhill runs its own models, so it pays per hour. The GPU costs the same whether it writes one
token in that hour or a million.

### 2.2 Why the hourly price misleads

Two GPUs at the same hourly price can produce very different numbers of tokens in that hour. A newer
card can produce several times as many. So the hourly price alone cannot say which is cheaper for
the work.

**Example.** Fernhill's two offers for a second GPU are:

* an older card at 520 thousandths of a dollar an hour;
* a newer card at 800 thousandths of a dollar an hour.

By the hour, the older card is cheaper. Part 3 shows it is the more expensive way to get answers.

## Part 3. Cost per million tokens

### 3.1 The formula

To compare like with like, convert each hourly price into a price per unit of work.

```
cost per million tokens = hourly price / (tokens per second × 3,600) × 1,000,000
```

Read it in three steps.

1. Tokens per second × 3,600 is the tokens produced in one hour.
2. The hourly price divided by that is the price of one token.
3. Multiplying by a million gives the price per million tokens, which is easier to read.

### 3.2 Fernhill's two offers

The older card produces 100 tokens a second when busy. The newer one produces 400.

| Offer | Price per hour | Busy throughput | Tokens per hour | Cost per million tokens |
|---|---|---|---|---|
| Older card | 520 thousandths of a dollar | 100 tokens a second | 360,000 | 520 / 360,000 × 1,000,000 = 1,444 |
| Newer card | 800 thousandths of a dollar | 400 tokens a second | 1,440,000 | 800 / 1,440,000 × 1,000,000 = 556 |

Notice what drives the result. The newer card costs 1.5 times as much per hour but produces 4 times
as many tokens. Per answer, it costs 556 / 1,444 = 0.39 of the older card: about two-fifths. Ranked
by the hour, the older card wins. Ranked per token, the newer one does.

### 3.3 A free backend

Fernhill's spare CPU machine costs nothing extra. Its cost per token is zero, however slow it is.
The router treats it as the cheapest backend whenever it is eligible, which Part 5 explains.

## Part 4. Which throughput to use

### 4.1 Throughput

**Throughput** is how many tokens a backend produces per second. It depends heavily on how many
requests the backend is handling at once. A GPU writing one answer at a time wastes most of its
capacity. Writing sixteen at once, it produces many more tokens in the same second, because the
same memory reads serve several answers.

### 4.2 The obvious figure is wrong

The obvious place to get throughput is the monitoring system. vLLM publishes a running count of the
tokens it has generated, and Prometheus, the monitoring system, can turn that into a rate per
second.

That rate measures how much work the backend is doing, not how much it can do. When the backend is
idle, the rate falls towards zero, so the cost per token computed from it rises towards infinity.

**Example.** At 7:00 the newer card is idle and its observed rate is 2 tokens a second. Its cost
per million tokens computed from that would be 800 / (2 × 3,600) × 1,000,000 = 111,111. At exactly
the moment it has the most spare room, it would look 200 times more expensive than it is, and the
router would send work elsewhere.

### 4.3 The right figure

The figure that matters is the throughput one replica sustains when it is busy. It is measured once
with a **load test**: send many requests at once and count the tokens that come back per second.
vLLM ships a tool for this, `vllm bench serve`, and `hack/verify.py` runs a small one (Part 7).

The measured figure is set on the backend as `tokens_per_second`. It changes only when the model,
the card or the serving software changes, and then it should be measured again.

### 4.4 When no figure is known

A backend with no throughput figure cannot be priced per token. The router ranks it after every
backend that can be priced. Among backends without a figure, it falls back to the hourly price, as
the first version of the router did. The routing reason says which basis it used:

```
cheapest of 2 eligible (cost=556m$/Mtok, p95 TTFT=500ms)
cheapest of 2 eligible (cost=400m$/h, throughput unknown)
```

## Part 5. Where cost sits in the router's decisions

Cost decides last. Before cost is considered, the router removes every backend that must not or
cannot answer. README Part 7.1 gives the full order. In short, step by step:

1. Remove backends not approved for the request's data classification.
2. Remove backends without a capability the request needs, or without room for its text.
3. Remove backends whose recent first-token latency is over the request's budget.
4. Rank what is left by cost per token, then by hourly price for unpriced backends.
5. Choose the first. If nothing is left, refuse rather than downgrade.

Notice the consequence. A cheap backend that is not approved for Restricted data is never chosen
for a payroll request, however cheap. Cost ranks backends that are already allowed. It never
removes one, and it never adds one back.

**Example.** HR's payroll request is Restricted. The CPU machine is approved only up to Internal,
so step 1 removes it, even though it costs nothing. Of the two GPUs, both are approved and both are
fast enough. Step 4 ranks the newer card first at 556 per million tokens, and it answers.

## Part 6. Charging each call its share of the hour

Choosing a backend is one question. Recording what a call cost, after the fact, is another. The
gateway keeps a **ledger**: a record of every call, with its tokens, latency and cost.

### 6.1 Charging by time held

A self-hosted GPU is paid by the hour. The ledger charges each call the backend's hourly rate for
the time the call held it.

```
cost of a call = hourly rate × (seconds the call took / 3,600)
```

### 6.2 Dividing by how many calls shared the GPU

If sixteen calls run on the GPU at once, charging each one the full rate for its time bills the
same hour sixteen times. So the ledger divides each call's charge by how many calls were running at
the same time.

The gateway counts calls in progress on each backend. It notes the count when a call starts and
when it ends, and uses the average of the two as the call's **concurrency**.

**Example.** A payroll summary takes 3 seconds on Fernhill's GPU at 520 thousandths of a dollar an
hour. Alone, it costs 520 × 3 / 3,600 = 0.433 thousandths of a dollar. Three other calls were
running when it started and when it ended, so the concurrency is 4, and the call is charged
0.433 / 4 = 0.108.

The average of start and end counts is exact when no other call starts or ends part-way through.
Otherwise it is an approximation, which is good enough for showing teams what they used.

### 6.3 Why both are needed

The router needs cost per token to choose, before a call runs. The ledger needs the shared hourly
charge to record what a call cost, after it ran. They answer different questions and use different
data.

## Part 7. Measured on a real GPU

`hack/verify.py` section 6 runs a small load test against the live vLLM server: 8 requests at once,
each asking for 64 tokens. It counts the tokens returned, divides by the time taken, and uses the
result as the backend's busy throughput.

The setup: an NVIDIA RTX 500 Ada laptop GPU with 4 GB of memory, vLLM serving
Qwen2.5-0.5B-Instruct with `hack/serve-vllm.sh`. That script limits the server to 16 requests at a
time and turns off some speed optimisations so the model fits in 4 GB.

### 7.1 The first measurement was wrong by almost five times

The first version of the check sent one burst to a server that had only just started.

| Run | Tokens | Seconds | Tokens per second | Cost per million tokens at 520 m$/h |
|---|---|---|---|---|
| One burst, server just started | 512 | 5.6 | 92 | 1,567 |
| One burst, same server, a minute later | 512 | 1.2 | 434 | 333 |

Same server, same requests, a factor of 4.7 apart. The first concurrent batch on a fresh server pays
one-off start-up work, such as loading GPU code it has not used yet. Had the first figure been typed
into the backend, Fernhill's GPU would have looked nearly five times as expensive per answer as it
is, and the router would have preferred a worse backend.

Notice that this is the same mistake as Part 4.2 in a different form. Both measure the backend in a
state it is not normally in: idle, or cold.

### 7.2 Warm up, then measure several times

The check now sends one warm-up burst that is not counted, then three measured bursts, and takes the
middle value, the **median**. It also checks that the three agree within a factor of two, so a
noisy measurement fails loudly instead of being used.

| Burst | Tokens per second |
|---|---|
| Warm-up | not counted |
| 1 | 467 |
| 2 | 421 |
| 3 | 405 |
| Median | **421** |

**Example.** At 421 tokens a second, Fernhill's GPU produces 421 × 3,600 = 1,515,600 tokens an
hour. At 520 thousandths of a dollar an hour, that is 520 / 1,515,600 × 1,000,000 = 343 thousandths
of a dollar per million tokens. A payroll summary of 150 tokens costs 343 × 150 / 1,000,000 = 0.05
thousandths of a dollar.

The router, given this figure, reports `cost=343m$/Mtok` as its reason. The ledger, for the same
calls, recorded up to 7.5 calls sharing the GPU at once, and charged each its share of the hour.

### 7.3 What these numbers do not say

They describe one small model on one laptop GPU, eight requests at a time. A larger model, a larger
card, or more requests at once would give very different figures. The method carries over. The
numbers do not.

## Part 8. Design principles

**1. Compare the cost of an answer, not the cost of an hour.** Two backends at the same hourly
price can differ several-fold per answer.

**2. Use capacity, not current load.** Busy throughput from a load test, never the rate observed
at a quiet moment.

**3. Unknown cost ranks last, and says so.** A backend that cannot be priced per token is ranked
after those that can, and the routing reason names the basis.

**4. Cost decides last.** Classification, capability and latency come first. Cost never removes a
backend and never adds one back.

**5. Charge the hour once.** Calls that share a GPU share its cost.

## Part 9. What is unresolved

1. **The throughput figure is typed in.** It goes stale when the model, card or vLLM version
   changes, and nothing warns when it does. Comparing it with the throughput observed under heavy
   load would catch drift.
2. **Throughput depends on the request mix.** Long answers and short ones, long prompts and short
   ones, give different figures. One number per backend is a simplification.
3. **Prefill is not priced.** Cost per generated token ignores the work of reading a long prompt. A
   backend serving long documents with short answers is cheaper per token than it looks.
4. **Idle time is not charged to anyone.** A GPU that serves nobody for an hour still costs an
   hour. The ledger records what calls used, not what the GPU cost. The gap is the price of spare
   capacity, and who pays for it is a budgeting decision.

## Glossary

| Term | Plain meaning | Explained in |
|---|---|---|
| Backend | A server that can answer a request, such as vLLM on a GPU or Ollama on a CPU | Part 3.3 |
| Busy throughput | Tokens a replica produces per second when handling many requests at once | Part 4.3 |
| Concurrency | How many calls were running on a backend at the same time as this one | Part 6.2 |
| Cost per million tokens | Hourly price divided by the tokens produced in an hour, times a million | Part 3.1 |
| Decode | The phase where the model writes its answer one token at a time | Part 1 |
| GPU | The graphics processor that does a model's arithmetic | Running example |
| Ledger | The gateway's record of every call: tokens, latency and cost | Part 6 |
| Load test | Sending many requests at once to measure what a backend can sustain | Part 4.3 |
| Prefill | The phase where the model reads the whole request at once | Part 1 |
| Prometheus | The monitoring system that collects the metrics vLLM publishes | Part 4.2 |
| Router | The part of the gateway that chooses a backend for each request | Part 5 |
| Throughput | Tokens a backend produces per second | Part 4.1 |
| Token | A word or piece of a word; models read and write in tokens | Part 1 |
| vLLM | A model server built for GPUs | Part 4.2 |
