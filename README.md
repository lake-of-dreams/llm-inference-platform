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
7. Part 7 covers choosing a backend for each request, caching answers, comparing cost per answer, keeping one caller group's cached prompts from another, attributing cost, deciding who waits when replicas are full, and tracing one request.
8. Part 8 shows how to run and test it, on a cluster with a GPU, and how to watch a workload.
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

The behaviour described here was checked against the source code of vLLM v0.33.0, KEDA v2.21.0,
the Gateway API Inference Extension v1.6.2, the llm-d router v1.0.0, the NVIDIA DRA driver v0.5.1
and Kubernetes 1.37. The operator runs vLLM v0.33.0 and Ollama v0.40.14. Where this guide says
something was tested, Part 8 names the test.

vLLM's images from v0.33.0 are built for CUDA 13 only. CUDA is NVIDIA's software layer between a
program and the GPU, and a node whose GPU driver is too old for CUDA 13 cannot start the container.

The guide simplifies how a model computes an answer. It describes the two phases at the level
needed to understand the scaling decisions, and no deeper.

What has run, and where:

* The operator's logic is tested against a real Kubernetes API server, the component that stores
  every object and checks it on the way in.
* The operator has run on a two-node kind cluster on a laptop, Kubernetes 1.37, with the laptop's
  one 4 GB GPU passed into the worker node. A GPU is the graphics processor that does a model's
  arithmetic. The NVIDIA DRA driver published the card as `gpu-0` with 4,094 MiB. The vLLM service
  claimed it, loaded the model and served requests, and its status named the device. The Ollama
  service, on v0.40.14, served from the CPU. KEDA reported its scaler ready against a real
  Prometheus. With the llm-d CRD installed, a priority became an objective on the pool.
* `hack/verify.py` has run against those two services in the cluster, through port-forwards: vLLM
  v0.33.0 on the GPU and Ollama v0.40.14. Every check passed, including the prefix cache salt and
  its control.
* It has not run on more than one GPU, or on a data-centre card.
* CI, the automatic checks that run on every push, builds the operator image.

Part 10 lists what is still open.

This is not a production platform. It is a small, working reference that runs on one 4 GB GPU.

## See it decide, in two minutes

The gateway's decisions run on any laptop, with no cluster, GPU or model:

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
make demo
```

Two stand-in model servers answer with a fixed sentence. The routing, the caches and the ledger
are the real code. Part of the output:

```text
2. hr: 'Summarise this grievance letter.'
   Restricted data: only the approved backend may see it
   -> qwen-restricted  (cheapest of 1 eligible (cost=833m$/Mtok, p95 TTFT=450ms))

3. hr: 'Summarise this grievance letter.'
   the same team asks again: answered from the cache, no model call
   -> answer cache  (cheapest of 1 eligible (cost=833m$/Mtok, p95 TTFT=450ms))

4. contracts: 'Summarise this grievance letter.'
   another team asks the same thing: HR's cached answer is not theirs to see
   -> qwen-restricted  (cheapest of 1 eligible (cost=833m$/Mtok, p95 TTFT=450ms))
```

Part 7 explains each of those decisions. Part 8 runs the rest.

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
  toolCallParser: hermes
  priority: 10
  autoscaling:
    minReplicas: 1
    maxReplicas: 3
    queueDepthTarget: 4
    ttftP95Milliseconds: 2000
    kvCacheUsagePercent: 80
```

The `backend` names the program that serves the model. **vLLM** is a model server built for GPUs.
**Ollama** is a simpler model server that runs well on a CPU. `costPerHourMilliUSD` is in
thousandths of a dollar, so 520 means $0.52 an hour. `priority` is optional and says which
model's requests go first when replicas are full; 7.7 explains it. `toolCallParser` is optional
too. It lets clients offer the model tools, and names the format this model writes a tool call in.
Without it vLLM refuses any request that says the model may choose a tool, which is the first
thing a chat window sends (ADR-0016).

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
| Deployment | Runs and replaces the copies of the model server. Each copy is a **replica**, and each replica runs in a **pod**, a group of containers scheduled together | One to three pods running vLLM v0.33.0 |
| Service | A stable address that spreads requests across the ready pods | `http://qwen-small.llm-platform.svc.cluster.local:8000/v1` |
| ResourceClaimTemplate | The DRA request each pod makes for its device | One `gpu.nvidia.com` device with at least 3 GiB of memory |
| ScaledObject | Instructions to KEDA, the autoscaler, on what to measure and when to add replicas | Scale on waiting requests, cache fill and first-token latency |
| InferencePool | Registers the pods with the Gateway API Inference Extension, so a model-aware gateway can route to them | A pool of the `qwen-small` pods on port 8000 |
| InferenceObjective | Only when `priority` is set. Tells llm-d how important this model's requests are when the pool is full | Priority 10 on the `qwen-small` pool |

