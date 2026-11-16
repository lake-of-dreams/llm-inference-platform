.PHONY: all build vet fmt generate manifests check-generated test test-go test-py lint \
	validate cluster cluster-gpu gpu-driver observability load image deploy samples demo verify clean

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

# Prometheus, GPU and node metrics, Tempo for traces, and a Grafana dashboard.
observability:
	hack/observability/install.sh

# A ramp of requests at vLLM on localhost:8001, to watch on the dashboard.
load:
	.venv/bin/python hack/load.py

image:
	docker build -t $(IMG) .

deploy: image
	kind load docker-image $(IMG) --name $(KIND_CLUSTER)
	kubectl apply -k config/default

samples:
	kubectl create namespace llm-platform --dry-run=client -o yaml | kubectl apply -f -
	kubectl apply -f config/samples/

# The gateway's decisions with stand-in backends. No cluster, GPU or model.
demo:
	.venv/bin/python hack/demo.py

# End-to-end against live backends: vLLM on :8001 and Ollama on :11434, or
# wherever VLLM_URL and OLLAMA_URL point.
verify:
	.venv/bin/python hack/verify.py

clean:
	rm -rf bin/
