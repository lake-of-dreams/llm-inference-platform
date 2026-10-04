# LLM Inference Platform

## How to read this guide

**Who it is for.** This guide is for engineers who run software on Kubernetes and now need to
run a language model there too. You should know roughly what a container is. You do not need to
know anything about how language models are served, and every term from that world is explained
the first time it appears.

**What you will know at the end.** You will understand why a language model behaves differently
from a web service under load, and what this repository builds to cope with that. For each
decision the system makes, you will know which part makes it. By the last part you can run the
tests and deploy it yourself.

**How it is organised.**

1. Part 1 explains what makes serving a language model different.
2. Part 2 introduces the pieces this repository builds and who owns each one.
3. Part 3 follows one request from start to finish.
4. Part 4 covers starting a model without Kubernetes killing it.
5. Part 5 covers scaling on the signal that moves first.
6. Part 6 covers who owns the replica count, and why that needs care.
7. Part 7 covers choosing a backend for each request, caching answers, and attributing cost.
8. Part 8 shows how to run and test it.
9. Part 9 states the design principles and lists the decision records.
10. Part 10 lists what is unresolved.

A glossary at the end defines every term and points to the part that explains it.

You can read it front to back like a short book. Each part builds on the one before it.

## The whole idea

**In one paragraph.** A company wants to run its own AI model instead of sending its data to an
outside provider. Running one is harder than running an ordinary website, for three reasons. The
model takes minutes to start. The usual measure of how busy a server is gives the wrong answer.
Some of the company's data must stay on approved machines. This repository writes those three
facts into the software. You describe the model you want in a short file. A program then builds
and maintains everything needed to run it, adds copies when requests start to queue, and sends
each request to a machine that is allowed to see it.

## A note on accuracy

The version numbers and behaviour described here were checked in October 2026 against the source
code of vLLM v0.30.0, Ollama v0.35.1, KEDA v2.21.0, the Gateway API Inference Extension v1.6.2,
the NVIDIA DRA driver v25.12.0 and Kubernetes 1.37. Where this guide says something was tested,
Part 8 names the test.

The guide simplifies how a model computes an answer. It describes the two phases at the level
needed to understand the scaling decisions, and no deeper.

Some of this has not been run end to end. The operator's logic is tested against a real
Kubernetes API server, the component that stores every object and checks it on the way in. It has
not yet run on a cluster with a GPU, the graphics processor that does a model's arithmetic. The
container image has not been built in CI, the automatic checks that run on every push. `hack/verify.py` checks routing, caching and cost against live vLLM and Ollama, and it has
not been rerun since the changes of October 2026. Part 10 lists these gaps.

This is not a production platform. It is a small, working reference that runs on one 4 GB GPU.

## The running example

Fernhill Legal is a law firm in Leeds with 140 staff. It has decided to run its own models rather
than send client documents to an outside provider. It has three kinds of work.

* **Payroll summaries.** HR asks for summaries of salary files. This data is **Restricted**.
* **Contract clauses.** Lawyers ask for clause summaries and structured extracts of contracts.
  This data is **Internal**.
* **Website questions.** Visitors to the public website ask about opening hours and services. This
  data is **Public**.

Fernhill has one office server with a 4 GB GPU. Renting equivalent capacity would cost $0.52 an
hour, so the firm books it at that rate. It also has a spare CPU machine that costs nothing extra
to use. On a normal morning 40 requests arrive in the first minute after 9:00.

The numbers are illustrative. They are chosen to make the arithmetic in each example easy to
follow.

## Part 1. Why serving a language model is different

A language model behaves differently from a web service in three ways, and each one breaks a
default that Kubernetes users rely on.

### 1.1 Prefill and decode: why a busy GPU can look idle

Imagine Fernhill's HR manager asks for a payroll summary. The model first reads the whole request
at once. This is called **prefill**. During prefill the GPU does a large amount of arithmetic in
parallel, and its utilisation, the share of time its arithmetic units are busy, goes up.

