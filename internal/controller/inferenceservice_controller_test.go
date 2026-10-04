package controller

import (
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	resourcev1 "k8s.io/api/resource/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/yaml"

	servingv1alpha1 "github.com/lake-of-dreams/llm-inference-platform/api/v1alpha1"
)

const promAddr = "http://prometheus.test:9090"

func newNamespace(t *testing.T) string {
	t.Helper()
	ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{GenerateName: "isvc-"}}
	if err := k8s.Create(ctx, ns); err != nil {
		t.Fatal(err)
	}
	return ns.Name
}

func vllmService(ns string) *servingv1alpha1.InferenceService {
	mem := resource.MustParse("3Gi")
	return &servingv1alpha1.InferenceService{
		ObjectMeta: metav1.ObjectMeta{Name: "qwen-small", Namespace: ns},
		Spec: servingv1alpha1.InferenceServiceSpec{
			Model:           "Qwen/Qwen2.5-0.5B-Instruct",
			Backend:         servingv1alpha1.BackendVLLM,
			Classification:  servingv1alpha1.ClassificationInternal,
			DeviceClass:     "gpu.nvidia.com",
			MinDeviceMemory: &mem,
			Autoscaling:     servingv1alpha1.AutoscalingSpec{MinReplicas: 2, MaxReplicas: 4},
		},
	}
}

func ollamaService(ns string) *servingv1alpha1.InferenceService {
	return &servingv1alpha1.InferenceService{
		ObjectMeta: metav1.ObjectMeta{Name: "phi4", Namespace: ns},
		Spec: servingv1alpha1.InferenceServiceSpec{
			Model:   "phi4-mini",
			Backend: servingv1alpha1.BackendOllama,
		},
	}
}

func reconciler(c client.Client) *InferenceServiceReconciler {
	return &InferenceServiceReconciler{Client: c, Scheme: scheme, PrometheusAddress: promAddr}
}

func reconcileOnce(t *testing.T, r *InferenceServiceReconciler, svc *servingv1alpha1.InferenceService) ctrl.Result {
	t.Helper()
	res, err := r.Reconcile(ctx, ctrl.Request{NamespacedName: client.ObjectKeyFromObject(svc)})
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	return res
}

func create(t *testing.T, svc *servingv1alpha1.InferenceService) {
	t.Helper()
	if err := k8s.Create(ctx, svc); err != nil {
		t.Fatal(err)
	}
}

func get[T client.Object](t *testing.T, obj T, ns, name string) T {
	t.Helper()
	if err := k8s.Get(ctx, types.NamespacedName{Namespace: ns, Name: name}, obj); err != nil {
		t.Fatalf("get %T %s: %v", obj, name, err)
	}
	return obj
}

