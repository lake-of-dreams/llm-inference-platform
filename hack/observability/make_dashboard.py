#!/usr/bin/env python3
"""Writes dashboard-activity.json: one Grafana page for what a GPU workload is doing.

GPU, model server, pod and node panels share one time axis, so a burst of
requests can be read downwards: tokens per second rise, Tensor Core activity
and power follow, the pod's network and memory move with them.

Metric names are those of dcgm-exporter 4.8 (…_RATIO, …_BYTES, …_WATTS) and
vLLM 0.33. Run this again after changing a panel; the JSON is generated.
"""
from __future__ import annotations

import json
from pathlib import Path

DS = {"type": "prometheus", "uid": "prometheus"}
NS = 'namespace=~"$namespace"'
panels: list[dict] = []
_id = 0
_y = 0


def row(title: str) -> None:
    global _id, _y
    _id += 1
    panels.append({"id": _id, "type": "row", "title": title, "collapsed": False,
                   "gridPos": {"h": 1, "w": 24, "x": 0, "y": _y}})
    _y += 1


def graph(title: str, exprs: list[tuple[str, str]], unit: str, x: int, w: int = 8, h: int = 7,
          desc: str = "", max_: float | None = None) -> None:
    global _id
    _id += 1
    defaults: dict = {"unit": unit, "custom": {"lineWidth": 2, "fillOpacity": 10,
                                               "showPoints": "never", "spanNulls": True}}
    if max_ is not None:
        defaults.update({"min": 0, "max": max_})
    panels.append({
        "id": _id, "type": "timeseries", "title": title, "description": desc, "datasource": DS,
        "gridPos": {"h": h, "w": w, "x": x, "y": _y},
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom"},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
        "targets": [{"datasource": DS, "expr": e, "legendFormat": legend, "refId": chr(65 + i)}
                    for i, (e, legend) in enumerate(exprs)]})


def stat(title: str, expr: str, unit: str, x: int, w: int = 4, desc: str = "") -> None:
    global _id
    _id += 1
    panels.append({
        "id": _id, "type": "stat", "title": title, "description": desc, "datasource": DS,
        "gridPos": {"h": 4, "w": w, "x": x, "y": _y},
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value",
                    "graphMode": "area"},
        "targets": [{"datasource": DS, "expr": expr, "refId": "A"}]})


def traces(title: str, traceql: str, x: int) -> None:
    """A table of traces from Tempo. vLLM sends one per request and one per start-up."""
    global _id
    _id += 1
    tempo = {"type": "tempo", "uid": "tempo"}
    panels.append({
        "id": _id, "type": "table", "title": title, "datasource": tempo,
        "gridPos": {"h": 8, "w": 12, "x": x, "y": _y},
        "targets": [{"datasource": tempo, "queryType": "traceql", "query": traceql,
                     "limit": 20, "tableType": "traces", "refId": "A"}]})


def next_line(h: int) -> None:
    global _y
    _y += h


# ---------------------------------------------------------------- at a glance
row("At a glance")
stat("GPU busy", "avg(DCGM_FI_DEV_GPU_UTIL_RATIO)", "percentunit", 0,
     desc="Share of time some kernel was running on the GPU. Says the GPU is doing something, not that it is doing useful arithmetic.")
stat("Tensor Core activity", "avg(DCGM_FI_PROF_TENSOR_UTIL_RATIO)", "percentunit", 4,
     desc="Share of time the Tensor Cores were active. The honest measure of useful model work.")
stat("GPU memory used", "sum(DCGM_FI_DEV_FB_USED_BYTES)", "bytes", 8)
stat("Power", "sum(DCGM_FI_DEV_BOARD_POWER_WATTS)", "watt", 12)
stat("Tokens generated per second", 'sum(rate(vllm:generation_tokens_total[1m]))', "short", 16)
stat("Requests running / waiting",
     'sum(vllm:num_requests_running) + sum(vllm:num_requests_waiting)', "short", 20)
next_line(4)

# ---------------------------------------------------------------- GPU
row("GPU: what the card is doing")
graph("Utilisation", [("DCGM_FI_DEV_GPU_UTIL_RATIO", "GPU busy"),
                      ("DCGM_FI_PROF_TENSOR_UTIL_RATIO", "Tensor Cores"),
                      ("DCGM_FI_PROF_GR_ENGINE_UTIL_RATIO", "graphics engine"),
                      ("DCGM_FI_PROF_DRAM_UTIL_RATIO", "memory bandwidth")],
      "percentunit", 0, max_=1,
      desc="Decode is limited by memory bandwidth, so that line can be high while Tensor Core activity is low.")