The model then writes the answer one **token** at a time. A token is a word or a piece of a word.
This is called **decode**. For each token, the GPU reads the model's saved working state from
memory. That state is called the **KV cache**: the model's notes on everything it has read and
written so far. Moving those bytes takes longer than the arithmetic done on them. Decode is
therefore limited by memory bandwidth, not by arithmetic.

Notice what this means for monitoring. During decode, the GPU's utilisation figure is modest
even when the server is overloaded. Chat traffic spends most of its time in decode. Part 5 shows
how this leads a standard autoscaler to remove capacity at the worst moment.

### 1.2 Weights: why a model takes minutes to start

A model is a very large set of numbers called **weights**. A small model has hundreds of millions
of them, and a large one has hundreds of billions. Before the server can answer anything, it must
download the weights and load them into GPU memory.

That takes from tens of seconds to several minutes. During that time the server cannot answer
health checks. Part 4 explains why Kubernetes kills such a container unless it is told to wait.

### 1.3 Why a GPU cannot be counted like a CPU

Kubernetes traditionally asks for GPUs by count: "give this container one GPU". A count says
nothing about the card. A card with 4 GB of memory and a card with 80 GB both count as one. A model
that fits on the second runs out of memory on the first.

**Dynamic Resource Allocation (DRA)** is the Kubernetes feature that replaces the count with a
description. A driver on each node publishes what its devices are, including how much memory each
has. A pod asks for "one GPU with at least 3 GiB of memory". The scheduler, the Kubernetes component
that decides which node each pod runs on, then finds a device that matches. Part 2 shows the object that carries this request.

## Part 2. The pieces and who owns what

You write one short file per model. A program called an operator turns it into five Kubernetes
objects and keeps them correct.

### 2.1 The InferenceService: one file describes one model

Kubernetes lets a project add its own kinds of object. The definition of a new kind is called a
**CustomResourceDefinition (CRD)**. This repository defines one: the **InferenceService**.

Here is Fernhill's payroll model, from `config/samples/qwen-small.yaml`.

```yaml
apiVersion: serving.platform.io/v1alpha1
kind: InferenceService
metadata:
  name: qwen-small
  namespace: llm-platform
spec:
  model: Qwen/Qwen2.5-0.5B-Instruct
  backend: vLLM
  classification: Restricted
  deviceClass: gpu.nvidia.com
  minDeviceMemory: 3Gi
  maxModelLen: 2048
  gpuMemoryUtilization: 85
  weightLoadSeconds: 300
  costPerHourMilliUSD: 520
  autoscaling:
    minReplicas: 1
    maxReplicas: 3
    queueDepthTarget: 4
    ttftP95Milliseconds: 2000
    kvCacheUsagePercent: 80
```

The `backend` names the program that serves the model. **vLLM** is a model server built for GPUs.
**Ollama** is a simpler model server that runs well on a CPU. `costPerHourMilliUSD` is in
thousandths of a dollar, so 520 means $0.52 an hour.

The API server checks each InferenceService before it is stored. The checks are written in the
**Common Expression Language (CEL)**, a small rule language the API server runs on every create
and update. They reject these combinations:

| Rule | Plain meaning | Fernhill example |
|---|---|---|
| Restricted needs a `deviceClass` | Restricted data must run on a named, approved kind of device | The payroll model must name `gpu.nvidia.com` |
| Ollama cannot take Restricted data | The CPU server is not approved for Restricted data | Payroll can never be served by Ollama |
| vLLM needs a `deviceClass` | The vLLM image is built for GPUs and cannot serve on a CPU | A vLLM model with no GPU is refused at once, not after a failed start |
| `minDeviceMemory` needs a `deviceClass` | A memory floor only makes sense for a device | The payroll model's 3 GiB floor is valid because it names `gpu.nvidia.com` |
| `maxReplicas` ≥ `minReplicas`, and `minReplicas` ≥ 1 | The limits must make sense, and Part 5 explains why zero is not allowed | The payroll model runs between 1 and 3 replicas |

Because the API server enforces these rules, they hold even while the operator is not running.

