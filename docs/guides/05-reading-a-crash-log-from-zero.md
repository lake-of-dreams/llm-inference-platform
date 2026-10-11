# Guide 5. Reading a crash log, from zero

## How to read this guide

**Who it is for.** Anyone who has to work out why a model server will not start. You do not need to
know how vLLM or FlashInfer work inside. Each term is explained when it first appears.

**What you will know at the end.** How to read a long start-up log so that it tells you what
failed, rather than what you expected to fail. You will follow one real failure through two
diagnoses. The first was careful, ruled out the obvious cause, and was still wrong. The second
found the cause in the last line of the traceback, and the fix was one line in a shell script.

**How it is organised.**

1. Part 1 explains what happens when vLLM starts, in order.
2. Part 2 shows the failure as it first appeared.
3. Part 3 explains the first diagnosis, and the rule it used well: read forwards from the last
   success.
4. Part 4 explains where the first diagnosis stopped, and what it cost.
5. Part 5 explains the second diagnosis: read the traceback to its last line.
6. Part 6 explains the cause: a build tool that was installed but not findable.
7. Part 7 explains the fix and how it was proven.
8. Part 8 lists the design principles.
9. Part 9 lists what is unresolved.

A glossary at the end defines every term.

You can read it front to back like a short book. Each part builds on the one before it.

## The idea in one paragraph

A model server on a small laptop GPU kept dying at start-up with a message that pointed nowhere. On
a card with only 4 GB of memory, everyone's first guess is "out of memory". The log showed memory
was fine, and the investigation moved on to the part of the server that crashed: a speed-up library
that compiles some code the first time it runs. The conclusion was "that compile does not work on
this card", so the speed-up was switched off. Weeks later, running it again and reading the error
to the very end, the real message was plain: a program called `ninja`, which the compile needs,
could not be found. It was installed. It was just in a folder the server was not looking in. One
line fixed it, and the speed-up works on this card after all.

## A note on accuracy

The log lines quoted here are copied from real runs of vLLM v0.29.0 on an NVIDIA RTX 500 Ada laptop
GPU with 4 GB of memory, compute capability 8.9, driver 595.91, with FlashInfer 0.6.18. The decision
record is ADR-0003, which now records both diagnoses. The script is `hack/serve-vllm.sh`.

Simplified for teaching: the start-up sequence in Part 1 lists only the steps that matter here.

## The running example

Fernhill Legal runs its payroll model on one office server with a 4 GB GPU, using vLLM. The
engineer setting it up types the start command, waits, and gets an error. This guide follows what
they see and how they read it.

---

## Part 1. What happens when vLLM starts

vLLM does several things before it can answer a request. They happen in order, and each one prints
a line to the log when it finishes.

1. **Start the engine process.** vLLM runs the model in a separate process, the **engine core**.
2. **Load the weights.** The model's numbers are read from disk into GPU memory. The log says how
   long that took and how much memory it used.
3. **Measure free memory.** vLLM checks how much GPU memory is left.
4. **Allocate the KV cache.** It reserves most of what is left for the model's working notes, the
   **KV cache** (Guide 2, Part 1), and logs how much it got.
5. **Warm up.** It runs a few dummy requests through the model so every piece of GPU code is ready.
   Some pieces are compiled at this point, the first time they are used.
6. **Report ready.** The web server starts answering health checks.

Keep this order in mind. Where a log stops in this sequence says a great deal about what failed.

## Part 2. The failure as it first appeared

The engineer starts vLLM. After about a minute, the last lines of the log say:

```
RuntimeError: Engine core initialization failed. See root cause above.
Failed core proc(s): {}
```

*Figure 1. The end of the log. It says the engine failed, and points "above" without naming anything.*

This is the error everyone sees first, because it is at the bottom. It is also the least useful
line in the log. It is the outer process reporting that the inner one died. It does not say why.

## Part 3. The first diagnosis: read forwards from the last success

### 3.1 The tempting guess

The card has 4 GB of memory. Model servers run out of memory constantly. So the first guess is
memory, and the first instinct is to try smaller settings.

### 3.2 What the log had already said

Scrolling up from the error, the log shows this, in order:

