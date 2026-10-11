# Guide 3. An operator on a real cluster, from zero

## How to read this guide

**Who it is for.** Engineers who have written or will write a Kubernetes operator, and anyone who
reads an operator's status to decide whether a service is working. You should know roughly what a
container and a Kubernetes cluster are. You do not need to have written an operator. Each term is
explained when it first appears.

**What you will know at the end.** What an operator does, how it reports what it has done, and why
a test suite that passes can still leave its status wrong. You will see the first run of this
repository's operator on a real cluster, the two wrong answers that run exposed, and how the status
now reports what the cluster says rather than what the operator did.

**How it is organised.**

1. Part 1 explains what an operator is and what it reconciles.
2. Part 2 explains status: phases and conditions.
3. Part 3 explains how the operator is tested without a cluster, and what that cannot show.
4. Part 4 walks through the first run on a real cluster.
5. Part 5 explains the first wrong answer: autoscaling that did not work.
6. Part 6 explains the second wrong answer: weights that were never loading.
7. Part 7 explains a smaller finding: a warning on every apply.
8. Part 8 shows the fixed status, and the request that proved a pod could serve.
9. Part 9 lists the design principles.
10. Part 10 lists what is unresolved.

A glossary at the end defines every term.

You can read it front to back like a short book. Each part builds on the one before it.

## The idea in one paragraph

An operator is a program that turns a short description of what you want, such as "serve this model
on a GPU", into the many Kubernetes objects that make it happen, and then reports how it is going.
This repository's operator had been tested thoroughly against a real Kubernetes database, but never
run in a working cluster. The first real run showed its report was wrong twice. It said autoscaling
was set up when the autoscaler itself said it was broken. It said a model was loading when the pod
could never start, because there was no GPU. Both times the operator reported what it had done, not
what had happened. It now reads back the autoscaler's verdict and the scheduler's reason, and
reports those.

## A note on accuracy

The operator is in `internal/controller/`. The tests are in
`internal/controller/inferenceservice_controller_test.go`. The decision records are ADR-0007 and
ADR-0009. Every status message quoted in this guide is copied from the cluster.

The cluster was a three-node kind cluster running Kubernetes 1.35, on a laptop, with KEDA installed,
no Prometheus at the operator's configured address, and no GPU driver inside the cluster. A real
platform would differ in all three. That is what made it a useful test.

## The running example

Fernhill Legal is a law firm in Leeds. It wants two models running in its cluster:

* `qwen-small`, a small model on vLLM, for payroll summaries. Payroll data is Restricted, so this
  model must run on an approved GPU.
* `phi4-mini`, a small model on Ollama, for internal questions. It runs on an ordinary CPU.

Fernhill describes each model in one short file, an InferenceService, and applies both. The cluster
it applies them to is the laptop cluster described above.

---

## Part 1. What an operator does

### 1.1 Declarative descriptions

In Kubernetes you do not give step-by-step instructions. You write down the state you want, and
programs in the cluster work to make it true. That style is called **declarative**.

Kubernetes lets a project add its own kinds of object, through a **CustomResourceDefinition**, or
**CRD**. This repository adds one kind: the **InferenceService**. Fernhill's file for `qwen-small`
says which model, which serving program, which data classification, which kind of GPU, and how to
scale.

### 1.2 The operator and the reconcile loop

An **operator** is a program that watches objects of a kind and makes the rest of the cluster match
them. For each InferenceService, this operator creates five objects:

| Object | Plain meaning | For `qwen-small` |
|---|---|---|
| ResourceClaimTemplate | A request for a GPU by description: "one GPU with at least 3 GiB" | Asks for a `gpu.nvidia.com` device |
| Deployment | Runs and replaces the copies of the model server | vLLM pods |
| Service | A stable address that spreads requests across the copies | `qwen-small` on port 8000 |
| ScaledObject | Instructions to KEDA, the autoscaler | Scale on waiting requests, cache fill and latency |
| InferencePool | Registers the pods with a model-aware gateway, if one is installed | Not installed here |