**KEDA** is the Kubernetes Event-driven Autoscaler. It reads a metric, such as a count from
Prometheus, and tells the standard Kubernetes autoscaler how many replicas to run. **Prometheus**
is the monitoring system that collects the metrics vLLM publishes.

KEDA, the Inference Extension and llm-d are optional. If one is not installed, the operator still
builds the rest, and the InferenceService's status says what is missing. ADR-0007 records why.

The status reports what the cluster says, not what the operator did. Applying a ScaledObject only
proves the API server stored it. So the operator reads KEDA's own verdict back, and a pod the
scheduler cannot place shows as `Unschedulable` with the scheduler's reason, not as loading.

| Phase or condition | Plain meaning | Seen on the laptop clusters |
|---|---|---|
| Phase `Loading` | Pods exist and are starting; weights may take minutes | `qwen-small` for about 80 seconds while vLLM loaded weights onto the GPU |
| Phase `Unschedulable` | No node can take the pod; the reason is the scheduler's | `qwen-small` on a cluster with no GPU driver: "device class gpu.nvidia.com does not exist" |
| Phase `Failing` | A pod was placed but its container will not start: a crash loop, a failed image pull | A model name Ollama cannot pull |
| Phase `Ready` | At least one replica is serving | `phi4-mini`, which then answered a chat request |
| `Autoscaling=True`, `ScalerReady` | KEDA can read the metrics it scales on | `qwen-small`, once Prometheus was at the configured address |
| `Autoscaling=False`, `ScalerNotReady` | KEDA has the ScaledObject but says its triggers do not work | Prometheus was not at the configured address |
| `Autoscaling=Unknown`, `WaitingForKEDA` | KEDA has not reported on the ScaledObject yet | Briefly, right after creation |
| `Prioritised=True`, `ObjectiveApplied` | The priority is stored as an objective on the pool | `qwen-small` at priority 10, with the llm-d CRD installed |
| `Prioritised=False`, `InferenceObjectiveNotInstalled` | A priority is set and nothing will read it | Any service with a priority on a cluster without llm-d |
| `status.devices` | Which device the scheduler gave each pod | `gpu-0` from driver `gpu.nvidia.com`, on node `inference-worker` |

ADR-0009 records how running the operator on a real cluster found both of the wrong answers this
table replaced. ADR-0012 records why status names the device: a request for "one device with at
least 3 GiB" does not say which card answered it.

A service that holds a device is rolled out differently from one that does not. Kubernetes
normally starts a new pod before removing the old one. With one GPU, the new pod waited for the
card and the old pod kept it until the new one was ready, so the rollout never finished. A service
with a `deviceClass` now has one pod removed first and its replacement started after. ADR-0015
records the finding and what it costs: a one-replica GPU service is down while its weights reload.

### 2.3 Who does what, and why

Several programs make decisions in this system. Some of the decisions follow fixed rules and
others respond to live measurements. The table puts each decision where it belongs.

| Task | Who does it | Why |
|---|---|---|
| Reject a disallowed spec | The API server, using the CRD's CEL rules | It runs on every write, so a bad spec is refused before any pod exists, even with the operator down |
| Build and correct the objects in 2.2 | The operator | One owner per object keeps the objects consistent with the spec |
| Set the replica count after creation | KEDA, through the Kubernetes autoscaler | Only it sees live demand; Part 6 explains how the operator keeps out of its way |
| Choose a device for each pod | The Kubernetes scheduler, with DRA | Only the scheduler sees every node's free devices |
| Decide when a pod is ready | The kubelet, the agent on each node, using the probes | It is on the node and checks every few seconds |
| Decide who waits when replicas are full | llm-d's endpoint picker, using the priority the operator wrote | It sees every waiting request for the pool; a model server only sees its own queue |
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
that looks like running out of memory and is not. Its first diagnosis blamed the card; the last line
of the traceback said a build tool, `ninja`, was not on `PATH`. Guide 5 walks through how each
reading of that log was made, and where the first one stopped too soon.

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
4. It picks the cheapest of what is left. It compares cost per answer where it can, and falls back
   to price per hour where it cannot (7.4).

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

### 7.4 Comparing cost per answer, not per hour

An hourly price says what a replica costs to keep running. It does not say what an answer costs,
because replicas differ in how many tokens they produce in that hour. The router therefore compares
**cost per million generated tokens**: the hourly price divided by the tokens a replica produces
in an hour.