```
Loading weights took 0.27 seconds
Model loading took 0.93 GiB memory and 3.3 seconds
Available KV cache memory: 1.97 GiB
GPU KV cache size: 172,272 tokens
```

*Figure 2. The last lines that report success. Look at the KV cache line: it is healthy.*

Map those lines to Part 1. Step 2, the weights, finished. Steps 3 and 4, measuring memory and
allocating the KV cache, finished, with almost 2 GiB of room for 172,272 tokens. The failure
therefore came after step 4. Running out of memory while loading would have stopped the log at
step 2 or 3.

This is the rule the first diagnosis used well: **read forwards from the last success.** The last
line that says something worked tells you where the failure is not. Here, it ruled out memory.

### 3.3 Where it landed

Between the KV cache line and the final error is a **traceback**: the list of function calls that
were running when the error happened, from the outermost to the innermost. It showed the engine was
in its warm-up step, inside a function called `top_k_top_p_sampling_from_logits`, in a library
called **FlashInfer**.

FlashInfer provides fast GPU code for parts of the model's work, including **sampling**: picking the
next word from the model's scores. It compiles some of that code the first time it is used, which
is called **just-in-time (JIT) compilation**. So step 5 of Part 1 was compiling FlashInfer's
sampling code, and something went wrong there.

The first diagnosis concluded: FlashInfer's compile fails on this combination of card, driver and
toolkit. The fix was to tell vLLM not to use FlashInfer for sampling, with
`VLLM_USE_FLASHINFER_SAMPLER=0`. vLLM then used a plainer sampler, started, and worked.

## Part 4. Where the first diagnosis stopped

The first diagnosis was right about the location and wrong about the cause. It found the frame that
crashed, and inferred a reason that sounded plausible for a laptop GPU. It did not read the
traceback to its last line, which is where the reason is written.

That had three costs.

1. **A wrong fact was recorded.** ADR-0003 said FlashInfer's compile does not work on this card.
   Anyone reading it would avoid FlashInfer on similar hardware for no reason.
2. **A working feature was switched off.** The faster sampler was disabled everywhere the script
   ran.
3. **The real fault stayed.** Whatever broke the compile was still there, waiting for the next
   thing that needed to compile.

## Part 5. The second diagnosis: read the traceback to the end

The question in ADR-0003, whether newer vLLM still fails on this card, was tested by running vLLM
again with the FlashInfer sampler on. It failed exactly as before, with the same healthy KV cache
line. This time the whole traceback was read, down to the innermost call:

```
File ".../flashinfer/sampling.py", in top_k_top_p_sampling_from_logits
File ".../flashinfer/sampling.py", in get_sampling_module
File ".../flashinfer/jit/core.py", in build_and_load
File ".../flashinfer/jit/core.py", in build
File ".../flashinfer/jit/cpp_ext.py", in run_ninja
FileNotFoundError: [Errno 2] No such file or directory: 'ninja'
```

*Figure 3. The bottom of the traceback. Look at the last line: it names exactly what was missing.*

Read it from the top down. Sampling asked for the sampling module. The module was not built yet, so
FlashInfer started to build it. To build, it ran a program called `ninja`. That program could not
be found.

The rule here: **a traceback's last line is the cause; the lines above it are the route there.**
The first diagnosis read the route and guessed the cause.

## Part 6. The cause: installed, but not findable

### 6.1 What ninja is

**`ninja`** is a small build tool: it runs compile commands quickly, in the right order. FlashInfer
uses it for its JIT compile.

### 6.2 It was installed

Checking showed `ninja` was installed, by pip, in the same Python environment as vLLM:
`.venv/bin/ninja`. A **virtual environment**, or venv, is a self-contained folder of Python packages
and the programs they bring, kept apart from the rest of the machine.

### 6.3 Why it was not found

When a program runs another program by name, such as `ninja`, the operating system looks for it in
a list of folders called the **PATH**. A venv's `bin` folder is added to the PATH when you
**activate** the venv.

`hack/serve-vllm.sh` did not activate the venv. It ran vLLM by its full path,
`.venv/bin/vllm`, which works for vLLM itself. But everything vLLM then ran by name was looked up in
the normal PATH, which did not include `.venv/bin`. So FlashInfer asked for `ninja`, the system
looked in the usual folders, and it was not there.

