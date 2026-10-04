package controller

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"testing"

	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"

	servingv1alpha1 "github.com/lake-of-dreams/llm-inference-platform/api/v1alpha1"
)

// The tests run against a real kube-apiserver and etcd started by envtest.
// There are no controllers in that API server, so pods never run and nothing
// garbage-collects. What it does give is the real behaviour of server-side
// apply, field ownership, immutable fields and the CRD's CEL rules, which are
// the things this operator depends on.

var (
	scheme   = runtime.NewScheme()
	k8s      client.Client
	ctx      = context.Background()
	baseCRDs = filepath.Join("..", "..", "config", "crd", "bases")
	addOns   = filepath.Join("testdata", "crds")
)

func TestMain(m *testing.M) {
	if os.Getenv("KUBEBUILDER_ASSETS") == "" {
		fmt.Println("KUBEBUILDER_ASSETS is not set; run `make test-go`, which downloads the envtest binaries")
		os.Exit(1)
	}
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		panic(err)
	}
	if err := servingv1alpha1.AddToScheme(scheme); err != nil {
		panic(err)
	}

	env, cfg := startEnv(baseCRDs, addOns)
	var err error
	k8s, err = client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		panic(err)
	}
	code := m.Run()
	_ = env.Stop()
	os.Exit(code)
}

func startEnv(crdDirs ...string) (*envtest.Environment, *rest.Config) {
	env := &envtest.Environment{CRDDirectoryPaths: crdDirs, ErrorIfCRDPathMissing: true}
	cfg, err := env.Start()
	if err != nil {
		panic(err)
	}
	return env, cfg
}
