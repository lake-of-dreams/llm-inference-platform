"""Optional tracing for the gateway, in OpenTelemetry's GenAI attribute names.

Off unless the process that runs the gateway has installed the OpenTelemetry API
and configured an exporter. Without the package this module hands out a span
that does nothing, so the gateway keeps its no-dependency install.

The span carries what a person debugging a slow or misrouted request needs: the
model, the token counts, which backend was chosen and why, whose request it
was. It never carries the prompt or the answer. A trace store is usually open
to more people than the data in a Restricted prompt is.

The gen_ai.* names are still marked Development by OpenTelemetry and may change
(ADR-0013). server.address, server.port and error.type are stable.
"""

from __future__ import annotations

from contextlib import contextmanager
from urllib.parse import urlsplit

try:
    from opentelemetry import trace
    from opentelemetry.trace import SpanKind, Status, StatusCode
except ImportError:  # the gateway runs without it
    trace = None


class _NoSpan:
    def set_attribute(self, _key: str, _value) -> None:
        pass


@contextmanager
def chat_span(model: str, base_url: str, **attributes):
    """One span per gateway request, named as the conventions ask: `chat <model>`."""
    if trace is None:
        yield _NoSpan()
        return
    url = urlsplit(base_url)
    attrs = {"gen_ai.operation.name": "chat", "gen_ai.request.model": model,
             "server.address": url.hostname or ""}
    if url.port:
        attrs["server.port"] = url.port
    attrs.update({k: v for k, v in attributes.items() if v is not None})
    tracer = trace.get_tracer("model-gateway")
    with tracer.start_as_current_span(f"chat {model}", kind=SpanKind.CLIENT, attributes=attrs,
                                      record_exception=False, set_status_on_exception=False) as span:
        try:
            yield span
        except Exception as exc:
            span.set_attribute("error.type", type(exc).__name__)
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            raise