The tokens a replica produces depend on how busy it is. The router uses the **throughput** a
replica sustains when busy, measured once with a load test such as `vllm bench serve` and set as
`tokens_per_second` on the backend. It does not use the rate Prometheus observes. An idle replica
produces almost no tokens, so its observed cost per token would look enormous at exactly the moment
it has the most room.

**Example.** Fernhill considers renting a second GPU for contract work. Two offers pass the first
three filters.

| Offer | Price per hour | Busy throughput | Cost per million tokens |
|---|---|---|---|
| Older card | 520 thousandths of a dollar | 100 tokens a second | 520 / (100 × 3,600) × 1,000,000 = 1,444 |
| Newer card | 800 thousandths of a dollar | 400 tokens a second | 800 / (400 × 3,600) × 1,000,000 = 556 |

The older card is cheaper by the hour and costs 2.6 times as much per answer. Ranked by hourly
price, the router would pick the older card. Ranked per token, it picks the newer one.

A backend with no throughput figure cannot be priced per answer. The router ranks it after every
backend that can be, and ranks such backends among themselves by hourly price, as before. The
routing reason names the basis it used, so "why did this go there" stays answerable. ADR-0005
records the change.

### 7.5 The model server's own cache, and why it is salted

Part 1 explained the KV cache: the model's working notes on everything it has read. vLLM keeps
these notes after a request finishes. If a new request starts with exactly the same text, such as
the same long system prompt or the same contract, vLLM reuses the notes instead of reading that
text again. This is called **prefix caching**, and it makes the first word of the answer arrive
sooner.

That speed can be measured by whoever sent the request, and that is the problem. Suppose a caller
from one group guesses the opening of a document another group uses and sends it. A fast first word
means somebody already sent that text. Learning a secret from how long something takes is called a
**timing side channel**.

vLLM lets each request carry a **cache salt**, a string mixed into the cache's fingerprint for the
first block of text. Only requests with the same salt can reuse each other's notes. The gateway
computes one salt for each pair of tenant, the group a caller belongs to, and classification, in
`prefix_cache_salt` in `gateway/app/cache.py`. It sends the salt only to backends marked as
accepting one, which today means vLLM.

The salt is not the group's name. It is an HMAC, a keyed fingerprint, of the tenant and
classification under a secret that only the gateway holds. Anyone who can reach vLLM directly can
send whatever salt they like. A salt that could be guessed would let them join another group's
cache.

**Example.** Fernhill's HR team and its contracts team both use vLLM. HR's payroll prompts start
with the same 1,500-token instruction. Without salts, a contracts user who obtained that instruction
could send it and see a first word in 80 ms instead of 600 ms, which confirms HR is using it. With
salts, the contracts user's request carries a different salt, finds no cached notes, and takes
600 ms like any new text. HR's own requests still share notes with each other, because they carry
the same salt. ADR-0008 records the decision.

### 7.6 One request, end to end, and the answer cache that crossed teams

The router, the answer cache, the salt and the ledger each work alone. `Gateway.handle` in
`gateway/app/gateway.py` runs them together for one request, and it is the one place that requires
a tenant. Step by step:

1. A request with no tenant is refused, before anything else.
2. The router chooses a backend, or refuses (7.1).
3. The answer cache is searched in the request's own scope.
4. On a miss, the backend is called with the tenant's salt (7.5), and the answer is cached.
5. The call is recorded in the ledger under the tenant, at zero cost if it came from the cache.

Writing this exposed a leak. The answer cache was divided by classification (7.2) but not by
tenant, so two teams at the same classification shared cached answers.

**Example.** HR asks "summarise the payroll file", marked Internal, and the answer is cached. A
contracts user sends the same words, also Internal. Before the fix, the contracts user received
HR's summary from the cache: the whole answer, not a timing hint. The cache scope now includes the
tenant, and the contracts request goes to the model. The ledger can now also say what each team
spent. ADR-0010 records the change, and Guide 4 walks through it.

### 7.7 When replicas are full: priority and fairness

A replica can serve only so many requests at once. Past that, requests wait in the model server's
queue in the order they arrived, and a nightly batch job can sit ahead of a clerk who is waiting at
a screen.

**llm-d** is an open-source project that puts a request scheduler, the **endpoint picker**, in
front of a pool of model servers. Its **flow control** holds requests that cannot be served yet and
releases them by priority, sharing capacity between callers. Requests with a negative priority are
the first to be dropped under overload.

The pieces here fit that as follows:

1. `spec.priority` on the InferenceService becomes an `InferenceObjective` on the service's pool
   (2.2).
2. The gateway adds two headers to a request for a backend behind llm-d: the objective's name, and
   the tenant as the fairness group, so one team's burst queues behind itself.
3. llm-d's picker does the queuing. Nothing in this repository queues or drops a request.

