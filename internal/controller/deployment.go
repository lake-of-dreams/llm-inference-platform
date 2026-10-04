package controller

import (
	"fmt"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	"k8s.io/apimachinery/pkg/util/intstr"
	appsv1ac "k8s.io/client-go/applyconfigurations/apps/v1"
	corev1ac "k8s.io/client-go/applyconfigurations/core/v1"
	metav1ac "k8s.io/client-go/applyconfigurations/meta/v1"

	servingv1alpha1 "github.com/lake-of-dreams/llm-inference-platform/api/v1alpha1"
)

const (
	servingPort = 8000

	vllmImage   = "vllm/vllm-openai:v0.30.0"
	ollamaImage = "ollama/ollama:0.35.1"

	// The startup probe checks every startupPeriod seconds. Its failure budget
	// is weightLoadSeconds divided by this period, plus one for rounding.
	startupPeriod = 10
)

// desiredDeployment builds the serving Deployment as a server-side apply
// configuration.
//
// Replicas is left out on purpose. The operator applies this object on every
// reconcile, and server-side apply only changes fields the applier lists. With
// replicas absent, the operator never touches the count the autoscaler set
// (ADR-0004). The initial count is written once, by createDeployment.
func desiredDeployment(svc *servingv1alpha1.InferenceService) *appsv1ac.DeploymentApplyConfiguration {
	var container *corev1ac.ContainerApplyConfiguration
	switch svc.Spec.Backend {
	case servingv1alpha1.BackendOllama:
		container = ollamaContainer(svc)
	default:
		container = vllmContainer(svc)
	}

	pod := corev1ac.PodSpec().
		WithContainers(container).
		WithVolumes(modelCacheVolume(svc))

	if svc.Spec.Backend != servingv1alpha1.BackendOllama {
		// vLLM workers share tensors through /dev/shm. The container default is
		// 64 MiB, which is too small and fails without a clear message.
		pod.WithVolumes(corev1ac.Volume().WithName("dshm").
			WithEmptyDir(corev1ac.EmptyDirVolumeSource().
				WithMedium(corev1.StorageMediumMemory).
				WithSizeLimit(resource.MustParse("1Gi"))))
	}

	// Dynamic Resource Allocation. The scheduler matches the claim against the
	// devices each node publishes, rather than counting nvidia.com/gpu integers.
	if svc.Spec.DeviceClass != "" {
		pod.WithResourceClaims(corev1ac.PodResourceClaim().
			WithName("gpu").
			WithResourceClaimTemplateName(claimTemplateName(svc)))
		container.WithResources(container.Resources.
			WithClaims(corev1ac.ResourceClaim().WithName("gpu")))
	}

	return appsv1ac.Deployment(svc.Name, svc.Namespace).
		WithLabels(podLabels(svc)).
		WithOwnerReferences(ownerRef(svc)).
		WithSpec(appsv1ac.DeploymentSpec().
			WithSelector(metav1ac.LabelSelector().WithMatchLabels(selectorLabels(svc))).
			WithTemplate(corev1ac.PodTemplateSpec().
				WithLabels(podLabels(svc)).
				WithAnnotations(map[string]string{
					"prometheus.io/scrape": "true",
					"prometheus.io/port":   fmt.Sprint(servingPort),
					"prometheus.io/path":   "/metrics",
				}).
				WithSpec(pod)))
}

func vllmContainer(svc *servingv1alpha1.InferenceService) *corev1ac.ContainerApplyConfiguration {
	return corev1ac.Container().
		WithName("vllm").
		WithImage(vllmImage).
		// The image entrypoint is `vllm serve`, so the model is the first argument.
		WithArgs(
			svc.Spec.Model,
			// Prometheus labels every vLLM metric with model_name, taken from this
			// flag. The ScaledObject queries filter on it, so it must be stable
			// and unique per InferenceService.
			"--served-model-name", svc.Name,
			"--max-model-len", fmt.Sprint(svc.Spec.MaxModelLen),
			"--gpu-memory-utilization", fmt.Sprintf("%.2f", float64(svc.Spec.GPUMemoryUtilization)/100.0),
			"--port", fmt.Sprint(servingPort),
			// Bounds concurrent sequences, and so KV cache pressure. This is the
			// setting that matters on a small device. Continuous batching is on
			// by default.
			"--max-num-seqs", "16",
		).
		WithPorts(corev1ac.ContainerPort().WithName("http").WithContainerPort(servingPort)).
		WithVolumeMounts(
			corev1ac.VolumeMount().WithName("model-cache").WithMountPath("/root/.cache/huggingface"),
			corev1ac.VolumeMount().WithName("dshm").WithMountPath("/dev/shm"),
		).
		WithResources(baseResources()).
		WithStartupProbe(httpProbe("/health").
			WithPeriodSeconds(startupPeriod).
			WithFailureThreshold(startupFailureThreshold(svc))).
		WithReadinessProbe(httpProbe("/health").WithPeriodSeconds(5).WithFailureThreshold(3)).
		WithLivenessProbe(httpProbe("/health").WithPeriodSeconds(20).WithFailureThreshold(3))
}