Each pass of the operator over one object is called a **reconcile**. It compares what exists with
what should, fixes the difference, and writes a report. The report is the object's **status**.

## Part 2. Status: phases and conditions

### 2.1 Phase

The **phase** is a one-word summary, shown when you list the objects:

```
NAME         BACKEND   MODEL                        CLASS        PHASE
phi4-mini    Ollama    phi4-mini                    Internal     Ready
qwen-small   vLLM      Qwen/Qwen2.5-0.5B-Instruct   Restricted   Unschedulable
```

### 2.2 Conditions

**Conditions** are a list of named statements, each `True`, `False` or `Unknown`, with a short
machine-readable **reason** and a sentence for people. This operator reports four.

| Condition | Plain meaning |
|---|---|
| `Admitted` | The description passes the placement rules, such as "Restricted data needs an approved GPU" |
| `Ready` | At least one copy of the model is serving |
| `Autoscaling` | KEDA is managing the number of copies |
| `Routing` | The pods are registered with a model-aware gateway |

### 2.3 Why status matters more than it looks

People and programs act on status. An engineer waits when it says "loading". An alert stays quiet
when autoscaling says `True`. A wrong status is not a cosmetic bug: it tells the reader not to look
at the thing that is broken.

## Part 3. Tested without a cluster

### 3.1 envtest

The operator's tests use **envtest**: a real Kubernetes API server and its database, etcd, started
on the test machine. The **API server** is the component that stores every object and checks it on
the way in. Running against the real one means the tests exercise exactly how Kubernetes stores,
merges and validates objects.

### 3.2 What envtest leaves out

envtest starts no other programs. There is no scheduler to place pods, no kubelet to run them, and
no KEDA to read a ScaledObject. A pod created in envtest never runs, and a ScaledObject is never
examined. So any status that depends on what those programs think cannot be tested there, only
assumed.

Keep that gap in mind. Both wrong answers in this guide lived in it.

## Part 4. The first real run, step by step

1. `make deploy` built the operator into a container image and loaded it into the kind cluster.
2. The operator started, found KEDA's CRD, did not find the InferencePool CRD, and logged that it
   would not watch InferencePools.
3. It found a `qwen-small` left over from an older version. That old description had no GPU device
   class, so the operator marked it `Rejected` with the reason "vLLM backend requires a deviceClass:
   the vLLM image is built for CUDA and does not serve on CPU". This was correct.
4. `make samples` applied the current `phi4-mini` and `qwen-small`.
5. The operator created every object in the table in Part 1.2 for `qwen-small`, and every object
   except the claim and ScaledObject for `phi4-mini`.
6. It reported the status in Figure 1.

```
qwen-small
  Admitted=True      PolicySatisfied: spec passes placement policy
  Autoscaling=True   ScaledObjectApplied: KEDA scales on queue depth, KV cache usage and p95 TTFT
  Routing=False      InferencePoolNotInstalled: the InferencePool CRD is not installed
  Ready=False        WeightsLoading: no ready replicas yet
```

*Figure 1. The status of `qwen-small` before the fix. Look at `Autoscaling` and `Ready`: both are
wrong.*

## Part 5. Autoscaling that did not work

### 5.1 What KEDA said

KEDA writes its own status on the ScaledObject. It said:

```
Ready=False  TriggerError: Triggers defined in ScaledObject are not working correctly
```

The ScaledObject told KEDA to read metrics from Prometheus at
`http://prometheus.monitoring.svc.cluster.local:9090`. Nothing was listening there. KEDA could not
read a single metric, so it could never change the number of copies.

### 5.2 Why the operator said True

The operator set `Autoscaling=True` as soon as the API server accepted the ScaledObject. That proves
the object was stored. It says nothing about whether KEDA can use it. The condition reported the
operator's own action.

### 5.3 The fix