**Example.** Fernhill sets the payroll model to priority 10 and a document-summarising batch model
to priority -1 on the same GPUs. On a busy afternoon the payroll questions go first and the batch
summaries wait, or are refused and retried later.

The operator stores the objective and says so in status. Whether requests are actually queued by it
depends on llm-d's picker running with flow control switched on, which has not been tried here
(Part 10). ADR-0011 records the decision.

### 7.8 Following one request: tracing

The ledger adds up what each team spent. A **trace** shows one request: a timed record, called a
**span**, that a trace viewer can display. The gateway writes one span for each request that
reaches a backend, using the attribute names OpenTelemetry publishes for calls to language models.

| On the span | Plain meaning | Example |
|---|---|---|
| `gen_ai.request.model`, `gen_ai.usage.*` | Which model, and how many tokens went in and came out | `qwen-small`, 9 in, 4 out |
| `gateway.backend`, `gateway.routing.reason` | Where the request went and the router's reason | `qwen-restricted`, "cheapest of 1 eligible" |
| `gateway.tenant`, `gateway.classification` | Whose request, and how sensitive | `hr`, `RESTRICTED` |
| `gateway.cached`, `gateway.ttft_ms`, `gateway.cost_milli_usd` | Whether a model was called, how long the first word took, what it cost | `false`, 412, 0.31 |
| `error.type` | The kind of failure, when the backend call failed | `BackendError` |

The prompt and the answer are never on the span. More people can usually read a trace store than
are cleared to read a Restricted prompt, and a test searches every recorded field for a salary
figure sent in a prompt.

Tracing is off unless the OpenTelemetry API package is installed
(`pip install -e ".[tracing]"`) and the program running the gateway has configured where traces go.
The `gen_ai.*` names are still marked as in development by OpenTelemetry and may change. ADR-0013
records the decision.

## Part 8. Running it yourself

Everything in this part runs on a laptop except the live GPU checks. Each check in 8.3 says what it
proves and what it does not.

### 8.1 What you need

* Go 1.26, and Python 3.12 or later.
* `kind`, `kubectl` and Docker. `kind` runs a whole Kubernetes cluster inside Docker containers on
  one machine.
* For GPU serving on the cluster: an NVIDIA GPU; the NVIDIA Container Toolkit set as Docker's
  default runtime, which needs root once; `nvkind`, NVIDIA's wrapper that passes the host's GPUs
  into a kind node; and `kind` v0.33 or later, the first that can start Kubernetes 1.37.
  `make cluster-gpu` and `make gpu-driver` do the rest. The driver installs the `gpu.nvidia.com`
  DeviceClass. A **DeviceClass** is a named kind of device that a DRA request can ask for.
* `helm`, for the driver, KEDA and the monitoring stack.
* Optionally, the Gateway API Inference Extension and llm-d CRDs.
* To see a device claim allocated without a GPU, the Kubernetes example DRA driver
  (`dra-example-driver`), which publishes pretend devices under the class `gpu.example.com`.

### 8.2 The commands, in the order you run them

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"

make demo       # the gateway's decisions with stand-in backends; no cluster needed
make test       # envtest controller tests and the gateway tests
make lint       # gofmt and ruff
make validate   # schema check of the install manifests

make cluster    # create a kind cluster with no GPU, or:
make cluster-gpu gpu-driver   # one whose worker node has the host's NVIDIA GPU
make deploy     # build the operator image, load it into kind, apply config/default
make samples    # apply the two sample InferenceServices

make observability   # Prometheus, GPU metrics, traces and a Grafana dashboard
make routes          # a hostname for each UI and model API, with no port-forwards
make chat            # a chat window with a menu of both models
make load            # a ramp of requests, to watch on the dashboard
```

`make generate manifests` regenerates three files from comments in the Go code under `api/` and
`internal/`. They are the CRD, the ClusterRole that lists what the operator may touch, and the
DeepCopy functions Kubernetes needs to copy objects safely. CI fails if the committed files differ from what the code
produces.

To run the live checks, start vLLM with `hack/serve-vllm.sh` and Ollama on port 11434, then run
`make verify`. It stops at once if either backend is down. To check the two services the operator
runs in the cluster instead, forward a local port to each and say where they are:

```bash
kubectl -n llm-platform port-forward svc/qwen-small 8001:8000 &
kubectl -n llm-platform port-forward svc/phi4-mini 11435:8000 &
OLLAMA_URL=http://localhost:11435 make verify
```

### 8.4 Watching a workload: GPU, tokens, memory and network on one page

`make observability` installs a monitoring stack sized for a laptop cluster. `make load` then
gives it something to show.

| Piece | Plain meaning | What it adds |
|---|---|---|
| Prometheus | Collects numbers from every pod, here every five seconds | The store the dashboard, KEDA and the operator all read |
| dcgm-exporter | NVIDIA's GPU metrics exporter | Utilisation, Tensor Core activity, memory, power, temperature |
| node-exporter, kube-state-metrics, cAdvisor | Standard node and pod metrics | Memory, CPU and network for every pod and node |
| Tempo | Stores traces | One trace per vLLM request and one per model start-up (ADR-0014) |
| Token meter | A small proxy in front of the Ollama service, in `hack/observability/token-meter` | Token counts for Ollama, which publishes no metrics of its own |
| Grafana | Draws dashboards | "GPU workload activity": the GPU, the model server, the pods and the node on one time axis |

To open it: `kubectl -n monitoring port-forward svc/grafana 3001:80`, then
`http://localhost:3001`. There is no sign-in, which suits a laptop and nothing else.

