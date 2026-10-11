# Guide 4. One request through the gateway, from zero

## How to read this guide

**Who it is for.** Engineers who will run, extend or review the model gateway, and anyone who has
to say whether one team's data can reach another. You should be able to read Python. You do not
need to know how caching or routing works. Each term is explained when it first appears.

**What you will know at the end.** The five steps one request takes through the gateway, and why
they run in that order. Why every request must name its tenant. How an answer cache that was
carefully divided by data classification still handed one team's answer to another, and how a test
of the whole path found it when the tests of each part could not.

**How it is organised.**

1. Part 1 explains the gateway's parts and why they need one entry point.
2. Part 2 explains tenants and why a request must name one.
3. Part 3 walks one request through the five steps.
4. Part 4 explains why the steps run in this order.
5. Part 5 explains the answer cache, and how it crossed teams.
6. Part 6 explains how the ledger reports cost per team.
7. Part 7 shows how the tests prove each step is needed.
8. Part 8 lists the design principles.
9. Part 9 lists what is unresolved.

A glossary at the end defines every term.

You can read it front to back like a short book. Each part builds on the one before it.

## The idea in one paragraph

The gateway sits between the people asking questions and the models answering them. It decides
which model may answer, remembers answers it has already given, labels each request so the model
server keeps teams apart, and records what every request cost. Each of those parts was built and
tested on its own. When they were finally joined into one path, one gap showed at once: the memory
of past answers was divided by how sensitive the data was, but not by team. Two teams asking the
same question at the same sensitivity shared answers, so one team could read another's. Every
request now has to say which team it is for, and the memory is divided by team as well.

## A note on accuracy

The code is in `gateway/app/gateway.py`, with the parts in `router.py`, `cache.py` and `client.py`.
The tests are in `gateway/tests/test_gateway.py`. The decision record is ADR-0010, building on
ADR-0005, ADR-0006 and ADR-0008. Every behaviour described has a test.

Simplified for teaching: the gateway here is a Python object, not a network service. Part 9 says
what a real front end must add.

## The running example

Fernhill Legal is a law firm in Leeds. Two of its teams use the gateway.

* **HR** sends payroll questions. Some are Restricted, some Internal.
* **Contracts** sends questions about clauses and templates. They are Internal.

The gateway has two backends: vLLM on a GPU, approved for Restricted data, and Ollama on a CPU,
approved up to Internal. On Monday morning, HR asks "summarise the payroll file", marked Internal.
A few minutes later, someone in contracts sends exactly the same words, also Internal.

The names and timings are invented. The behaviour is what the tests check.

---

## Part 1. The parts, and why they need one entry point

### 1.1 The four parts

| Part | Plain meaning | Guide that explains it |
|---|---|---|
| Router | Chooses which backend may answer, or refuses | Guide 1, Part 5 |
| Answer cache | Remembers past answers and serves one again for a near-identical question | README Part 7.2 |
| Prefix cache salt | A secret label per team that keeps the model server's memory apart | Guide 2 |
| Ledger | Records every call: tokens, time and cost | Guide 1, Part 6 |

### 1.2 Tested apart, never together

Each part had its own tests, and each passed. But a real request needs all four, in a particular
order, and each needs something from the request. The router needs the classification. The salt
needs the team. The ledger needs to know whom to charge.

When nothing joins the parts, whoever wires them up decides the order and what to pass. Getting it
wrong breaks no individual test. So the order itself was untested, and so was anything that only
shows up when the parts meet.

### 1.3 The entry point

`Gateway.handle(request, tenant)` is now the one way a request goes through the gateway. It runs
the parts in a fixed order and passes each what it needs.

## Part 2. Tenants

### 2.1 What a tenant is

A **tenant** is the team or customer a request belongs to. At Fernhill, HR and contracts are two
tenants. In a platform serving several companies, each company is a tenant.

### 2.2 Why every request must name one

Three things in the gateway depend on the tenant.