### 2.2 What the operator builds

An **operator** is a program that runs inside the cluster and keeps real objects in line with a
description. Each pass it makes is called a **reconcile**. For every InferenceService it builds
the following.

| Object | Plain meaning | Fernhill's payroll model |
|---|---|---|
| Deployment | Runs and replaces the copies of the model server. Each copy is a **replica**, and each replica runs in a **pod**, a group of containers scheduled together | One to three pods running vLLM v0.30.0 |
| Service | A stable address that spreads requests across the ready pods | `http://qwen-small.llm-platform.svc.cluster.local:8000/v1` |
| ResourceClaimTemplate | The DRA request each pod makes for its device | One `gpu.nvidia.com` device with at least 3 GiB of memory |
| ScaledObject | Instructions to KEDA, the autoscaler, on what to measure and when to add replicas | Scale on waiting requests, cache fill and first-token latency |
| InferencePool | Registers the pods with the Gateway API Inference Extension, so a model-aware gateway can route to them | A pool of the `qwen-small` pods on port 8000 |

**KEDA** is the Kubernetes Event-driven Autoscaler. It reads a metric, such as a count from
Prometheus, and tells the standard Kubernetes autoscaler how many replicas to run. **Prometheus**
is the monitoring system that collects the metrics vLLM publishes.

KEDA and the Inference Extension are optional. If either is not installed, the operator still
builds the rest, and the InferenceService's status says what is missing. ADR-0007 records why.

### 2.3 Who does what, and why

Several programs make decisions in this system. Some of the decisions follow fixed rules and
others respond to live measurements. The table puts each decision where it belongs.

| Task | Who does it | Why |
|---|---|---|
| Reject a disallowed spec | The API server, using the CRD's CEL rules | It runs on every write, so a bad spec is refused before any pod exists, even with the operator down |
| Build and correct the five objects | The operator | One owner per object keeps the objects consistent with the spec |
| Set the replica count after creation | KEDA, through the Kubernetes autoscaler | Only it sees live demand; Part 6 explains how the operator keeps out of its way |
| Choose a device for each pod | The Kubernetes scheduler, with DRA | Only the scheduler sees every node's free devices |
| Decide when a pod is ready | The kubelet, the agent on each node, using the probes | It is on the node and checks every few seconds |
| Choose a backend for each request | The gateway's router, in `gateway/app/router.py` | Classification is a property of the request, so it must be decided per request |
| Serve a cached answer | The gateway's semantic cache | It must sit behind the same classification boundary as the router (Part 7) |

### 2.4 How the pieces connect

```
 InferenceService (your YAML)
        │
        ▼
    operator ──► ResourceClaimTemplate ──► one GPU for each pod
        │
        ├──► Deployment ──► pods running vLLM ──► metrics ──► Prometheus
        │        ▲                                                 │
        │        └───────── replica count ◄── KEDA ◄───────────────┘
        │                                      ▲
        ├──► ScaledObject ─────────────────────┘  (tells KEDA what to measure)
        ├──► Service        (one stable address for the pods)
        └──► InferencePool  (registers the pods with a model-aware gateway)

 caller ──► model gateway ──► vLLM or Ollama
            (classification → capability → latency → cost)
```

*Figure 1. The operator creates every object it points to. Look at the loop on the right: the
replica count reaches the Deployment from KEDA, never from the operator. The gateway on the last
two lines is a separate program that chooses a backend for each request.*

## Part 3. One request, end to end

This part follows a single payroll request through the whole system. Every later part explains
one or more of these steps in depth.

**How one payroll request is served, step by step**

1. An administrator applies the `qwen-small` InferenceService, and the API server checks it
   against the CEL rules.
2. The operator creates the ResourceClaimTemplate, the Deployment with one replica, the Service,
   the ScaledObject and the InferencePool.
3. The scheduler finds a GPU with at least 3 GiB of memory and places the pod on that node.
4. vLLM downloads the weights and loads them while the startup probe waits (Part 4).
5. The startup probe passes, and the kubelet marks the pod ready.
6. The operator sees a ready replica and sets the InferenceService's phase to `Ready`.
7. Fernhill's HR system sends "Summarise the March payroll file" to the gateway, marked
   Restricted.