`make routes` removes the need for port-forwards. It installs a gateway with one fixed address
and gives each service a hostname through an **HTTPRoute**, the Gateway API object that says
"requests for this hostname go to this Service".

| Address | What is there |
|---|---|
| `http://grafana.172.18.255.200.nip.io` | The dashboard and the traces |
| `http://prometheus.172.18.255.200.nip.io` | Raw metrics and queries |
| `http://headlamp.172.18.255.200.nip.io` | A web UI for the cluster; sign in with `kubectl -n headlamp create token headlamp` |
| `http://qwen-small.172.18.255.200.nip.io/v1` | The vLLM service's API |
| `http://phi4-mini.172.18.255.200.nip.io/v1` | The Ollama service's API |
| `http://chat.172.18.255.200.nip.io` | After `make chat`: a chat window with a menu of both models |

`make chat` deploys Open WebUI, an open-source chat interface, pointed at both services through
their OpenAI-compatible APIs. Its built-in tools are switched off: attached to every message,
they made a one-line question to the CPU model take 90 seconds with nothing on screen until the
end. Pick `qwen-small` or `phi4-mini:latest` from the menu and type. Keep
the Grafana dashboard open beside it: each message shows up there as a request, a burst of tokens
and a new trace. A model can also be called directly:

```bash
curl http://qwen-small.172.18.255.200.nip.io/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "qwen-small", "messages": [{"role": "user", "content": "What is a GPU?"}]}'
```

Two pieces make that work on a laptop. **MetalLB** gives the gateway an address on the Docker
network kind uses, which a Linux host can reach directly. **nip.io** is a public DNS service that
answers any name ending in `172.18.255.200.nip.io` with that address, so no hosts file needs
editing. The addresses work only from this machine, over plain HTTP. On macOS or Windows the Docker
network is not reachable from the host, and the port-forward is still the way in.

**Example: what one load ramp looked like.** `make load` steps from 1 caller to 24 against
`qwen-small` on the 4 GB card. The model server admits 16 requests at once.

| Callers | Tokens written per second | What the dashboard showed |
|---|---|---|
| 1 | 88 | One request running, none waiting |
| 4 | 350 | |
| 8 | 657 | |
| 16 | 1,179 | Every slot full |
| 24 | 1,197 | 16 running and 8 waiting: more callers, no more tokens |

Three things on the page are worth looking at during that run:

1. **"GPU busy" read 100% while Tensor Core activity peaked at 15% and memory bandwidth at 75%.**
   The card was working flat out, and the limit was moving weights through memory, not arithmetic.
   This is Part 1.1 measured on real hardware, and it is why ADR-0001 does not scale on GPU
   utilisation.
2. **Waiting requests rose to 8, and KEDA asked for a second replica.** The new pod stayed
   `Pending`: the cluster has one GPU and the first replica holds it. The autoscaler was right
   about demand and the scheduler was right about supply.
3. **96% of prompt tokens came from the prefix cache**, because half the prompts shared a long
   opening. The pod's network traffic peaked at a few hundred bytes a second: tokens are tiny.

Each request also leaves a trace with its time in the queue, the time spent reading the prompt,
the time spent writing the answer and the token counts. A model start-up leaves one too, with a
span per stage. Neither holds prompt or answer text, which was checked by sending a marker word in
a prompt and searching the stored trace for it.

### 8.3 What each check proves

