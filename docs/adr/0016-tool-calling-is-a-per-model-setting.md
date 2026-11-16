# ADR-0016: Tool calling is switched on per model, by naming its parser

**Status:** Accepted

## Context
A chat window was pointed at the vLLM service. Its first message came back as an error from vLLM:
`"auto" tool choice requires --enable-auto-tool-choice and --tool-call-parser to be set`.

Chat clients offer the model tools, such as a search or a memory, and send `tool_choice: auto`,
meaning the model decides whether to call one. vLLM refuses such a request unless it was started
with tool calling on. Turning it on needs a parser, because each model family writes a tool call in
its own format and vLLM has to recognise it in the output. Qwen 2.5 uses the format vLLM calls
`hermes`. A wrong parser does not fail; it just never finds a tool call.

The operator started every vLLM server with tool calling off.

## Decision
* `spec.toolCallParser` is an optional string. When set, the vLLM container gets
  `--enable-auto-tool-choice` and `--tool-call-parser <value>`. Both or neither: vLLM will not
  start with one.
* There is no default. The right value depends on the model, and a wrong one fails silently.
* An Ollama service with the field set is refused at admission. Ollama decides tool support from
  the model's own template and has no such flag.

## Consequences
* One controller test: no parser, no flags; a parser, both flags; Ollama with a parser is refused
  by the API server.
* On the cluster, `qwen-small` with `toolCallParser: hermes` answered a weather question with a
  call to `get_weather` and the argument `{"city": "Leeds"}`.
* Setting the field restarts the model server. On a cluster with one GPU that means the weights
  reload (ADR-0015).

## Open questions
* The Ollama service accepted the same request and returned a tool call whose arguments were the
  tool's schema, not a city. The request shape works on both servers; whether a 3.8-billion-
  parameter model fills in arguments correctly is a property of the model, and was not measured
  beyond this one call.
* The operator does not check the parser name against the list vLLM supports. A misspelt name
  stops the server at start-up, which status reports as a crash loop.