1. The prefix cache salt is computed from it (Guide 2, Part 5).
2. The answer cache keeps each tenant's answers apart (Part 5).
3. The ledger charges the call to it (Part 6).

A request with no tenant would get a salt shared by everyone without one, would share cached
answers with them, and would be charged to nobody. So `handle` refuses it.

### 2.3 Refused first

The tenant check is the first thing `handle` does, before routing.

**Example.** A request with no tenant also asks for a capability no backend has. If routing ran
first, the caller would be told "no eligible backend", fix that, and only then learn about the
tenant. Checked first, the missing tenant is always the error, whatever else is wrong.

## Part 3. One request, step by step

HR's question goes through these steps. Each names who acts.

1. **The caller** sends "summarise the payroll file", Internal, for tenant `hr`.
2. **The gateway** checks there is a tenant. There is.
3. **The router** removes backends not approved for Internal data, without the needed capability,
   or over the latency budget, then ranks the rest by cost per token. Ollama is free, so it is
   chosen (Guide 1, Part 5).
4. **The gateway** builds the cache scope: classification Internal, model `phi4-mini`, the
   sampling settings, and tenant `hr`.
5. **The answer cache** is searched in that scope. Nothing is there.
6. **The gateway** computes HR's salt for Internal data under its secret key.
7. **The client** calls Ollama. Ollama does not accept a salt, so the salt is not sent to it; vLLM
   would receive it.
8. **Ollama** answers.
9. **The answer cache** stores the answer in HR's scope.
10. **The ledger** records the call: Ollama, tokens, time, cost zero, tenant `hr`.

If HR asks the same question again, step 5 finds the stored answer. Steps 6 to 9 are skipped, and
step 10 records a cached call at no cost.

## Part 4. Why this order

### 4.1 Route before the cache

The cache is searched only after the router has chosen a backend. That way a cached answer can
never skip the classification check.

**Example.** Suppose the cache were searched first, by prompt alone. A Public caller sends the same
words as an earlier Restricted request, and the cache returns the Restricted answer before the
router is ever asked. README Part 7.2 describes exactly this failure in an earlier version. Routing
first means every answer, cached or not, comes from a backend the router allowed for this request.

### 4.2 The model is part of the scope

The router's choice decides which model answers, and the cache scope includes the model. An answer
from one model is not an answer from another, so the cache cannot be consulted until the model is
known. That is a second reason the router comes first.

### 4.3 Record last, always

The ledger records every call that reaches a backend or the cache, including cached ones. A refused
request, refused for a missing tenant or by the router, records nothing, because nothing was used.

## Part 5. The answer cache that crossed teams

### 5.1 What the answer cache is

The **answer cache**, also called a semantic cache, stores past answers. When a new question means
nearly the same as an old one, it returns the stored answer instead of asking a model. README Part
7.2 explains how it compares questions.

Each stored answer sits in a **scope**: the conditions under which it may be served. A lookup only
searches its own scope.

### 5.2 The scope, before

The scope held the classification, the model, the sampling settings and a fingerprint of the system
prompt. It was designed to stop a Restricted answer reaching a Public caller, and it did.

It did not include the tenant.

### 5.3 What that meant

**Example.** At 9:00, HR asks "summarise the payroll file", Internal. The model answers with a
summary of salaries by department, and the answer is cached in the scope (Internal, `phi4-mini`,
settings). At 9:05, a contracts user sends the same words, also Internal. The scope is identical:
same classification, same model, same settings. The cache returns HR's payroll summary.

No model was asked and no rule was broken. The router allowed Internal data to Ollama, which was
correct. The contracts user simply received another team's answer.

Notice how this differs from the timing leak in Guide 2. There, a team could confirm a guess from
how fast an answer came. Here, the team received the answer itself.

### 5.4 How it was found

None of the cache's tests could find it, because the cache did not know about tenants. The router's
tests could not either. It showed up in the first test of the joined path,
`test_another_tenants_cached_answer_is_never_served`, which sends the same question from two
tenants. With the old scope, the second request was served from the first one's cache, and the test
failed.