After applying the ScaledObject, the operator reads it back and reports KEDA's own `Ready` condition.

| KEDA's `Ready` | Operator's `Autoscaling` | Reason |
|---|---|---|
| `True` | `True` | `ScalerReady` |
| `False` | `False` | `ScalerNotReady`, quoting KEDA's reason and message |
| Not written yet | `Unknown` | `WaitingForKEDA` |

The ScaledObject is one of the objects the operator watches, so when KEDA updates its status, the
operator reconciles and the condition follows.

## Part 6. Weights that were never loading

### 6.1 What the scheduler said

The `qwen-small` pod was `Pending`. The **scheduler**, the component that decides which node runs
each pod, had marked it:

```
PodScheduled=False  Unschedulable: 0/3 nodes are available: request gpu: device class
gpu.nvidia.com does not exist.
```

The laptop's GPU is not visible inside kind, and no GPU driver was installed in the cluster. So no
node could ever satisfy the request for a `gpu.nvidia.com` device.

### 6.2 Why the operator said WeightsLoading

The operator saw no ready copies, and assumed the usual reason: a large model takes minutes to load
its weights, its numbers, into GPU memory. That is normal, so it reported `WeightsLoading`. Here,
nothing was loading and nothing ever would.

### 6.3 Why five minutes would have passed

A pod the scheduler cannot place does not change any object the operator watches. The Deployment
looks the same; no copy is ready either way. So the operator would not have looked again until its
regular five-minute recheck.

### 6.4 The fix

While no copy is ready, the operator lists the service's pods. If the scheduler has marked any of
them `Unschedulable`, the phase becomes `Unschedulable`, and `Ready` carries the scheduler's own
message. Otherwise the phase stays `Loading`. And while nothing is ready, the operator looks again
every 30 seconds instead of every five minutes.

The operator needed permission to list pods for this, so its role gained `get`, `list` and `watch`
on pods.

### 6.5 Testing what envtest cannot run

envtest has no scheduler, but it has an API server, and an API server accepts a pod's status from
anyone allowed to write it. The new test creates a pod with the service's labels and writes the
scheduler's verdict into its status by hand. The operator then has to report it. The KEDA test does
the same with the ScaledObject's status.

| Test | What it proves |
|---|---|
| `TestAutoscalingReportsKEDAsVerdict` | KEDA `Ready=False` gives `ScalerNotReady` with KEDA's reason; `Ready=True` gives `True` |
| `TestUnschedulablePodIsReportedNotLoading` | An unschedulable pod gives phase `Unschedulable` with the scheduler's message, and a 30-second recheck |
| `TestVLLMServiceCreatesEveryOwnedObject` | With no KEDA running, `Autoscaling` is `Unknown`, `WaitingForKEDA`, not `True` |

All three were run against the previous version of the operator, and all three failed there.

## Part 7. A warning on every apply

The operator's log showed two lines from KEDA on every reconcile:

```
PollingInterval is configured but is not relevant. PollingInterval is only relevant when
minReplicaCount = 0 ...
CooldownPeriod is configured but is not relevant. ...
```

Both fields only matter when scaling to and from zero copies. This operator always keeps at least
one copy running, so they did nothing. The scale-down delay they seemed to provide already lives in
the autoscaler's behaviour settings. With the 30-second recheck, the warnings would have appeared
every 30 seconds. The fields are gone, and a test checks they stay gone.

## Part 8. The fixed status, and a real request

### 8.1 After the fix

```
qwen-small   phase Unschedulable
  Admitted=True      PolicySatisfied: spec passes placement policy
  Autoscaling=False  ScalerNotReady: KEDA reports TriggerError: Triggers defined in ScaledObject
                     are not working correctly
  Routing=False      InferencePoolNotInstalled: the InferencePool CRD is not installed
  Ready=False        Unschedulable: 0/3 nodes are available: request gpu: device class
                     gpu.nvidia.com does not exist. ...

phi4-mini    phase Ready
  Autoscaling=False  BackendNotSupported: Ollama exports no queue metrics; replicas stay at minReplicas
  Ready=True         MinimumReplicasAvailable: serving traffic
```

