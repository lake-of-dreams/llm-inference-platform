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
#   Headlamp        a web UI for the cluster's own objects
#
# Chart versions are pinned to the ones this was last run and checked with.
#
# Needs helm and a cluster made by `make cluster-gpu`. Safe to run again.
set -euo pipefail
cd "$(dirname "$0")"
NS=monitoring

# Chart downloads from GitHub's release store fail now and then. Try a few times.
helm() {
  local n
  for n in 1 2 3 4; do
    command helm "$@" && return 0
    echo "helm $1 failed (attempt $n of 4), retrying" >&2; sleep 5
  done
  return 1
}

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null
helm repo add grafana https://grafana.github.io/helm-charts >/dev/null
helm repo add gpu-helm-charts https://nvidia.github.io/dcgm-exporter/helm-charts >/dev/null
helm repo add headlamp https://kubernetes-sigs.github.io/headlamp/ >/dev/null
helm repo update prometheus-community grafana gpu-helm-charts headlamp >/dev/null

helm upgrade -i prometheus prometheus-community/prometheus -n "$NS" --create-namespace \
  --version 29.47.2 -f prometheus-values.yaml --wait --timeout 6m
helm upgrade -i dcgm-exporter gpu-helm-charts/dcgm-exporter -n "$NS" --version 4.9.0 -f dcgm-values.yaml
helm upgrade -i tempo grafana/tempo -n "$NS" --version 1.24.4 -f tempo-values.yaml --wait --timeout 6m
helm upgrade -i headlamp headlamp/headlamp -n headlamp --create-namespace --version 0.46.0

python3 make_dashboard.py
kubectl -n "$NS" create configmap dashboard-activity --from-file=dashboard-activity.json \
  --dry-run=client -o yaml | kubectl label --local -f - grafana_dashboard=1 -o yaml | kubectl apply -f -
helm upgrade -i grafana grafana/grafana -n "$NS" --version 10.5.15 -f grafana-values.yaml --wait --timeout 6m

# Ollama publishes no metrics. A small proxy in front of it counts tokens.
kubectl create namespace llm-platform --dry-run=client -o yaml | kubectl apply -f -
kubectl -n llm-platform create configmap token-meter --from-file=meter.py=token-meter/meter.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f token-meter/deploy.yaml

# Tell the operator where traces go. Every vLLM service restarts once to pick
# the flag up (ADR-0014).
# An operator installed before this ran is switched over here. One installed
# afterwards gets the flag from `make deploy`, which sees Tempo and applies the
# same directory.
if kubectl -n inference-system get deploy inference-operator >/dev/null 2>&1; then
  kubectl apply -k ../../config/with-tracing
fi

cat <<EOF

Installed. To look, after \`make routes\`:
  http://grafana.172.18.255.200.nip.io   ->  dashboard "GPU workload activity"
  http://headlamp.172.18.255.200.nip.io  ->  sign in with: kubectl -n headlamp create token headlamp
or without routes:
  kubectl -n $NS port-forward svc/grafana 3001:80
  Explore -> Tempo for traces, by service name (the InferenceService's name)
EOF