### 5.5 The fix

The scope now includes the tenant. Answers are shared within a team and never across teams.

The cost is a lower hit rate: two teams asking the same question each pay for one model call. That
is the same trade README Part 7.2 made for classifications.

## Part 6. Cost per team

The ledger used to report cost per backend. That answers "what did the GPU cost" but not "what did
HR spend".

Each record now carries the tenant, and `Ledger.by_tenant()` reports, per tenant: calls, how many
were served from the cache, tokens, and cost.

**Example.** HR sends one question. Contracts sends one question, then the same one again.

| Tenant | Calls | From cache | Cost |
|---|---|---|---|
| hr | 1 | 0 | one call's share of the GPU hour |
| contracts | 2 | 1 | one call's share; the cached one is free |

A cached answer costs nothing, but it is still recorded. A team whose calls are mostly cached is
getting a lot of value from the cache, and that is worth knowing.

## Part 7. Proving each step is needed

Each guard in `handle` was switched off in turn, and the suite run.

| Guard switched off | Test that fails |
|---|---|
| Tenant in the cache scope | `test_another_tenants_cached_answer_is_never_served` |
| Sending the tenant's salt | `test_the_tenants_salt_reaches_the_model_server` |
| Recording the tenant on the call | `test_cost_is_attributed_to_the_tenant` |
| The tenant check at the start | `test_a_request_without_a_tenant_is_refused_before_any_call` |

The last row needed care. With the tenant check removed, a request without a tenant still failed,
because computing the salt also refuses an empty tenant. But it failed later, after routing and the
cache lookup. The test now also sends a tenantless request that the router would refuse, and
requires the error to be about the tenant. Only the check at the start passes that.

## Part 8. Design principles

**1. One entry point, one order.** Route, look up, call, record. The order is code, not convention.

**2. No tenant, no request.** Every part that keeps teams apart depends on it.

**3. Route before the cache.** A cached answer must come from a backend the router allowed.

**4. Every cache stops at every boundary.** Classification and tenant, for the answer cache and the
model server's prefix cache alike.

**5. Test the joined path.** The cross-team leak was invisible to every test of a single part.

## Part 9. What is unresolved

1. **The tenant must come from a verified identity.** `handle` takes a tenant as an argument. A
   network front end must read it from a signed-in identity. Taking it from a header the caller
   writes would let any caller claim to be HR.
2. **No network front end.** The gateway is a Python object. Serving it over HTTP, with sign-in,
   limits per tenant and streaming to the caller, is not built.
3. **Teams that should share.** Some answers, such as public opening hours, could safely be shared
   across teams. Today nothing is. Allowing it would need a rule saying which classifications may
   be shared, and the saving is small.
4. **Within a team, one user can read another's cached answer.** The tenant is the boundary. If two
   people in HR should not see each other's answers, the tenant is too coarse.

## Glossary

| Term | Plain meaning | Explained in |
|---|---|---|
| Answer cache | A store of past answers, served again for a near-identical question | Part 5.1 |
| Backend | A server that can answer a request, such as vLLM or Ollama | Running example |
| Classification | How sensitive a request's data is: Public, Internal or Restricted | Running example |
| Entry point | The one function every request goes through: `Gateway.handle` | Part 1.3 |
| Hit rate | The share of questions answered from the cache | Part 5.5 |
| Ledger | The gateway's record of every call: tokens, time, cost and tenant | Part 6 |
| Prefix cache salt | A secret label per tenant and classification that keeps the model server's memory apart | Part 1.1 |
| Router | The part that chooses which backend may answer, or refuses | Part 1.1 |
| Scope | The conditions under which a cached answer may be served | Part 5.1 |
| Semantic cache | Another name for the answer cache | Part 5.1 |
| Tenant | The team or customer a request belongs to | Part 2.1 |
