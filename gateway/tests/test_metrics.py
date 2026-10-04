import json
import urllib.parse

from app.metrics import query_scalar, with_observed_ttft
from app.router import Backend, Classification

VLLM = Backend("vllm-qwen", "http://x/v1", "qwen-small", 2048, Classification.RESTRICTED, 520,
               ttft_p95_ms=800)
OLLAMA = Backend("ollama-phi4", "http://y/v1", "phi4-mini", 4096, Classification.INTERNAL, 0,
                 ttft_p95_ms=1500)


def prometheus(results_by_model):
    def handle(h):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(h.path).query)["query"][0]
        result = []
        for model, value in results_by_model.items():
            if f'model_name="{model}"' in q:
                result = [{"metric": {}, "value": [0, value]}]
        h.send_response(200)
        h.send_header("Content-Type", "application/json")
        h.end_headers()
        h.wfile.write(json.dumps({"status": "success",
                                  "data": {"resultType": "vector", "result": result}}).encode())
    return handle


def test_observed_p95_replaces_the_configured_one(http_server):
    url = http_server(prometheus({"qwen-small": "2350.7"}))
    vllm, ollama = with_observed_ttft([VLLM, OLLAMA], url)
    assert vllm.ttft_p95_ms == 2350
    # no vLLM metrics for Ollama, so it keeps its configured figure
    assert ollama.ttft_p95_ms == 1500


def test_idle_backend_keeps_configured_value_not_zero(http_server):
    url = http_server(prometheus({"qwen-small": "NaN"}))
    assert query_scalar(url, "anything") is None
    (vllm,) = with_observed_ttft([VLLM], url)
    assert vllm.ttft_p95_ms == 800
