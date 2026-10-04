.PHONY: all build vet fmt generate manifests check-generated test test-go test-py lint \
	validate cluster image deploy samples verify clean

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

image:
	docker build -t $(IMG) .

deploy: image
	kind load docker-image $(IMG) --name $(KIND_CLUSTER)
	kubectl apply -k config/default

samples:
	kubectl create namespace llm-platform --dry-run=client -o yaml | kubectl apply -f -
	kubectl apply -f config/samples/

# End-to-end against live backends: vLLM on :8001 and Ollama on :11434.
verify:
	.venv/bin/python hack/verify.py

clean:
	rm -rf bin/