func TestVLLMServiceCreatesEveryOwnedObject(t *testing.T) {
	ns := newNamespace(t)
	svc := vllmService(ns)
	create(t, svc)
	r := reconciler(k8s)
	reconcileOnce(t, r, svc)
	// A second pass applies the Deployment again without replicas. The count
	// must stay at minReplicas, not fall back to the Kubernetes default of one.
	reconcileOnce(t, r, svc)

	dep := get(t, &appsv1.Deployment{}, ns, svc.Name)
	if got := *dep.Spec.Replicas; got != 2 {
		t.Errorf("replicas = %d after two reconciles, want minReplicas 2", got)
	}
	c := dep.Spec.Template.Spec.Containers[0]
	if c.Image != vllmImage {
		t.Errorf("image = %s", c.Image)
	}
	if c.Args[0] != svc.Spec.Model {
		t.Errorf("first arg = %q, want the model", c.Args[0])
	}
	if slices.Contains(c.Args, "--disable-log-requests") {
		t.Error("--disable-log-requests was removed from vLLM and stops the server at start-up")
	}
	if i := slices.Index(c.Args, "--served-model-name"); i < 0 || c.Args[i+1] != svc.Name {
		t.Errorf("--served-model-name must be the object name so metric labels match; args=%v", c.Args)
	}
	if got := c.StartupProbe.FailureThreshold; got != 61 {
		t.Errorf("startup failureThreshold = %d, want 600/10+1 = 61", got)
	}
	if !hasVolume(dep, "dshm") || !hasVolume(dep, "model-cache") {
		t.Error("vLLM pod needs the dshm and model-cache volumes")
	}
	if len(dep.Spec.Template.Spec.ResourceClaims) != 1 ||
		*dep.Spec.Template.Spec.ResourceClaims[0].ResourceClaimTemplateName != svc.Name+"-gpu" {
		t.Errorf("pod resource claims = %+v", dep.Spec.Template.Spec.ResourceClaims)
	}
	if !metav1.IsControlledBy(dep, get(t, &servingv1alpha1.InferenceService{}, ns, svc.Name)) {
		t.Error("deployment is not controlled by the InferenceService")
	}

	rct := get(t, &resourcev1.ResourceClaimTemplate{}, ns, svc.Name+"-gpu")
	exact := rct.Spec.Spec.Devices.Requests[0].Exactly
	if exact.DeviceClassName != "gpu.nvidia.com" || exact.Count != 1 {
		t.Errorf("claim request = %+v", exact)
	}
	if len(exact.Selectors) != 1 || !strings.Contains(exact.Selectors[0].CEL.Expression, `quantity("3Gi")`) {
		t.Errorf("memory floor selector missing: %+v", exact.Selectors)
	}

	s := get(t, &corev1.Service{}, ns, svc.Name)
	if s.Spec.Selector["app.kubernetes.io/instance"] != svc.Name || len(s.Spec.Selector) != 2 {
		t.Errorf("service selector = %v", s.Spec.Selector)
	}

	so := &unstructured.Unstructured{}
	so.SetGroupVersionKind(scaledObjectGVK)
	get(t, so, ns, svc.Name)
	triggers, _, _ := unstructured.NestedSlice(so.Object, "spec", "triggers")
	if len(triggers) != 3 {
		t.Fatalf("triggers = %d, want queue depth, KV cache and TTFT", len(triggers))
	}
	for _, tr := range triggers {
		m := tr.(map[string]any)
		q := m["metadata"].(map[string]any)["query"].(string)
		if !strings.Contains(q, `model_name="qwen-small"`) || !strings.Contains(q, `namespace="`+ns+`"`) {
			t.Errorf("query does not select this service: %s", q)
		}
		if strings.Contains(q, "time_to_first_token") && m["metricType"] != "Value" {
			t.Errorf("TTFT trigger metricType = %v, want Value", m["metricType"])
		}
	}
	if minR, _, _ := unstructured.NestedInt64(so.Object, "spec", "minReplicaCount"); minR != 2 {
		t.Errorf("minReplicaCount = %d", minR)
	}

	pool := &unstructured.Unstructured{}
	pool.SetGroupVersionKind(inferencePoolGVK)
	get(t, pool, ns, svc.Name)
	if port, _, _ := unstructured.NestedSlice(pool.Object, "spec", "targetPorts"); len(port) != 1 {
		t.Errorf("targetPorts = %v", port)
	}

	got := get(t, &servingv1alpha1.InferenceService{}, ns, svc.Name)
	for _, cond := range []string{CondAdmitted, CondAutoscaling, CondRouting} {
		if !meta.IsStatusConditionTrue(got.Status.Conditions, cond) {
			t.Errorf("condition %s not True: %+v", cond, got.Status.Conditions)
		}
	}
	if got.Status.Phase != "Loading" || got.Status.ObservedGeneration != got.Generation {
		t.Errorf("status = %+v", got.Status)
	}
}

// ADR-0004. Another manager scales the Deployment, the spec changes, and the
// operator applies the change without touching replicas.
func TestReplicasSetByAutoscalerSurviveSpecChange(t *testing.T) {
	ns := newNamespace(t)
	svc := vllmService(ns)
	create(t, svc)
	r := reconciler(k8s)
	reconcileOnce(t, r, svc)

	dep := get(t, &appsv1.Deployment{}, ns, svc.Name)
	scale := client.RawPatch(types.MergePatchType, []byte(`{"spec":{"replicas":3}}`))
	if err := k8s.Patch(ctx, dep, scale, client.FieldOwner("keda-hpa")); err != nil {
		t.Fatal(err)
	}

	cur := get(t, &servingv1alpha1.InferenceService{}, ns, svc.Name)
	cur.Spec.MaxModelLen = 2048
	if err := k8s.Update(ctx, cur); err != nil {
		t.Fatal(err)
	}
	reconcileOnce(t, r, svc)
	reconcileOnce(t, r, svc)

	dep = get(t, &appsv1.Deployment{}, ns, svc.Name)
	if *dep.Spec.Replicas != 3 {
		t.Errorf("replicas = %d, want the autoscaler's 3", *dep.Spec.Replicas)
	}
	args := dep.Spec.Template.Spec.Containers[0].Args
	if i := slices.Index(args, "--max-model-len"); args[i+1] != "2048" {
		t.Errorf("spec change not applied: %v", args)
	}
}

