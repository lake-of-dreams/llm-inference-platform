.PHONY: all build vet fmt generate manifests check-generated test test-go test-py lint \
	validate up cluster cluster-gpu gpu-driver addons observability routes chat load image deploy samples \
	samples-shared train flow-control priority demo verify clean

CONTROLLER_GEN_VERSION ?= v0.22.0
ENVTEST_K8S_VERSION    ?= 1.37.x
KUBECONFORM_VERSION    ?= v0.8.0
KIND_CLUSTER           ?= inference
IMG                    ?= inference-operator:dev

GOBIN          := $(shell go env GOPATH)/bin
CONTROLLER_GEN := $(GOBIN)/controller-gen
SETUP_ENVTEST  := $(GOBIN)/setup-envtest
KUBECONFORM    := $(GOBIN)/kubeconform
KUSTOMIZE      := $(GOBIN)/kustomize

all: generate manifests vet test lint validate

build:
	go build -o bin/operator ./cmd/operator

vet:
	go vet ./...

fmt:
	gofmt -l -w .

$(CONTROLLER_GEN):
	go install sigs.k8s.io/controller-tools/cmd/controller-gen@$(CONTROLLER_GEN_VERSION)

$(SETUP_ENVTEST):
	go install sigs.k8s.io/controller-runtime/tools/setup-envtest@release-0.25

$(KUSTOMIZE):
	go install sigs.k8s.io/kustomize/kustomize/v5@v5.8.2

$(KUBECONFORM):
	go install github.com/yannh/kubeconform/cmd/kubeconform@$(KUBECONFORM_VERSION)

# DeepCopy methods, generated from the Go types.
generate: $(CONTROLLER_GEN)
	$(CONTROLLER_GEN) object:headerFile=hack/boilerplate.go.txt paths=./api/...

# The CRD and the operator's ClusterRole, generated from markers in the Go code.
manifests: $(CONTROLLER_GEN)
	$(CONTROLLER_GEN) crd paths=./api/... output:crd:artifacts:config=config/crd/bases
	$(CONTROLLER_GEN) rbac:roleName=inference-operator paths=./internal/... output:rbac:artifacts:config=config/rbac

# Fails when the committed generated files differ from what the code produces.
check-generated: generate manifests
	git diff --exit-code -- api config

test: test-go test-py

# Starts a real kube-apiserver and etcd for the controller tests.
test-go: $(SETUP_ENVTEST)
	KUBEBUILDER_ASSETS="$$($(SETUP_ENVTEST) use $(ENVTEST_K8S_VERSION) -p path)" go test ./...

test-py:
	.venv/bin/pytest

lint:
	test -z "$$(gofmt -l .)"
	.venv/bin/ruff check .

# Schema-checks the rendered install manifests offline. kubeconform has no
# schema for the InferenceService samples; test-go checks those against the
# real CRD instead.
validate: $(KUBECONFORM) $(KUSTOMIZE)
	$(KUSTOMIZE) build config/default | \
		$(KUBECONFORM) -strict -summary -kubernetes-version 1.37.0 -ignore-missing-schemas -

# The whole laptop cluster from nothing, in the order the pieces depend on each
# other: a GPU node, the driver that publishes the GPU, the add-ons the
# operator talks to, the monitoring stack, the operator, the hostnames, the
# chat window, and both models on the one card. Every step is safe to run again.
up: cluster-gpu gpu-driver addons observability deploy routes chat samples-shared flow-control

cluster:
	kind create cluster --name $(KIND_CLUSTER)

# A kind cluster whose worker node is given the host's NVIDIA GPUs. Needs the
# NVIDIA Container Toolkit as Docker's default runtime, nvkind, and a kind new
# enough for the node image (v0.33 for Kubernetes 1.37).
cluster-gpu:
	nvkind cluster create --name $(KIND_CLUSTER) --image kindest/node:v1.37.0 \
		--config-template hack/kind-gpu.yaml --wait 180s

# The NVIDIA DRA driver publishes each GPU to the scheduler and installs the
# gpu.nvidia.com DeviceClass.
gpu-driver:
	kubectl label node -l '!node-role.kubernetes.io/control-plane' --overwrite nvidia.com/gpu.present=true
	helm upgrade -i dra-driver-nvidia-gpu oci://registry.k8s.io/dra-driver-nvidia/charts/dra-driver-nvidia-gpu \
		--version 0.5.1 --namespace dra-driver-nvidia-gpu --create-namespace \
		--set gpuResourcesEnabledOverride=true --set maskNvidiaDriverParams=true \
		--set resources.computeDomains.enabled=false --wait