**Example.** Typing `.venv/bin/vllm serve ...` starts vLLM. Inside it, FlashInfer runs `ninja`. The
system searches `/usr/local/bin`, `/usr/bin` and the other usual folders. It never looks in
`.venv/bin`, where `ninja` actually is. The compile fails before it begins.

This kind of fault is common and confusing, because everything looks installed. The package is
there, `pip show ninja` finds it, and the error says "No such file".

## Part 7. The fix, and how it was proven

### 7.1 The fix

`hack/serve-vllm.sh` now adds the folder holding `vllm` to the PATH before starting it. Anything
installed beside vLLM is then found. The sampler is no longer switched off by default.

The script also checks for `ninja` before starting. If FlashInfer's sampler is on and `ninja`
cannot be found, it stops at once with a message saying what to install, instead of failing a
minute later inside a traceback.

### 7.2 How it was proven, step by step

1. vLLM was started with the sampler on and the original script: it failed with the `ninja` error.
2. vLLM was started with the sampler on and `.venv/bin` added to the PATH by hand: the log showed
   "Using FlashInfer for top-p & top-k sampling", the compile ran for about 1 minute 40 seconds,
   and the server came up.
3. `hack/verify.py` ran its full live check against that server. Every check passed.
4. Throughput was measured the same way as in Guide 1, Part 7: a median of 436 tokens a second,
   against 421 with the plainer sampler. The difference is within the spread between bursts, so on
   this small model the faster sampler makes no measurable difference, and switching it off had not
   cost anything either.
5. The fixed script was started with no overrides at all, as anyone would run it, to confirm the
   default works.

Step 4 matters for how to read the old fix. Switching the sampler off was harmless here. The harm
was in the wrong explanation, which would have misled anyone deciding about this card later.

## Part 8. Design principles

**1. The bottom error is the least informative.** "Engine core initialization failed" only says the
inner process died.

**2. Read forwards from the last success.** The last line that says something worked tells you
what did not fail. Here, it ruled out memory.

**3. Read the traceback to its last line.** The lines above are the route. The last line is the
cause.

**4. A plausible explanation is not a confirmed one.** "The compile fails on this card" fitted the
evidence, and was never checked.

**5. Fail early with the fix in the message.** A missing build tool should stop the script in one
line at start, naming what to install.

**6. Retest old conclusions when the question comes up again.** The open question in ADR-0003 is
what got this run, and the run is what found the cause.

## Part 9. What is unresolved

1. **Inside the container image.** The operator runs vLLM from the `vllm/vllm-openai` image. Whether
   `ninja` is on the PATH there has not been checked. If it is not, pods would fail the same way.
2. **The first compile delays readiness.** The compile took about 1 minute 40 seconds on this card.
   It happened before the server reported ready, so the startup probe covered it here (ADR-0002).
   A larger model or a slower machine would need that time in the probe's budget.
3. **The compiled code is not kept between runs here.** FlashInfer can cache what it compiles. In a
   container that starts fresh each time, every start pays the compile again unless the cache folder
   is kept on a volume.

## Glossary

| Term | Plain meaning | Explained in |
|---|---|---|
| Activate (a venv) | Add a virtual environment's `bin` folder to the PATH for the current shell | Part 6.3 |
| Engine core | The separate process inside vLLM that runs the model | Part 1 |
| FlashInfer | A library of fast GPU code that vLLM can use, including for sampling | Part 3.3 |
| Just-in-time (JIT) compilation | Compiling a piece of code the first time it is needed, instead of in advance | Part 3.3 |
| KV cache | The model's working notes on the text it has read, held in GPU memory | Part 1 |
| `ninja` | A small build tool that runs compile commands quickly, in order | Part 6.1 |
| PATH | The list of folders the system searches when a program is run by name | Part 6.3 |
| Sampling | Picking the next word from the model's scores | Part 3.3 |
| Traceback | The list of function calls running when an error happened, outermost first | Part 3.3 |
| Virtual environment (venv) | A self-contained folder of Python packages and their programs | Part 6.2 |
| Warm-up | Running dummy requests at start so every piece of GPU code is ready | Part 1 |