graph("Memory", [("DCGM_FI_DEV_FB_USED_BYTES", "used"),
                 ("DCGM_FI_DEV_FB_CAPACITY_BYTES", "capacity"),
                 ("DCGM_FI_DEV_FB_RESERVED_BYTES", "reserved by the driver")], "bytes", 8,
      desc="vLLM claims its share up front, so this is flat while a model is loaded. A step up is a model loading.")
graph("Power and temperature", [("DCGM_FI_DEV_BOARD_POWER_WATTS", "power, W"),
                                ("DCGM_FI_DEV_GPU_TEMP_CELSIUS", "temperature, °C")], "short", 16)
next_line(7)
graph("Data moved between CPU and GPU (PCIe)",
      [("rate(DCGM_FI_PROF_PCIE_RX_BYTES[1m])", "to the GPU"),
       ("rate(DCGM_FI_PROF_PCIE_TX_BYTES[1m])", "from the GPU")], "Bps", 0,
      desc="Large while weights load or a training job feeds batches; small during inference, where tokens are tiny.")
graph("Clocks", [("DCGM_FI_DEV_SM_CLOCK_HERTZ", "processor clock"),
                 ("DCGM_FI_DEV_MEMORY_CLOCK_HERTZ", "memory clock")], "hertz", 8,
      desc="A clock that drops under load means the card is throttling for heat or power.")
graph("Energy used", [("increase(DCGM_FI_DEV_GPU_ENERGY_JOULES_TOTAL[5m])", "joules per 5 minutes")],
      "joule", 16)
next_line(7)

# ---------------------------------------------------------------- model server
row("Model server (vLLM)")
graph("Requests", [(f"sum by (model_name) (vllm:num_requests_running{{{NS}}})", "running {{model_name}}"),
                   (f"sum by (model_name) (vllm:num_requests_waiting{{{NS}}})", "waiting {{model_name}}")],
      "short", 0, desc="Waiting is the signal the autoscaler scales on.")
graph("Tokens per second", [(f"sum by (model_name) (rate(vllm:generation_tokens_total{{{NS}}}[1m]))", "generated {{model_name}}"),
                            (f"sum by (model_name) (rate(vllm:prompt_tokens_total{{{NS}}}[1m]))", "prompt {{model_name}}")],
      "short", 8)
graph("Time to first token", [
    (f"histogram_quantile(0.5, sum by (le) (rate(vllm:time_to_first_token_seconds_bucket{{{NS}}}[2m])))", "p50"),
    (f"histogram_quantile(0.95, sum by (le) (rate(vllm:time_to_first_token_seconds_bucket{{{NS}}}[2m])))", "p95")],
      "s", 16)
next_line(7)
graph("Time between tokens", [
    (f"histogram_quantile(0.5, sum by (le) (rate(vllm:inter_token_latency_seconds_bucket{{{NS}}}[2m])))", "p50"),
    (f"histogram_quantile(0.95, sum by (le) (rate(vllm:inter_token_latency_seconds_bucket{{{NS}}}[2m])))", "p95")],
      "s", 0)
graph("KV cache in use", [(f"avg by (model_name) (vllm:kv_cache_usage_perc{{{NS}}})", "{{model_name}}")],
      "percentunit", 8, max_=1,
      desc="The memory holding each conversation's context. Near 100% vLLM starts pausing requests.")
graph("Prefix cache hit rate", [
    (f"sum(rate(vllm:prefix_cache_hits_total{{{NS}}}[5m])) / sum(rate(vllm:prefix_cache_queries_total{{{NS}}}[5m]))", "hit rate")],
      "percentunit", 16, max_=1,
      desc="Share of prompt tokens served from already-computed prefixes.")
next_line(7)

graph("Where a request's time goes", [
    (f"sum(rate(vllm:request_queue_time_seconds_sum{{{NS}}}[1m])) / sum(rate(vllm:request_queue_time_seconds_count{{{NS}}}[1m]))", "waiting in the queue"),
    (f"sum(rate(vllm:request_prefill_time_seconds_sum{{{NS}}}[1m])) / sum(rate(vllm:request_prefill_time_seconds_count{{{NS}}}[1m]))", "reading the prompt (prefill)"),
    (f"sum(rate(vllm:request_decode_time_seconds_sum{{{NS}}}[1m])) / sum(rate(vllm:request_decode_time_seconds_count{{{NS}}}[1m]))", "writing the answer (decode)")],
      "s", 0, w=12,
      desc="Mean seconds per request in each phase. Queue time rising first is the sign of saturation.")