| Check | Plain meaning | What it covers, and what it does not |
|---|---|---|
| `make demo` | Sends five requests through the real gateway code to two stand-in servers | The routing, cache and ledger decisions, in a form you can read. It proves nothing about a real model |
| `make test-go` | Runs the operator against a real Kubernetes API server and etcd, the database behind it, with no other controllers | Object shapes, field ownership, the selector change, CEL rules and the samples. No pods actually run |
| `make test-py` | Runs the gateway's routing, cache, client and metrics code; the client tests use a real local web server | Every gateway rule. It does not talk to vLLM or Ollama |
| `make validate` | Checks the install manifests against the Kubernetes 1.37 schemas, offline | Field names and types. It does not install anything |
| `make deploy` and `make samples` | Builds the operator image, runs it in a kind cluster and applies the two samples | The operator's real behaviour with a scheduler, KEDA and a kubelet. On a laptop with no GPU driver in kind, the vLLM sample can only reach `Unschedulable` |
| `make verify` | Sends real requests to live vLLM and Ollama | Routing, streaming, caching and cost end to end, the prefix cache salt with a control that shows the leak it closes, and a warmed-up throughput measurement. Needs a GPU |

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

**6. Compare what an answer costs.** Two replicas at the same hourly price can differ fourfold per
answer, so the router compares cost per token wherever throughput is known (Part 7).

**7. Every cache is divided along the same lines.** The gateway's own cache and vLLM's prefix cache
both stop at a classification and tenant boundary (Part 7).

**8. Status says what happened, not what was attempted.** KEDA's verdict and the scheduler's reason
are read back and reported (Part 2).

**9. Missing add-ons are reported, not fatal.** The operator runs without KEDA or the Inference
Extension and says so in status (Part 2).

The reasoning behind each decision is in `docs/adr/`.

Five guides in `docs/guides/` teach one subject each from zero, with Fernhill as the running
example and real measurements from the laptop GPU:

| Guide | Plain meaning |
|---|---|
| `01-cost-per-answer-from-zero.md` | Why an hourly price does not tell you what an answer costs, and how the router prices per token |
| `02-prefix-caching-and-timing-from-zero.md` | How the model server's memory of earlier prompts leaks across teams, and how a keyed salt stops it |
| `03-an-operator-on-a-real-cluster-from-zero.md` | The operator's first run on a real cluster, the wrong status answers it exposed, and crash loops reported as they happen |
| `04-one-request-through-the-gateway-from-zero.md` | The five steps one request takes through the gateway, and the answer cache that crossed teams |
| `05-reading-a-crash-log-from-zero.md` | A vLLM start-up failure read twice: the first diagnosis blamed the card, the last line of the traceback named a missing `PATH` entry |

| Record | Plain meaning |
|---|---|
| ADR-0001 | Scale on waiting requests, cache fill and first-token latency, not GPU utilisation |
| ADR-0002 | Every model container gets a startup probe sized to its weight-load time |
| ADR-0003 | The FlashInfer sampler's failure on the laptop card was a missing `PATH` entry for `ninja`, not the hardware |
| ADR-0004 | The operator sets the replica count once, at creation, and never again |
| ADR-0005 | Route on classification, then capability, then latency, then cost |
| ADR-0006 | Divide the semantic cache by classification, model and sampling settings |
| ADR-0007 | Treat KEDA and the Inference Extension as optional, and report their absence |
| ADR-0008 | Salt vLLM's prefix cache per tenant and classification, with a keyed hash |
| ADR-0009 | Report KEDA's and the scheduler's verdicts in status, not the operator's own actions |
| ADR-0010 | One entry point per request, and an answer cache divided by tenant |
| ADR-0011 | A model's priority is an llm-d objective on its pool, and status says when nothing will read it |
| ADR-0012 | Status names the device the scheduler gave each pod |
| ADR-0013 | Tracing is opt-in, uses OpenTelemetry's names, and never carries the prompt |
| ADR-0014 | The operator can tell every vLLM server where to send its request and start-up traces |
| ADR-0015 | A service that holds a device has a pod taken down before its replacement starts |
| ADR-0016 | Tool calling is switched on per model, by naming the parser for its tool-call format |

## Part 10. What is unresolved

These are the known gaps. Each needs either a test on real hardware or a decision from the
person who owns the platform.

* **One small GPU.** Everything on real hardware ran on one 4 GB laptop card in a kind cluster.
  Nothing here has run on a data-centre card, on several GPUs, or on a multi-node GPU cluster.
* **A scaled-up replica with no device waits silently.** Under load KEDA asked for a second replica
  and the pod stayed `Pending`, because the only GPU was in use. Status still read `Ready`, since
  one replica was serving. Nothing reports that demand exceeded the devices available.
* **A rollout of a one-replica GPU service means downtime.** The old pod is removed before the new
  one loads its weights (ADR-0015).
* **Prometheus is where the cluster says.** The ScaledObject's address is a start-up flag. On the
  laptop cluster nothing listened there, and the status now says so (ADR-0009).
