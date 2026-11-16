#!/usr/bin/env bash
# Installs what it takes to watch a GPU workload on the kind cluster: metrics
# from the GPU, the model server, every pod and the node; request traces; and
# one Grafana dashboard that puts them on a single page.
#
#   Prometheus      collects metrics every five seconds, at the address the
#                   operator and KEDA already expect
#   dcgm-exporter   NVIDIA's GPU metrics: utilisation, Tensor Core activity,
#                   memory, power, temperature
#   node-exporter, kube-state-metrics, and the kubelet's cAdvisor
#                   node and pod CPU, memory and network
#   token meter     counts tokens for Ollama, which publishes no metrics itself
#   Tempo           stores the traces vLLM sends
#   Grafana         dashboards; no sign-in, for a laptop cluster only
#
# Needs helm and a cluster made by `make cluster-gpu`. Safe to run again.
set -euo pipefail
cd "$(dirname "$0")"
NS=monitoring

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null
helm repo add grafana https://grafana.github.io/helm-charts >/dev/null
helm repo add gpu-helm-charts https://nvidia.github.io/dcgm-exporter/helm-charts >/dev/null
helm repo update prometheus-community grafana gpu-helm-charts >/dev/null

helm upgrade -i prometheus prometheus-community/prometheus -n "$NS" --create-namespace \
  -f prometheus-values.yaml --wait --timeout 6m
helm upgrade -i dcgm-exporter gpu-helm-charts/dcgm-exporter -n "$NS" -f dcgm-values.yaml
helm upgrade -i tempo grafana/tempo -n "$NS" -f tempo-values.yaml --wait --timeout 6m

python3 make_dashboard.py
kubectl -n "$NS" create configmap dashboard-activity --from-file=dashboard-activity.json \
  --dry-run=client -o yaml | kubectl label --local -f - grafana_dashboard=1 -o yaml | kubectl apply -f -
helm upgrade -i grafana grafana/grafana -n "$NS" -f grafana-values.yaml --wait --timeout 6m

# Ollama publishes no metrics. A small proxy in front of it counts tokens.
kubectl -n llm-platform create configmap token-meter --from-file=meter.py=token-meter/meter.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f token-meter/deploy.yaml

# Tell the operator where traces go. Every vLLM service restarts once to pick
# the flag up (ADR-0014).
ENDPOINT="--otlp-traces-endpoint=http://tempo.$NS.svc.cluster.local:4317"
if ! kubectl -n inference-system get deploy inference-operator \
     -o jsonpath='{.spec.template.spec.containers[0].args}' | grep -q -- "$ENDPOINT"; then
  kubectl -n inference-system patch deploy inference-operator --type=json \
    -p="[{\"op\":\"add\",\"path\":\"/spec/template/spec/containers/0/args/-\",\"value\":\"$ENDPOINT\"}]"
fi

cat <<EOF

Installed. To look:
  kubectl -n $NS port-forward svc/grafana 3001:80
  open http://localhost:3001  ->  dashboard "GPU workload activity"
  Explore -> Tempo for traces, by service name (the InferenceService's name)
EOF
