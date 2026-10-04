.PHONY: build vet fmt test lint crd verify clean

build:
	go build -o bin/operator ./cmd/operator

vet:
	go vet ./...

fmt:
	gofmt -l -w .

test:
	.venv/bin/pytest

lint:
	.venv/bin/ruff check .

# Applies the CRD and exercises the CEL admission rules against a live cluster.
crd:
	kubectl apply -f config/crd/inferenceservice.yaml

# Needs vLLM on :8001 and Ollama on :11434.
verify:
	.venv/bin/python hack/verify.py

clean:
	rm -rf bin/