* **CUDA 13.** The v0.33.0 image needs a node driver recent enough for CUDA 13. The operator cannot see a node's driver version. A node that is too old shows up as a crash loop, which status reports with the container's last exit (Part 2.2), but the message will not say "driver".
* **Scale-to-zero.** It needs a demand signal that exists with no replicas running. llm-d's
  endpoint picker holding requests in its queue is one (ADR-0011); it has not been tried.
* **Priority has been stored, not exercised.** The objective exists on the pool. No request has been
  queued or shed by it, because that needs llm-d's picker with flow control on and real load.
* **Throughput figures are typed in.** Cost per token needs each backend's busy throughput, which
  someone measures with a load test and enters. A figure that goes stale after a model or card
  change misprices that backend until it is measured again (ADR-0005).
* **The tenant must come from a verified identity.** `Gateway.handle` requires a tenant, but the
  gateway has no network front end or sign-in. Whatever calls it must read the tenant from a
  verified identity, never from a header the caller writes (ADR-0010). The gateway's secret key also
  needs a home and a rotation plan; rotating it empties every group's prefix cache once (ADR-0008).
* **The cache's embedder.** The default compares words, not meaning. The 0.93 threshold has not
  been tuned against a real embedding model (ADR-0006).
* **Model-aware routing.** The InferencePool and the objective are created, but routing through
  llm-d's endpoint picker has not been tried (ADR-0007).
* **Ollama is measured from outside.** Its tokens are counted by a proxy that reads the usage
  figures each answer ends with. There is no queue depth, no cache figure and no trace for it, and
  a caller that reaches the Service directly is not counted.
* **The gateway's traces and vLLM's are separate.** The gateway does not pass a trace context to
  the backend, so one request shows as two unrelated traces (ADR-0014).
* **Prometheus labels.** The scaling queries assume Prometheus adds a `namespace` label to every
  scraped pod. A different scrape configuration needs the queries changed.

## Glossary