graph("Tokens per request", [
    (f"sum(rate(vllm:request_prompt_tokens_sum{{{NS}}}[1m])) / sum(rate(vllm:request_prompt_tokens_count{{{NS}}}[1m]))", "prompt tokens read"),
    (f"sum(rate(vllm:request_generation_tokens_sum{{{NS}}}[1m])) / sum(rate(vllm:request_generation_tokens_count{{{NS}}}[1m]))", "tokens written"),
    (f"sum(rate(vllm:prompt_tokens_cached_total{{{NS}}}[1m])) / sum(rate(vllm:request_prompt_tokens_count{{{NS}}}[1m]))", "prompt tokens served from cache")],
      "short", 12, w=12)
next_line(7)
traces("Latest requests (click a row for its trace)",
       '{name="llm_request"} | select(span.gen_ai.usage.prompt_tokens, span.gen_ai.usage.completion_tokens, span.gen_ai.latency.time_to_first_token, span.gen_ai.latency.time_in_queue)', 0)
traces("Model start-ups (each stage of loading as a span)", '{name="Overall Loading"}', 12)
next_line(8)

# ---------------------------------------------------------------- pods
row("Pods: memory, CPU and network")
POD = f'{NS},pod=~"$pod",container!="",container!="POD"'
graph("Memory in use, by pod", [(f"sum by (pod) (container_memory_working_set_bytes{{{POD}}})", "{{pod}}")],
      "bytes", 0, desc="CPU-side memory. GPU memory is in the GPU row.")
graph("CPU, by pod", [(f"sum by (pod) (rate(container_cpu_usage_seconds_total{{{POD}}}[2m]))", "{{pod}}")],
      "short", 8, desc="Cores in use.")
graph("Restarts and ready replicas", [
    (f"sum by (deployment) (kube_deployment_status_replicas_ready{{{NS}}})", "ready {{deployment}}"),
    (f"sum by (pod) (kube_pod_container_status_restarts_total{{{NS}}})", "restarts {{pod}}")], "short", 16)
next_line(7)
NET = f'{NS},pod=~"$pod"'
graph("Network received, by pod", [(f"sum by (pod) (rate(container_network_receive_bytes_total{{{NET}}}[2m]))", "{{pod}}")],
      "Bps", 0, desc="A model download shows here as a long plateau; requests as small bumps.")
graph("Network sent, by pod", [(f"sum by (pod) (rate(container_network_transmit_bytes_total{{{NET}}}[2m]))", "{{pod}}")],
      "Bps", 8)
graph("Dropped packets and errors, by pod", [
    (f"sum by (pod) (rate(container_network_receive_packets_dropped_total{{{NET}}}[5m]))", "rx dropped {{pod}}"),
    (f"sum by (pod) (rate(container_network_transmit_errors_total{{{NET}}}[5m]))", "tx errors {{pod}}")], "pps", 16)
next_line(7)

# ---------------------------------------------------------------- node
row("Node")
graph("Memory", [("node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes", "used {{instance}}"),
                 ("node_memory_Cached_bytes", "page cache {{instance}}")], "bytes", 0,
      desc="kind nodes are containers on one machine, so both nodes report the laptop's memory.")
graph("Network, by interface", [
    ('sum by (device) (rate(node_network_receive_bytes_total{device!~"lo|veth.*"}[2m]))', "in {{device}}"),
    ('sum by (device) (rate(node_network_transmit_bytes_total{device!~"lo|veth.*"}[2m]))', "out {{device}}")], "Bps", 8)
graph("Disk", [("sum(rate(node_disk_read_bytes_total[2m]))", "read"),
               ("sum(rate(node_disk_written_bytes_total[2m]))", "written")], "Bps", 16,
      desc="Weights loading from disk and checkpoints being written show here.")
next_line(7)

dashboard = {
    "uid": "gpu-workload-activity", "title": "GPU workload activity",
    "tags": ["gpu", "inference", "training"], "schemaVersion": 39, "version": 1,
    "refresh": "5s", "time": {"from": "now-15m", "to": "now"}, "timezone": "browser",
    "templating": {"list": [
        {"name": "namespace", "label": "Namespace", "type": "query", "datasource": DS,
         "query": "label_values(kube_pod_info, namespace)", "refresh": 2, "includeAll": True,
         "multi": True, "allValue": ".*", "current": {"text": "llm-platform", "value": "llm-platform"}},
        {"name": "pod", "label": "Pod", "type": "query", "datasource": DS,
         "query": 'label_values(kube_pod_info{namespace=~"$namespace"}, pod)', "refresh": 2,
         "includeAll": True, "multi": True, "allValue": ".*",
         "current": {"text": "All", "value": "$__all"}}]},
    "panels": panels,
}
out = Path(__file__).with_name("dashboard-activity.json")
out.write_text(json.dumps(dashboard, indent=1) + "\n")
print(f"{out.name}: {sum(p['type'] != 'row' for p in panels)} panels")