8. The gateway's cache looks for a near-identical earlier request in the Restricted scope for
   this model, and finds none (Part 7).
9. The router rejects Ollama because it is not approved for Restricted data, and chooses vLLM.
10. The gateway streams the answer back and records when the first token arrived.
11. The ledger, the gateway's record of every call, stores the call's tokens, latency and share of the GPU's hourly cost.
12. vLLM reports a waiting request to Prometheus, and KEDA adds a replica if the queue keeps
    growing (Part 5).

## Part 4. Starting a model without killing it

A model container is silent for minutes while it loads, and Kubernetes reads silence as failure.
The fix is a probe whose only job is to wait.

### 4.1 The three probes and their separate jobs

Kubernetes checks a container with **probes**, small requests the kubelet sends on a timer. A
**liveness probe** restarts a container that stops answering. A **readiness probe** removes a pod
from the Service while it is not answering, without restarting it. A **startup probe** runs first,
and the other two do not start until it has passed.

Without a startup probe, the liveness probe fires while the weights are still loading. The
container is killed, restarts, starts loading again, and is killed again. Kubernetes reports
CrashLoopBackOff, and the application log is clean, because the application never failed. That
combination sends people looking for a bug that does not exist.

**Example.** Fernhill sets `weightLoadSeconds: 300`. The startup probe checks every 10 seconds
and may fail 300 / 10 + 1 = 31 times. The container therefore has 310 seconds to load before
Kubernetes gives up.

### 4.2 What each backend's probes check

vLLM answers `GET /health` once its engine is running, so all three probes use it.

Ollama has no `/health` endpoint. The first version of this repository probed one anyway, and
Ollama pods never became ready. The readiness and liveness probes now use `GET /`. The startup
probe runs `ollama show <model>`, which succeeds only after the model has been downloaded.
Ollama's web listener starts within a second, long before the weights arrive, so a web-based
startup probe would pass too early.

ADR-0002 and ADR-0003 record these decisions, including a vLLM start-up failure on Fernhill's card
that looks like running out of memory and is not.

## Part 5. Scaling on the signal that moves first

The autoscaler should add replicas when requests start to wait, not when the GPU looks busy. As
Part 1 explained, a GPU in decode looks quiet while the queue grows.

### 5.1 Why GPU utilisation scales the wrong way

At 9:00 on a Monday, Fernhill's lawyers arrive and send 40 requests in a minute. Requests queue.
Each replica is in decode, moving KV cache bytes, and reports modest utilisation. An autoscaler
watching utilisation sees a quiet GPU and removes a replica. Latency gets worse at exactly the
moment it most needs to get better.

### 5.2 Three triggers, and the arithmetic behind each

The operator writes a ScaledObject with three triggers. KEDA computes a replica count for each
and uses the largest.

| Trigger | Plain meaning | Fernhill setting |
|---|---|---|
| Waiting requests | Requests that have arrived but not started | 4 waiting per replica |
| KV cache usage | How full each replica's working-state memory is | 80% |
| p95 time to first token (TTFT) | The wait for the first word that 95 in 100 callers stay under | 2,000 ms |

Waiting requests are the main signal because they move first.

**Example: the queue.** At 9:01 there are 13 requests waiting on Fernhill's single replica. The
target is 4 per replica. KEDA asks for ceil(13 / 4) = 4 replicas. `maxReplicas` is 3, so the
service grows to 3, one replica a minute, which is the scale-up pace the operator sets.

**Example: the cache.** Two replicas report KV cache usage of 0.95 and 0.85. The sum is 1.80 and
the target is 0.80. KEDA asks for ceil(1.80 / 0.80) = ceil(2.25) = 3 replicas. Notice that
neither replica has a long queue yet. A full cache forces vLLM to pause requests already running
to make room, and this trigger acts before that shows up as waiting requests.