| Term | Plain meaning | Explained in |
|---|---|---|
| ADR | Architecture decision record: a short note of one decision and why it was made | Part 9 |
| API server | The Kubernetes component that stores every object and checks it on the way in | A note on accuracy, Part 2.1 |
| Cache salt | A string mixed into vLLM's prefix cache so that only requests carrying the same salt share cached work | Part 7.5 |
| cAdvisor | The part of the kubelet that measures each container's CPU, memory and network | Part 8.4 |
| CEL | Common Expression Language, the rule language the API server runs to check objects | Part 2.1 |
| CI | Continuous integration: the automatic checks that run on every push | A note on accuracy, Part 8.2 |
| Classification | How sensitive a request's data is: Public, Internal or Restricted | Part 2.1, Part 7.1 |
| ClusterRole | The list of objects the operator is allowed to read and change | Part 8.2 |
| Context window | The most text a model can consider at once | Part 7.1 |
| Cost per million tokens | The hourly price divided by the tokens a replica generates in an hour when busy, times a million | Part 7.4 |
| CrashLoopBackOff | Kubernetes' status for a container that keeps failing and being restarted | Part 4.1 |
| CRD | CustomResourceDefinition: the definition of a new kind of Kubernetes object | Part 2.1 |
| CUDA | NVIDIA's software layer between a program and the GPU | A note on accuracy |
| dcgm-exporter | NVIDIA's program that publishes GPU measurements for Prometheus | Part 8.4 |
| Decode | The phase where the model writes its answer one token at a time; limited by memory bandwidth | Part 1.1 |
| DeepCopy | Generated Go functions that copy a Kubernetes object safely | Part 8.2 |
| Deployment | The Kubernetes object that runs and replaces copies of a program | Part 2.2 |
| DeviceClass | A named kind of device that a DRA request can ask for | Part 8.1 |
| DRA | Dynamic Resource Allocation: asking for a device by description rather than by count | Part 1.3 |
| Endpoint picker | llm-d's request scheduler, which chooses a replica for each request | Part 7.7 |
| envtest | A test harness that starts a real Kubernetes API server and etcd with no other controllers | Part 8.3 |
| etcd | The database that holds every Kubernetes object | Part 8.3 |
| Example DRA driver | A Kubernetes project that publishes pretend devices, for testing device claims without hardware | Part 8.1 |
| Fairness id | The group whose requests share a queue under flow control; here, the tenant | Part 7.7 |
| Field manager | The name the API server records as the owner of a field | Part 6.2 |
| Flow control | Holding requests that cannot be served yet and releasing them by priority | Part 7.7 |
| Gateway | The program that receives each request and chooses a backend for it | Part 2.4, Part 7 |
| Gateway API Inference Extension | A Kubernetes project that lets a gateway route model traffic to the best replica | Part 2.2 |
| GPU | The graphics processor that does a model's arithmetic | A note on accuracy, Part 1.1 |
| Grafana | A program that draws dashboards from stored measurements | Part 8.4 |
| HMAC | A keyed fingerprint: only someone holding the secret key can produce or predict it | Part 7.5 |
| InferenceObjective | llm-d's object giving a pool's requests a priority | Part 2.2, Part 7.7 |
| InferencePool | An object from the Gateway API Inference Extension listing the pods that serve a model | Part 2.2 |
| InferenceService | This repository's object: a description of one model to serve | Part 2.1 |
| JSON | A structured text format that other programs can read | Part 7.1 |
| KEDA | Kubernetes Event-driven Autoscaler: turns metric queries into a replica count | Part 2.2, Part 5 |
| kind | A tool that runs a whole Kubernetes cluster inside Docker containers on one machine | Part 8.1 |
| Kubelet | The agent on each node that starts containers and runs probes | Part 2.3, Part 4.1 |
| KV cache | The model's saved working state for each request, held in GPU memory | Part 1.1, Part 5.2 |
| Ledger | The gateway's record of every call: tokens, latency and cost | Part 3, Part 7.3 |
| Liveness probe | A check that restarts a container that stops answering | Part 4.1 |
| llm-d | An open-source project that schedules requests across a pool of model servers | Part 7.7 |
| nvkind | NVIDIA's wrapper around kind that passes the host's GPUs into a node | Part 8.1 |
| Ollama | A simple model server that runs well on a CPU | Part 2.1 |
| OpenTelemetry | A standard for traces and metrics, with published attribute names | Part 7.8 |
| Operator | A program in the cluster that keeps real objects in line with a description | Part 2.2 |
| p95 | The value that 95 in 100 measurements stay under | Part 5.2 |
| Pod | A group of containers that Kubernetes schedules together | Part 2.2 |
| Prefill | The phase where the model reads the whole request at once; limited by arithmetic | Part 1.1 |
| Prefix caching | Reusing the model's working notes for text that starts the same as an earlier request | Part 7.5 |
| Priority | An integer saying whose requests go first when replicas are full; negative may be dropped | Part 2.1, Part 7.7 |
| Probe | A small check the kubelet runs on a container on a timer | Part 4.1 |
| Prometheus | The monitoring system that collects the metrics vLLM publishes | Part 2.2 |
| Readiness probe | A check that removes a pod from the Service while it is not answering | Part 4.1 |
| Reconcile | One pass of the operator, comparing the description with reality and fixing differences | Part 2.2 |
| Replica | One running copy of the model server | Part 2.2 |
| ResourceClaim | The object Kubernetes creates for each pod's device request; the scheduler records the chosen device in it | Part 2.2 |
| ResourceClaimTemplate | The DRA request each pod makes for its device | Part 2.2 |
| ScaledObject | KEDA's object: what to measure and when to add replicas | Part 2.2, Part 5.2 |
| Scheduler | The Kubernetes component that decides which node each pod runs on | Part 1.3 |
| Selector | The labels a Deployment or Service uses to find its pods | Part 6.3 |
| Semantic cache | A store of past answers, reused when a new request means nearly the same | Part 7.2 |
| Server-side apply | A way of writing to Kubernetes where the API server tracks who owns each field | Part 6.2 |
| Service | A stable network address that spreads requests across ready pods | Part 2.2 |
| Span | One timed record in a trace | Part 7.8 |
| Startup probe | A check that runs first and holds off the other probes until the program has started | Part 4.1 |
| Temperature | The amount of randomness allowed in a model's answer | Part 7.2 |
| Tempo | A store for traces | Part 8.4 |
| Tenant | The group a caller belongs to, whose cached work is kept apart from other groups' | Part 7.5 |
| Tensor Core activity | The share of time the GPU's matrix units were working; the honest measure of model arithmetic | Part 8.4 |
| Throughput | Tokens a replica generates per second when it is busy, measured with a load test | Part 7.4 |
| Timing side channel | Learning a secret from how long something takes rather than from what it returns | Part 7.5 |
| Token | A word or piece of a word; models read and write in tokens | Part 1.1 |
| Trace | A timed record of one request that a trace viewer can display | Part 7.8 |
| TTFT | Time to first token: how long a caller waits for the first word of the answer | Part 5.2, Part 7.3 |
| Utilisation | The share of time a GPU's arithmetic units are busy | Part 1.1, Part 5.1 |
| Vector | A list of numbers that places similar text close together | Part 7.2 |
| vLLM | A model server built for GPUs | Part 2.1 |
| Weights | The very large set of numbers that make up a model | Part 1.2 |