*Figure 2. The status after the fix. Every `False` now names the component that said so and why.*

### 8.2 A pod that served

`phi4-mini` needs no GPU, so it could run. Its pod pulled the Ollama image, started, and passed its
probes, and the operator moved it to `Ready`. A chat request sent to the operator's Service,
asking the model to "Reply with exactly: OK", came back:

```
OK   {'prompt_tokens': 8, 'completion_tokens': 2, 'total_tokens': 10}
```

That was the first request ever served by a pod this operator created.

## Part 9. Design principles

**1. Report what happened, not what you did.** Storing an object is not the same as it working.
Read back the verdict of the component that does the work.

**2. Quote the component that knows.** KEDA's reason and the scheduler's message are better than any
summary the operator could write.

**3. Look again sooner when something is wrong.** A 30-second recheck while nothing is ready; five
minutes once things are healthy.

**4. A passing suite has edges.** envtest runs no scheduler and no KEDA. Run the operator on a real
cluster before believing any status that depends on them.

**5. Remove settings that do nothing.** They draw warnings, and readers assume they matter.

## Part 10. What is unresolved

1. **No vLLM pod has run under the operator.** That needs a cluster with a GPU visible to pods and
   the NVIDIA DRA driver installed. The laptop's GPU is not visible inside kind.
2. **Prometheus is a cluster decision.** The operator reports that KEDA cannot reach Prometheus. It
   cannot install one, and should not. The address is the `--prometheus-address` start-up flag.
3. **Crashes after scheduling.** A pod that is placed and then crashes while loading shows `Loading`
   until its startup probe gives up. Surfacing the container's own last error would close that.
4. **The image is built by hand.** `make deploy` built it here. CI does not build it.

## Glossary

| Term | Plain meaning | Explained in |
|---|---|---|
| API server | The Kubernetes component that stores every object and checks it on the way in | Part 3.1 |
| Condition | A named statement in status, `True`, `False` or `Unknown`, with a reason | Part 2.2 |
| CRD (CustomResourceDefinition) | The definition of a new kind of Kubernetes object | Part 1.1 |
| Declarative | Describing the state you want and letting programs make it true | Part 1.1 |
| Deployment | The Kubernetes object that runs and replaces copies of a program | Part 1.2 |
| envtest | A test harness that starts a real API server and etcd, and nothing else | Part 3.1 |
| etcd | The database that holds every Kubernetes object | Part 3.1 |
| InferenceService | This repository's object: a description of one model to serve | Part 1.1 |
| KEDA | The Kubernetes Event-driven Autoscaler, which reads metrics and sets the number of copies | Part 1.2 |
| kind | A tool that runs a whole Kubernetes cluster inside Docker containers | A note on accuracy |
| Kubelet | The agent on each node that starts containers | Part 3.2 |
| Operator | A program that watches objects of a kind and makes the cluster match them | Part 1.2 |
| Phase | A one-word summary of an object's state | Part 2.1 |
| Prometheus | The monitoring system KEDA reads metrics from | Part 5.1 |
| Reason | The short machine-readable word in a condition, such as `ScalerNotReady` | Part 2.2 |
| Reconcile | One pass of the operator: compare, fix, report | Part 1.2 |
| ResourceClaimTemplate | A request for a device by description rather than by count | Part 1.2 |
| ScaledObject | KEDA's object: what to measure and when to add copies | Part 1.2 |
| Scheduler | The component that decides which node runs each pod | Part 6.1 |
| Service | A stable address that spreads requests across copies | Part 1.2 |
| Status | The report an operator writes on the object it manages | Part 1.2 |
| Unschedulable | The scheduler's verdict that no node can take a pod | Part 6.1 |