**Example: latency.** Two replicas are running and the observed p95 is 2,600 ms against a target
of 2,000 ms. This trigger uses KEDA's `Value` metric type, which scales in proportion:
ceil(2 × 2600 / 2000) = ceil(2.6) = 3 replicas. The first version used the default type, which
divides the metric by the replica count. That treats latency as if it shrank when spread over more
replicas, and it could never ask for more than ceil(2600 / 2000) = 2.

### 5.3 Why scaling down waits for a full weight load

Removing a replica is cheap. Adding one back costs a full weight load, five minutes for Fernhill.
So the operator makes KEDA wait at least as long as `weightLoadSeconds`, and never less than five
minutes, before it removes a replica.

`minReplicas` cannot be zero. Every one of the three signals comes from a running vLLM replica.
At zero replicas nothing reports demand, so nothing would ever bring the service back.

Ollama publishes none of these metrics. An Ollama service gets no ScaledObject and stays at
`minReplicas`, and its status says so. ADR-0001 has the full reasoning.

## Part 6. Who owns the replica count

Two programs want to write the same number. The operator must set the replica count once and then
never touch it again, or it will fight the autoscaler.

### 6.1 How two programs end up fighting over one number

The operator creates the Deployment with one replica. KEDA scales it to 3 at 9:01. If the
operator wrote its spec back on its next pass, it would set 1. KEDA would then scale to 3 again.
Neither program is wrong on its own terms, and the replica count swings back and forth.

### 6.2 Server-side apply and field managers

Kubernetes has a feature called **server-side apply**. With it, a program sends only the fields it
cares about, and the API server records which program, called a **field manager**, owns each
field. A field that a manager does not mention is left alone.

The operator applies the Deployment on every pass under the manager name `inference-operator`, and
it never mentions `replicas`. That leaves the field to whoever owns it. Creation takes two steps.

1. `inference-operator` applies the full Deployment, without `replicas`.
2. A second manager, `inference-operator-initial-replicas`, applies `replicas: 1` and nothing
   else.
3. That second manager never writes again. When KEDA's autoscaler first changes the count, it
   takes ownership of the field.

The split matters because of one more rule. When a manager stops mentioning a field it alone
owns, the API server removes the field. If `inference-operator` had set the count at creation,
its very next pass would remove it, and Kubernetes would reset the Deployment to one replica.

### 6.3 The bug the tests caught, and the selector trap

The first implementation created the Deployment with an ordinary create. An ordinary create
records the creator as the owner of every field, so later applies could not remove anything. When
a test switched a service from vLLM to Ollama, the pod ended up running both containers. The
test suite caught this before any cluster did.

One more consequence: a Deployment's selector, the labels it uses to find its pods, cannot be
changed after creation. The first version put the backend and the classification in the selector,
so changing either one made every later update fail. The selector now holds only the service's
name. Deployments created by the first version are deleted and recreated once, which costs one
weight load of downtime. ADR-0004 has the details.

## Part 7. Choosing a backend for each request

The gateway decides, for each request, which backend may answer it. Data classification decides
first and cost decides last. A cache in front of it must respect the same boundary.

### 7.1 Four filters, in order

The router in `gateway/app/router.py` filters the backends in a fixed order.

1. It removes backends not approved for the request's classification.
2. It removes backends that lack a capability the request needs, or whose context window is too
   small. The **context window** is the most text the model can consider at once.
3. It removes backends whose p95 time to first token is over the request's latency budget.
4. It picks the cheapest of what is left.

If nothing is left, it raises an error rather than quietly using a weaker backend.

**Example.** A visitor asks the website "What are your opening hours?", which is Public. Both
backends pass the first three filters. Ollama costs nothing and vLLM costs $0.52 an hour, so
Ollama answers. A lawyer asks for a contract clause as JSON, a structured text format that other programs can
read, and the request is Internal. Ollama
is approved for Internal data but lacks the `json` capability, so vLLM answers. HR's payroll
request is Restricted, and Ollama is not approved for it, so vLLM answers.