// The old operator put classification in the Deployment selector, which made
// any classification change fail forever. Now it only changes a pod label.
func TestClassificationChangeUpdatesLabelsNotSelector(t *testing.T) {
	ns := newNamespace(t)
	svc := vllmService(ns)
	create(t, svc)
	r := reconciler(k8s)
	reconcileOnce(t, r, svc)

	cur := get(t, &servingv1alpha1.InferenceService{}, ns, svc.Name)
	cur.Spec.Classification = servingv1alpha1.ClassificationRestricted
	if err := k8s.Update(ctx, cur); err != nil {
		t.Fatal(err)
	}
	reconcileOnce(t, r, svc)

	dep := get(t, &appsv1.Deployment{}, ns, svc.Name)
	if got := dep.Spec.Template.Labels["serving.platform.io/dataclass"]; got != "Restricted" {
		t.Errorf("pod dataclass label = %q", got)
	}
	if len(dep.Spec.Selector.MatchLabels) != 2 {
		t.Errorf("selector grew: %v", dep.Spec.Selector.MatchLabels)
	}
}

// A Deployment left by the previous operator version has the old selector.
// The operator deletes it, then creates it again on the next pass.
func TestDeploymentWithOldSelectorIsRecreated(t *testing.T) {
	ns := newNamespace(t)
	svc := vllmService(ns)
	create(t, svc)

	old := podLabels(svc)
	legacy := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Name: svc.Name, Namespace: ns},
		Spec: appsv1.DeploymentSpec{
			Selector: &metav1.LabelSelector{MatchLabels: old},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: old},
				Spec:       corev1.PodSpec{Containers: []corev1.Container{{Name: "vllm", Image: "vllm/vllm-openai:v0.29.0"}}},
			},
		},
	}
	if err := k8s.Create(ctx, legacy); err != nil {
		t.Fatal(err)
	}
	oldUID := legacy.UID

	r := reconciler(k8s)
	if res := reconcileOnce(t, r, svc); res.RequeueAfter == 0 {
		t.Error("expected a requeue after deleting the old deployment")
	}
	reconcileOnce(t, r, svc)

	dep := get(t, &appsv1.Deployment{}, ns, svc.Name)
	if dep.UID == oldUID {
		t.Fatal("deployment was not recreated")
	}
	if len(dep.Spec.Selector.MatchLabels) != 2 {
		t.Errorf("selector = %v", dep.Spec.Selector.MatchLabels)
	}
}

func TestOllamaServiceProbesTheRightEndpoints(t *testing.T) {
	ns := newNamespace(t)
	svc := ollamaService(ns)
	create(t, svc)
	reconcileOnce(t, reconciler(k8s), svc)

	dep := get(t, &appsv1.Deployment{}, ns, svc.Name)
	c := dep.Spec.Template.Spec.Containers[0]
	if c.Image != ollamaImage {
		t.Errorf("image = %s", c.Image)
	}
	if got := c.ReadinessProbe.HTTPGet.Path; got != "/" {
		t.Errorf("readiness path = %q; Ollama has no /health", got)
	}
	if got := c.LivenessProbe.HTTPGet.Path; got != "/" {
		t.Errorf("liveness path = %q", got)
	}
	if c.StartupProbe.Exec == nil || !slices.Equal(c.StartupProbe.Exec.Command, []string{"/bin/ollama", "show", "phi4-mini"}) {
		t.Errorf("startup probe should wait for the pulled model: %+v", c.StartupProbe)
	}
	if !strings.Contains(c.Command[2], `ollama pull "$MODEL"`) {
		t.Errorf("container does not pull the model: %v", c.Command)
	}
	if hasVolume(dep, "dshm") {
		t.Error("Ollama does not need the shared-memory volume")
	}
	if *dep.Spec.Replicas != 1 {
		t.Errorf("replicas = %d, want default minReplicas 1", *dep.Spec.Replicas)
	}

	so := &unstructured.Unstructured{}
	so.SetGroupVersionKind(scaledObjectGVK)
	err := k8s.Get(ctx, types.NamespacedName{Namespace: ns, Name: svc.Name}, so)
	if !apierrors.IsNotFound(err) {
		t.Errorf("Ollama service should have no ScaledObject, got err=%v", err)
	}
	got := get(t, &servingv1alpha1.InferenceService{}, ns, svc.Name)
	if c := meta.FindStatusCondition(got.Status.Conditions, CondAutoscaling); c == nil || c.Reason != "BackendNotSupported" {
		t.Errorf("autoscaling condition = %+v", c)
	}
}