# What the operator works with but does not install: KEDA, which scales on the
# vLLM metrics, and the two CRDs it writes routing and priority objects to.
addons:
	helm repo add kedacore https://kedacore.github.io/charts >/dev/null
	helm upgrade -i keda kedacore/keda -n keda --create-namespace --version 2.21.0 --wait
	kubectl apply --server-side -f https://raw.githubusercontent.com/kubernetes-sigs/gateway-api-inference-extension/v1.6.2/config/crd/bases/inference.networking.k8s.io_inferencepools.yaml
	kubectl apply --server-side -f https://raw.githubusercontent.com/llm-d/llm-d-router/v1.0.0/config/crd/bases/llm-d.ai_inferenceobjectives.yaml

# Prometheus, GPU and node metrics, Tempo for traces, a Grafana dashboard and
# the Headlamp cluster UI.
observability:
	hack/observability/install.sh

# A gateway with one fixed address, and a hostname for each UI and model API,
# so nothing needs a port-forward. MetalLB provides the address, Envoy Gateway
# serves it. Run after `make observability`.
routes:
	helm repo add metallb https://metallb.github.io/metallb >/dev/null
	helm upgrade -i metallb metallb/metallb -n metallb-system --create-namespace --version 0.16.2 --wait
	helm upgrade -i envoy-gateway oci://docker.io/envoyproxy/gateway-helm --version v1.10.0 \
		-n envoy-gateway-system --create-namespace --wait
	kubectl apply -f hack/observability/routes.yaml
	kubectl -n gateway wait gateway/local --for=condition=Programmed --timeout=180s

# A chat window for both models, at http://chat.172.18.255.200.nip.io.
# Run after `make routes`.
chat:
	kubectl apply -f hack/chat/open-webui.yaml
	kubectl -n chat rollout status deploy/open-webui --timeout=600s

# A ramp of requests at vLLM, to watch on the dashboard. URL says where vLLM is.
load:
	.venv/bin/python hack/load.py

image:
	docker build -t $(IMG) .

# With Tempo installed, the operator is deployed with the traces flag
# (config/with-tracing); without it, plain config/default.
deploy: image
	kind load docker-image $(IMG) --name $(KIND_CLUSTER)
	if kubectl -n monitoring get svc tempo >/dev/null 2>&1; then \
		kubectl apply -k config/with-tracing; else kubectl apply -k config/default; fi
	kubectl -n inference-system rollout restart deploy/inference-operator
	kubectl -n inference-system rollout status deploy/inference-operator --timeout=180s

samples:
	kubectl create namespace llm-platform --dry-run=client -o yaml | kubectl apply -f -
	kubectl apply -f config/samples/

# Both models on one GPU through a shared claim (ADR-0017). vLLM goes first and
# takes its fixed share; Ollama then fits what it can into the rest.
samples-shared:
	kubectl create namespace llm-platform --dry-run=client -o yaml | kubectl apply -f -
	kubectl apply -f config/samples/shared-gpu/claim.yaml -f config/samples/shared-gpu/qwen-small.yaml
	kubectl -n llm-platform wait isvc/qwen-small --for=jsonpath='{.status.phase}'=Ready --timeout=900s
	kubectl apply -f config/samples/shared-gpu/phi4-mini.yaml
	kubectl -n llm-platform wait isvc/phi4-mini --for=jsonpath='{.status.phase}'=Ready --timeout=1200s

# A small model trained from scratch on the shared GPU, beside both model
# servers. Loss and tokens per second show on the dashboard. Needs
# `make samples-shared`, for the claim it shares.
train:
	kubectl -n llm-platform create configmap train-tiny-lm --from-file=train.py=hack/train/train.py \
		--dry-run=client -o yaml | kubectl apply -f -
	kubectl -n llm-platform delete job train-tiny-lm --ignore-not-found
	kubectl apply -f hack/train/job.yaml
	kubectl -n llm-platform wait pod -l app.kubernetes.io/name=train-tiny-lm --for=condition=Ready --timeout=300s
	kubectl -n llm-platform logs -f job/train-tiny-lm

# llm-d's endpoint picker in front of the qwen-small pool, with flow control on
# (ADR-0020). The chart's own InferencePool is dropped: the operator owns it.
# Run after `make routes` and `make samples-shared`.
flow-control:
	helm template qwen-small oci://ghcr.io/llm-d/charts/llm-d-router-standalone --version v1.0.0 \
		-n llm-platform -f hack/llm-d/values.yaml | hack/llm-d/without-pool.py | kubectl apply -f -
	kubectl -n llm-platform rollout status deploy/qwen-small-epp --timeout=300s
	kubectl apply -f hack/llm-d/objectives.yaml

# Batch requests first, interactive ones behind them: who is answered first.
priority:
	.venv/bin/python hack/priority.py

# The gateway's decisions with stand-in backends. No cluster, GPU or model.
demo:
	.venv/bin/python hack/demo.py

# End-to-end against live backends: vLLM on :8001 and Ollama on :11434, or
# wherever VLLM_URL and OLLAMA_URL point.
verify:
	.venv/bin/python hack/verify.py

clean:
	rm -rf bin/