The p95 figure comes from Prometheus when it is available, through `gateway/app/metrics.py`. A
backend with no recent traffic keeps its configured figure, because zero would make an idle
backend look like the fastest. ADR-0005 explains the order.

### 7.2 Why the cache is divided by classification

A **semantic cache** stores past answers and serves one again when a new request means nearly the
same as an earlier one. It compares requests as **vectors**, lists of numbers that place similar
text close together.

The first version kept one list of answers for everyone. HR asks "Summarise the payroll file" and
the answer is cached. A Public caller sends the same words and receives the payroll summary from
the cache. The router never sees the request, so its classification filter never runs.

Each cached answer is now stored under a scope: classification, model, temperature, maximum
answer length and system prompt. **Temperature** is the amount of randomness allowed in the
answer. A request only sees answers from its own scope. The cache also keeps a bounded number of
entries and expires them after an hour by default. It still refuses a near match whose numbers
differ, because "account 118" and "account 811" look almost the same as vectors. ADR-0006 records
the decision.

### 7.3 Sharing a GPU's hourly cost between the calls that use it

A self-hosted GPU is paid for by the hour, not by the token. The ledger charges each call the
replica's hourly rate for the time it held the replica. It then divides that charge by the number
of calls sharing the replica at the time.

**Example.** A payroll summary takes 3 seconds on vLLM at 520 thousandths of a dollar an hour.
Alone, it costs 520 × 3 / 3,600 = 0.433 thousandths of a dollar. Four calls overlapped it, so it
is charged 0.433 / 4 = 0.108. Without the division, the four overlapping calls would together be
charged four times what the GPU cost for those 3 seconds. Ollama calls cost zero.

The gateway streams answers, so it records the time to first token for each call as well as the
total time.

## Part 8. Running it yourself

Everything in this part runs on a laptop except the live GPU checks. Each check in 8.3 says what it
proves and what it does not.

### 8.1 What you need

* Go 1.26, and Python 3.12 or later.
* `kind`, `kubectl` and Docker. `kind` runs a whole Kubernetes cluster inside Docker containers on
  one machine.
* For GPU serving on the cluster: an NVIDIA GPU and the NVIDIA DRA driver, which installs the
  `gpu.nvidia.com` DeviceClass. A **DeviceClass** is a named kind of device that a DRA request
  can ask for. Making a GPU visible inside `kind` also needs NVIDIA's `nvkind` setup, which this
  repository does not automate.
* Optionally, KEDA, Prometheus and the Gateway API Inference Extension CRDs.

### 8.2 The commands, in the order you run them

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"

make test       # envtest controller tests and the gateway tests
make lint       # gofmt and ruff
make validate   # schema check of the install manifests