func TestSwitchingToOllamaRemovesClaimTemplateAndScaledObject(t *testing.T) {
	ns := newNamespace(t)
	svc := vllmService(ns)
	svc.Spec.MinDeviceMemory = nil
	create(t, svc)
	r := reconciler(k8s)
	reconcileOnce(t, r, svc)

	cur := get(t, &servingv1alpha1.InferenceService{}, ns, svc.Name)
	cur.Spec.Backend = servingv1alpha1.BackendOllama
	cur.Spec.DeviceClass = ""
	if err := k8s.Update(ctx, cur); err != nil {
		t.Fatal(err)
	}
	reconcileOnce(t, r, svc)

	err := k8s.Get(ctx, types.NamespacedName{Namespace: ns, Name: svc.Name + "-gpu"}, &resourcev1.ResourceClaimTemplate{})
	if !apierrors.IsNotFound(err) {
		t.Errorf("claim template should be deleted, err=%v", err)
	}
	so := &unstructured.Unstructured{}
	so.SetGroupVersionKind(scaledObjectGVK)
	err = k8s.Get(ctx, types.NamespacedName{Namespace: ns, Name: svc.Name}, so)
	if !apierrors.IsNotFound(err) {
		t.Errorf("scaledobject should be deleted, err=%v", err)
	}
	dep := get(t, &appsv1.Deployment{}, ns, svc.Name)
	if len(dep.Spec.Template.Spec.ResourceClaims) != 0 {
		t.Errorf("pod still claims a GPU: %+v", dep.Spec.Template.Spec.ResourceClaims)
	}
	if dep.Spec.Template.Spec.Containers[0].Name != "ollama" || len(dep.Spec.Template.Spec.Containers) != 1 {
		t.Errorf("containers = %+v", dep.Spec.Template.Spec.Containers)
	}
}

// The CEL rules run in the API server, so these objects never reach the
// operator at all.
func TestAdmissionRejectsBadSpecs(t *testing.T) {
	ns := newNamespace(t)
	cases := map[string]func(*servingv1alpha1.InferenceService){
		"restricted without deviceClass": func(s *servingv1alpha1.InferenceService) {
			s.Spec.Classification = servingv1alpha1.ClassificationRestricted
			s.Spec.DeviceClass = ""
			s.Spec.MinDeviceMemory = nil
		},
		"vLLM without deviceClass": func(s *servingv1alpha1.InferenceService) {
			s.Spec.DeviceClass = ""
			s.Spec.MinDeviceMemory = nil
		},
		"Ollama with restricted data": func(s *servingv1alpha1.InferenceService) {
			s.Spec.Backend = servingv1alpha1.BackendOllama
			s.Spec.Classification = servingv1alpha1.ClassificationRestricted
		},
		"maxReplicas below minReplicas": func(s *servingv1alpha1.InferenceService) {
			s.Spec.Autoscaling = servingv1alpha1.AutoscalingSpec{MinReplicas: 3, MaxReplicas: 2}
		},
		"minReplicas of zero": func(s *servingv1alpha1.InferenceService) {
			s.Spec.Autoscaling = servingv1alpha1.AutoscalingSpec{MinReplicas: 0, MaxReplicas: 2}
		},
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			svc := vllmService(ns)
			svc.Name = strings.ReplaceAll(strings.ToLower(name), " ", "-")
			mutate(svc)
			// minReplicas 0 is dropped by omitempty and defaulted to 1 by the
			// server, so send it as raw JSON to test the real floor.
			if svc.Spec.Autoscaling.MinReplicas == 0 && svc.Spec.Autoscaling.MaxReplicas != 0 {
				err := k8s.Create(ctx, rawZeroMinReplicas(svc))
				if !apierrors.IsInvalid(err) {
					t.Fatalf("want Invalid, got %v", err)
				}
				return
			}
			if err := k8s.Create(ctx, svc); !apierrors.IsInvalid(err) {
				t.Fatalf("want Invalid, got %v", err)
			}
		})
	}
}

