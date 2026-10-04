package controller

import (
	"fmt"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"

	servingv1alpha1 "github.com/lake-of-dreams/llm-inference-platform/api/v1alpha1"
)

func intstrFromInt(i int) intstr.IntOrString { return intstr.FromInt32(int32(i)) }

// desiredDeployment builds the serving Deployment. Careful around the probes
// and replicas if you edit it: startupProbe failureThreshold comes from
// spec.weightLoadSeconds, readiness stays fast and separate so a bad replica
// drops out of the Service without being restarted, and replicas is written at
// creation only (ADR-0004).
func (r *InferenceServiceReconciler) desiredDeployment(svc *servingv1alpha1.InferenceService) *appsv1.Deployment {
	l := labels(svc)
	replicas := svc.Spec.Autoscaling.MinReplicas

	var container corev1.Container
	switch svc.Spec.Backend {
	case servingv1alpha1.BackendOllama:
		container = ollamaContainer(svc)
	default:
		container = vllmContainer(svc)
	}

	// budget = weightLoadSeconds / period, +1 for the rounding
	const startupPeriod = 10
	container.StartupProbe = &corev1.Probe{
		ProbeHandler: corev1.ProbeHandler{HTTPGet: &corev1.HTTPGetAction{
			Path: "/health", Port: intstr.FromInt32(8000)}},
		PeriodSeconds:    startupPeriod,
		FailureThreshold: svc.Spec.WeightLoadSeconds/startupPeriod + 1,
	}
	container.ReadinessProbe = &corev1.Probe{
		ProbeHandler: corev1.ProbeHandler{HTTPGet: &corev1.HTTPGetAction{
			Path: "/health", Port: intstr.FromInt32(8000)}},
		PeriodSeconds:    5,
		FailureThreshold: 3,
	}
	container.LivenessProbe = &corev1.Probe{
		ProbeHandler: corev1.ProbeHandler{HTTPGet: &corev1.HTTPGetAction{
			Path: "/health", Port: intstr.FromInt32(8000)}},
		PeriodSeconds:    20,
		FailureThreshold: 3,
	}

	pod := corev1.PodSpec{Containers: []corev1.Container{container}}

	// DRA, resource.k8s.io/v1 (GA in 1.34). The scheduler matches this against
	// published ResourceSlices rather than counting nvidia.com/gpu integers.
	if svc.Spec.DeviceClass != "" {
		claimName := svc.Name + "-gpu"
		pod.ResourceClaims = []corev1.PodResourceClaim{{
			Name:                      claimName,
			ResourceClaimTemplateName: ptr(svc.Name + "-gpu-template"),
		}}
		pod.Containers[0].Resources.Claims = []corev1.ResourceClaim{{Name: claimName}}
	}

	return &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Name: svc.Name, Namespace: svc.Namespace, Labels: l},
		Spec: appsv1.DeploymentSpec{
			Replicas: &replicas,
			Selector: &metav1.LabelSelector{MatchLabels: l},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{
					Labels: l,
					Annotations: map[string]string{
						"prometheus.io/scrape": "true",
						"prometheus.io/port":   "8000",
						"prometheus.io/path":   "/metrics",
					},
				},
				Spec: pod,
			},
		},
	}
}

func vllmContainer(svc *servingv1alpha1.InferenceService) corev1.Container {
	return corev1.Container{
		Name:  "vllm",
		Image: "vllm/vllm-openai:v0.29.0",
		Args: []string{
			"--model", svc.Spec.Model,
			"--max-model-len", fmt.Sprintf("%d", svc.Spec.MaxModelLen),
			"--gpu-memory-utilization", fmt.Sprintf("%.2f", float64(svc.Spec.GPUMemoryUtilization)/100.0),
			"--port", "8000",
			// bounds concurrent sequences, and so KV-cache pressure. the knob
			// that matters on a small device. continuous batching is already on
			"--max-num-seqs", "16",
			"--disable-log-requests",
		},
		Ports: []corev1.ContainerPort{{Name: "http", ContainerPort: 8000}},
		Resources: corev1.ResourceRequirements{
			Requests: corev1.ResourceList{"cpu": qty("500m"), "memory": qty("2Gi")},
		},
	}
}

func ollamaContainer(svc *servingv1alpha1.InferenceService) corev1.Container {
	return corev1.Container{
		Name:  "ollama",
		Image: "ollama/ollama:0.12.3",
		Env: []corev1.EnvVar{
			{Name: "OLLAMA_HOST", Value: "0.0.0.0:8000"},
			{Name: "OLLAMA_MODELS", Value: "/models"},
			{Name: "OLLAMA_KEEP_ALIVE", Value: "24h"},
		},
		Ports: []corev1.ContainerPort{{Name: "http", ContainerPort: 8000}},
		Resources: corev1.ResourceRequirements{
			Requests: corev1.ResourceList{"cpu": qty("500m"), "memory": qty("2Gi")},
		},
	}
}

// Compares only what the operator owns. Replicas is not in here, KEDA owns it.
func specEquivalent(current, desired *appsv1.Deployment) bool {
	c, d := current.Spec.Template.Spec, desired.Spec.Template.Spec
	if len(c.Containers) != len(d.Containers) {
		return false
	}
	cc, dc := c.Containers[0], d.Containers[0]
	if cc.Image != dc.Image || len(cc.Args) != len(dc.Args) {
		return false
	}
	for i := range cc.Args {
		if cc.Args[i] != dc.Args[i] {
			return false
		}
	}
	if cc.StartupProbe == nil || dc.StartupProbe == nil {
		return cc.StartupProbe == dc.StartupProbe
	}
	return cc.StartupProbe.FailureThreshold == dc.StartupProbe.FailureThreshold
}

func ptr[T any](v T) *T { return &v }