make cluster    # create a kind cluster
make deploy     # build the operator image, load it into kind, apply config/default
make samples    # apply the two sample InferenceServices
```

`make generate manifests` regenerates three files from comments in the Go code under `api/` and
`internal/`. They are the CRD, the ClusterRole that lists what the operator may touch, and the
DeepCopy functions Kubernetes needs to copy objects safely. CI fails if the committed files differ from what the code
produces.

To run the live checks, start vLLM with `hack/serve-vllm.sh` and Ollama on port 11434, then run
`make verify`. It stops at once if either backend is down.

### 8.3 What each check proves

| Check | Plain meaning | What it covers, and what it does not |
|---|---|---|
| `make test-go` | Runs the operator against a real Kubernetes API server and etcd, the database behind it, with no other controllers | Object shapes, field ownership, the selector change, CEL rules and the samples. No pods actually run |
| `make test-py` | Runs the gateway's routing, cache, client and metrics code; the client tests use a real local web server | Every gateway rule. It does not talk to vLLM or Ollama |
| `make validate` | Checks the install manifests against the Kubernetes 1.37 schemas, offline | Field names and types. It does not install anything |
| `make verify` | Sends real requests to live vLLM and Ollama | Routing, streaming, caching and cost end to end. Needs a GPU, and was last run before the October 2026 changes |

## Part 9. Design principles

Each principle below appears somewhere in the earlier parts. This part gathers them in one place.

**1. Policy is enforced where it cannot be skipped.** Classification rules live in the CRD, so
the API server applies them on every write, even with the operator down (Part 2).

**2. Scale on the signal that moves first.** Waiting requests and cache fill rise before latency
does, and long before GPU utilisation says anything useful (Part 5).

**3. Every field has exactly one owner.** The operator owns the pod template and KEDA owns the
replica count. Server-side apply records that ownership rather than leaving it to convention
(Part 6).

**4. Refuse rather than downgrade.** When no backend meets a request's needs, the gateway raises
an error. A quiet substitution gives the caller results it cannot compare with yesterday's
(Part 7).

**5. A cache sits behind the boundary it serves.** An answer is only reused for a caller who could
have received it from the router (Part 7).

**6. Missing add-ons are reported, not fatal.** The operator runs without KEDA or the Inference
Extension and says so in status (Part 2).

The reasoning behind each decision is in `docs/adr/`.

| Record | Plain meaning |
|---|---|
| ADR-0001 | Scale on waiting requests, cache fill and first-token latency, not GPU utilisation |
| ADR-0002 | Every model container gets a startup probe sized to its weight-load time |
| ADR-0003 | Turn off vLLM's FlashInfer sampler on Fernhill's class of card, where its compile step fails |
| ADR-0004 | The operator sets the replica count once, at creation, and never again |
| ADR-0005 | Route on classification, then capability, then latency, then cost |
| ADR-0006 | Divide the semantic cache by classification, model and sampling settings |
| ADR-0007 | Treat KEDA and the Inference Extension as optional, and report their absence |

## Part 10. What is unresolved

These are the known gaps. Each needs either a test on real hardware or a decision from the
person who owns the platform.

* **No GPU cluster run yet.** The operator has been tested against a real API server, but not on a
  cluster with a GPU, the NVIDIA DRA driver and KEDA together. The first such run will be the
  first time pods actually start under the operator.
* **The image has not been built.** The Dockerfile and `make deploy` have not run in CI or here.
* **`make verify` predates these changes.** The live check needs rerunning against vLLM v0.30.0.
* **FlashInfer on the new vLLM.** The operator does not turn off the FlashInfer sampler on the pods
  it creates. Whether v0.30.0 still fails on Fernhill's card is untested (ADR-0003).
* **Scale-to-zero.** It needs a demand signal that exists with no replicas running, such as a
  queue length reported by the gateway (ADR-0001).
* **Cost per token.** The router ranks backends by hourly price. Two backends with the same price
  and different speed cost different amounts per answer (ADR-0005).
* **The cache's embedder.** The default compares words, not meaning. The 0.93 threshold has not
  been tuned against a real embedding model (ADR-0006).
* **Model-aware routing.** The InferencePool is created, but routing through llm-d's endpoint
  picker has not been tried (ADR-0007).
* **Prometheus labels.** The scaling queries assume Prometheus adds a `namespace` label to every
  scraped pod. A different scrape configuration needs the queries changed.

## Glossary

| Term | Plain meaning | Explained in |
|---|---|---|
| ADR | Architecture decision record: a short note of one decision and why it was made | Part 9 |
| API server | The Kubernetes component that stores every object and checks it on the way in | A note on accuracy, Part 2.1 |
| CEL | Common Expression Language, the rule language the API server runs to check objects | Part 2.1 |
| CI | Continuous integration: the automatic checks that run on every push | A note on accuracy, Part 8.2 |
| Classification | How sensitive a request's data is: Public, Internal or Restricted | Part 2.1, Part 7.1 |
| ClusterRole | The list of objects the operator is allowed to read and change | Part 8.2 |
| Context window | The most text a model can consider at once | Part 7.1 |
| CrashLoopBackOff | Kubernetes' status for a container that keeps failing and being restarted | Part 4.1 |
| CRD | CustomResourceDefinition: the definition of a new kind of Kubernetes object | Part 2.1 |
| Decode | The phase where the model writes its answer one token at a time; limited by memory bandwidth | Part 1.1 |
| DeepCopy | Generated Go functions that copy a Kubernetes object safely | Part 8.2 |
| Deployment | The Kubernetes object that runs and replaces copies of a program | Part 2.2 |
| DeviceClass | A named kind of device that a DRA request can ask for | Part 8.1 |
| DRA | Dynamic Resource Allocation: asking for a device by description rather than by count | Part 1.3 |
| envtest | A test harness that starts a real Kubernetes API server and etcd with no other controllers | Part 8.3 |
| etcd | The database that holds every Kubernetes object | Part 8.3 |
| Field manager | The name the API server records as the owner of a field | Part 6.2 |
| Gateway | The program that receives each request and chooses a backend for it | Part 2.4, Part 7 |
| Gateway API Inference Extension | A Kubernetes project that lets a gateway route model traffic to the best replica | Part 2.2 |
| GPU | The graphics processor that does a model's arithmetic | A note on accuracy, Part 1.1 |
| InferencePool | An object from the Gateway API Inference Extension listing the pods that serve a model | Part 2.2 |
| InferenceService | This repository's object: a description of one model to serve | Part 2.1 |
| JSON | A structured text format that other programs can read | Part 7.1 |
| KEDA | Kubernetes Event-driven Autoscaler: turns metric queries into a replica count | Part 2.2, Part 5 |
| kind | A tool that runs a whole Kubernetes cluster inside Docker containers on one machine | Part 8.1 |
| Kubelet | The agent on each node that starts containers and runs probes | Part 2.3, Part 4.1 |
| KV cache | The model's saved working state for each request, held in GPU memory | Part 1.1, Part 5.2 |
| Ledger | The gateway's record of every call: tokens, latency and cost | Part 3, Part 7.3 |
| Liveness probe | A check that restarts a container that stops answering | Part 4.1 |
| Ollama | A simple model server that runs well on a CPU | Part 2.1 |
| Operator | A program in the cluster that keeps real objects in line with a description | Part 2.2 |
| p95 | The value that 95 in 100 measurements stay under | Part 5.2 |
| Pod | A group of containers that Kubernetes schedules together | Part 2.2 |
| Prefill | The phase where the model reads the whole request at once; limited by arithmetic | Part 1.1 |
| Probe | A small check the kubelet runs on a container on a timer | Part 4.1 |
| Prometheus | The monitoring system that collects the metrics vLLM publishes | Part 2.2 |
| Readiness probe | A check that removes a pod from the Service while it is not answering | Part 4.1 |
| Reconcile | One pass of the operator, comparing the description with reality and fixing differences | Part 2.2 |
| Replica | One running copy of the model server | Part 2.2 |
| ResourceClaimTemplate | The DRA request each pod makes for its device | Part 2.2 |
| ScaledObject | KEDA's object: what to measure and when to add replicas | Part 2.2, Part 5.2 |
| Scheduler | The Kubernetes component that decides which node each pod runs on | Part 1.3 |
| Selector | The labels a Deployment or Service uses to find its pods | Part 6.3 |
| Semantic cache | A store of past answers, reused when a new request means nearly the same | Part 7.2 |
| Server-side apply | A way of writing to Kubernetes where the API server tracks who owns each field | Part 6.2 |
| Service | A stable network address that spreads requests across ready pods | Part 2.2 |
| Startup probe | A check that runs first and holds off the other probes until the program has started | Part 4.1 |
| Temperature | The amount of randomness allowed in a model's answer | Part 7.2 |
| Token | A word or piece of a word; models read and write in tokens | Part 1.1 |
| TTFT | Time to first token: how long a caller waits for the first word of the answer | Part 5.2, Part 7.3 |
| Utilisation | The share of time a GPU's arithmetic units are busy | Part 1.1, Part 5.1 |
| Vector | A list of numbers that places similar text close together | Part 7.2 |
| vLLM | A model server built for GPUs | Part 2.1 |
| Weights | The very large set of numbers that make up a model | Part 1.2 |