func rawZeroMinReplicas(svc *servingv1alpha1.InferenceService) *unstructured.Unstructured {
	u := &unstructured.Unstructured{Object: map[string]any{
		"metadata": map[string]any{"name": svc.Name, "namespace": svc.Namespace},
		"spec": map[string]any{
			"model":       svc.Spec.Model,
			"deviceClass": svc.Spec.DeviceClass,
			"autoscaling": map[string]any{"minReplicas": int64(0), "maxReplicas": int64(2)},
		},
	}}
	u.SetGroupVersionKind(servingv1alpha1.GroupVersion.WithKind("InferenceService"))
	return u
}

// The operator must keep working on a cluster with neither KEDA nor the
// Inference Extension installed, and say so in status.
func TestMissingAddOnCRDsAreReportedNotFatal(t *testing.T) {
	env, cfg := startEnv(baseCRDs)
	defer func() { _ = env.Stop() }()
	c, err := client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}
	ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: "bare"}}
	if err := c.Create(ctx, ns); err != nil {
		t.Fatal(err)
	}
	svc := vllmService(ns.Name)
	if err := c.Create(ctx, svc); err != nil {
		t.Fatal(err)
	}
	reconcileOnce(t, reconciler(c), svc)

	got := &servingv1alpha1.InferenceService{}
	if err := c.Get(ctx, client.ObjectKeyFromObject(svc), got); err != nil {
		t.Fatal(err)
	}
	for cond, reason := range map[string]string{
		CondAutoscaling: "KEDANotInstalled",
		CondRouting:     "InferencePoolNotInstalled",
	} {
		if c := meta.FindStatusCondition(got.Status.Conditions, cond); c == nil || c.Reason != reason {
			t.Errorf("%s condition = %+v, want reason %s", cond, c, reason)
		}
	}
	if err := c.Get(ctx, client.ObjectKeyFromObject(svc), &appsv1.Deployment{}); err != nil {
		t.Errorf("deployment should still exist: %v", err)
	}
}

func TestPolicyCheckInOperatorMatchesCEL(t *testing.T) {
	// The operator repeats the admission rules in case the installed CRD is
	// older than the operator. Same inputs, same answers.
	svc := vllmService("x")
	if msg := policyViolation(svc); msg != "" {
		t.Errorf("valid spec rejected: %s", msg)
	}
	svc.Spec.DeviceClass = ""
	if policyViolation(svc) == "" {
		t.Error("vLLM without deviceClass accepted")
	}
	o := ollamaService("x")
	if msg := policyViolation(o); msg != "" {
		t.Errorf("CPU Ollama rejected: %s", msg)
	}
	o.Spec.Classification = servingv1alpha1.ClassificationRestricted
	o.Spec.DeviceClass = "gpu.nvidia.com"
	if policyViolation(o) == "" {
		t.Error("Ollama with Restricted data accepted")
	}
}

func hasVolume(dep *appsv1.Deployment, name string) bool {
	return slices.ContainsFunc(dep.Spec.Template.Spec.Volumes, func(v corev1.Volume) bool { return v.Name == name })
}

// The samples in config/samples are what a reader applies first. Each one must
// pass the CRD's schema and CEL rules.
func TestSamplesAreAdmitted(t *testing.T) {
	files, err := filepath.Glob(filepath.Join("..", "..", "config", "samples", "*.yaml"))
	if err != nil || len(files) == 0 {
		t.Fatalf("no samples found: %v", err)
	}
	ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: "llm-platform"}}
	if err := k8s.Create(ctx, ns); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatal(err)
	}
	for _, f := range files {
		raw, err := os.ReadFile(f)
		if err != nil {
			t.Fatal(err)
		}
		u := &unstructured.Unstructured{}
		if err := yaml.Unmarshal(raw, &u.Object); err != nil {
			t.Fatalf("%s: %v", f, err)
		}
		if err := k8s.Create(ctx, u); err != nil {
			t.Errorf("%s rejected: %v", filepath.Base(f), err)
		}
	}
}