// ollamaServe starts the server, waits for it to answer, then pulls the model.
// Ollama can only pull through a running server, so the pull cannot be an init
// container. If the pull fails the container exits and the pod restarts.
const ollamaServe = `/bin/ollama serve & pid=$!
trap 'kill -TERM "$pid"' TERM INT
until /bin/ollama list >/dev/null 2>&1; do sleep 1; done
/bin/ollama pull "$MODEL" || { kill -TERM "$pid"; exit 1; }
wait "$pid"`

func ollamaContainer(svc *servingv1alpha1.InferenceService) *corev1ac.ContainerApplyConfiguration {
	return corev1ac.Container().
		WithName("ollama").
		WithImage(ollamaImage).
		WithCommand("/bin/sh", "-c", ollamaServe).
		WithEnv(
			corev1ac.EnvVar().WithName("MODEL").WithValue(svc.Spec.Model),
			corev1ac.EnvVar().WithName("OLLAMA_HOST").WithValue(fmt.Sprintf("0.0.0.0:%d", servingPort)),
			corev1ac.EnvVar().WithName("OLLAMA_MODELS").WithValue("/models"),
			corev1ac.EnvVar().WithName("OLLAMA_KEEP_ALIVE").WithValue("24h"),
		).
		WithPorts(corev1ac.ContainerPort().WithName("http").WithContainerPort(servingPort)).
		WithVolumeMounts(corev1ac.VolumeMount().WithName("model-cache").WithMountPath("/models")).
		WithResources(baseResources()).
		// Ollama has no /health endpoint. `ollama show` succeeds only once the
		// pull has finished, so the startup probe waits for the weights rather
		// than for the HTTP listener.
		WithStartupProbe(corev1ac.Probe().
			WithExec(corev1ac.ExecAction().WithCommand("/bin/ollama", "show", svc.Spec.Model)).
			WithPeriodSeconds(startupPeriod).
			WithTimeoutSeconds(5).
			WithFailureThreshold(startupFailureThreshold(svc))).
		// GET / answers "Ollama is running".
		WithReadinessProbe(httpProbe("/").WithPeriodSeconds(5).WithFailureThreshold(3)).
		WithLivenessProbe(httpProbe("/").WithPeriodSeconds(20).WithFailureThreshold(3))
}

func modelCacheVolume(svc *servingv1alpha1.InferenceService) *corev1ac.VolumeApplyConfiguration {
	v := corev1ac.Volume().WithName("model-cache")
	if svc.Spec.ModelCacheClaimName != "" {
		return v.WithPersistentVolumeClaim(corev1ac.PersistentVolumeClaimVolumeSource().
			WithClaimName(svc.Spec.ModelCacheClaimName))
	}
	return v.WithEmptyDir(corev1ac.EmptyDirVolumeSource())
}

func baseResources() *corev1ac.ResourceRequirementsApplyConfiguration {
	return corev1ac.ResourceRequirements().
		WithRequests(corev1.ResourceList{
			corev1.ResourceCPU:    resource.MustParse("500m"),
			corev1.ResourceMemory: resource.MustParse("2Gi"),
		})
}

func httpProbe(path string) *corev1ac.ProbeApplyConfiguration {
	return corev1ac.Probe().WithHTTPGet(corev1ac.HTTPGetAction().
		WithPath(path).
		WithPort(intstr.FromInt32(servingPort)))
}

func startupFailureThreshold(svc *servingv1alpha1.InferenceService) int32 {
	return svc.Spec.WeightLoadSeconds/startupPeriod + 1
}
