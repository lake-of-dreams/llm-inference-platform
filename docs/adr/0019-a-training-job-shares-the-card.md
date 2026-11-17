# ADR-0019: A training job runs on the shared card, and caps its own memory

**Status:** Accepted

## Context
Everything run on the GPU so far was inference. The dashboard was built to show any workload, and
the README said so, but no training job had been run to check it.

The card is shared through one claim (ADR-0017). Nothing divides its memory. A training job that
grows past what is free would not fail alone: whichever process next asks for memory fails, and
that may be a model server answering a request.

## Decision
* `hack/train` holds a plain Kubernetes Job that names the shared claim. The operator is not
  involved. It serves models and has no opinion on training.
* The job trains a 10.9-million-parameter transformer from scratch to predict the next byte of the
  Python standard library's source, which is already in the image. It needs no download.
* The job caps itself with `torch.cuda.set_per_process_memory_fraction`. Past its share of the card
  PyTorch raises an out-of-memory error inside the job.
* The job publishes its loss, tokens per second and memory in the Prometheus text format, and the
  dashboard has a row for them.
* The image is the vLLM one. It is already on the node and holds PyTorch built for the card's CUDA
  version.

## Consequences
* On the cluster, 1,500 steps took about two minutes beside both model servers. Held-out loss fell
  from 2.487 to 1.290, where guessing a byte at random scores 5.545. The sample it then wrote looks
  like Python and is not.
* The cap did its job on the first run. At a quarter of the card with batches of 32, the job
  stopped with PyTorch's own message, "934.52 MiB allowed". vLLM kept serving. The job now takes a
  third of the card with batches of 24 and peaks at 758 MiB.
* Taking turns is visible. The job trained on 84,000 tokens a second alone, and on 40,700 during
  the hundred steps in which vLLM answered a 150-token request. That request took 3.5 seconds; the
  same request took 3.2 with the card otherwise idle.
* `make train` runs it and follows its log.

## Open questions
* The cap is the job's own good behaviour. A job that does not set it is not stopped by anything.
* There is no queue for training jobs. Two started together both get the card. Kueue is the usual
  answer and is not installed.
* The checkpoint is written to the pod's scratch volume and is gone when the pod is.
