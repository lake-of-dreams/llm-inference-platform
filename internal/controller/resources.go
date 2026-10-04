package controller

import (
	"fmt"

	corev1 "k8s.io/api/core/v1"
	resourcev1 "k8s.io/api/resource/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/util/intstr"
	corev1ac "k8s.io/client-go/applyconfigurations/core/v1"
	metav1ac "k8s.io/client-go/applyconfigurations/meta/v1"
	resourcev1ac "k8s.io/client-go/applyconfigurations/resource/v1"

	servingv1alpha1 "github.com/lake-of-dreams/llm-inference-platform/api/v1alpha1"
)

// KEDA and the Gateway API Inference Extension are optional add-ons. The
// operator writes their objects as unstructured data so it does not import
// either project's Go module, and it skips them when their CRDs are absent.
var (
	scaledObjectGVK  = schema.GroupVersionKind{Group: "keda.sh", Version: "v1alpha1", Kind: "ScaledObject"}
	inferencePoolGVK = schema.GroupVersionKind{Group: "inference.networking.k8s.io", Version: "v1", Kind: "InferencePool"}
)

// selectorLabels identify the pods of one InferenceService. A Deployment's
// selector cannot change after creation, so it holds only labels that cannot
// change either: the object's name. Backend and classification go on the pod
// as extra labels, where they are free to change.
func selectorLabels(svc *servingv1alpha1.InferenceService) map[string]string {
	return map[string]string{
		"app.kubernetes.io/name":     "inference-service",
		"app.kubernetes.io/instance": svc.Name,
	}
}

func podLabels(svc *servingv1alpha1.InferenceService) map[string]string {
	l := selectorLabels(svc)
	l["serving.platform.io/backend"] = string(svc.Spec.Backend)
	l["serving.platform.io/dataclass"] = string(svc.Spec.Classification)
	return l
}

func ownerRef(svc *servingv1alpha1.InferenceService) *metav1ac.OwnerReferenceApplyConfiguration {
	return metav1ac.OwnerReference().
		WithAPIVersion(servingv1alpha1.GroupVersion.String()).
		WithKind("InferenceService").
		WithName(svc.Name).
		WithUID(svc.UID).
		WithController(true).
		WithBlockOwnerDeletion(true)
}

func ownerRefMap(svc *servingv1alpha1.InferenceService) map[string]any {
	return map[string]any{
		"apiVersion":         servingv1alpha1.GroupVersion.String(),
		"kind":               "InferenceService",
		"name":               svc.Name,
		"uid":                string(svc.UID),
		"controller":         true,
		"blockOwnerDeletion": true,
	}
}

func desiredService(svc *servingv1alpha1.InferenceService) *corev1ac.ServiceApplyConfiguration {
	return corev1ac.Service(svc.Name, svc.Namespace).
		WithLabels(podLabels(svc)).
		WithOwnerReferences(ownerRef(svc)).
		WithSpec(corev1ac.ServiceSpec().
			WithSelector(selectorLabels(svc)).
			WithPorts(corev1ac.ServicePort().
				WithName("http").
				WithProtocol(corev1.ProtocolTCP).
				WithPort(servingPort).
				WithTargetPort(intstr.FromInt32(servingPort))))
}

// nvidiaDriverDomain is the name the NVIDIA DRA driver publishes device
// capacities under. It is the same for whole GPUs (DeviceClass gpu.nvidia.com)
// and MIG slices (mig.nvidia.com), so the memory floor works with either class.
// A device from another vendor's driver has no capacity under this name, so a
// memory floor on it matches nothing and the pod stays Pending.
const nvidiaDriverDomain = "gpu.nvidia.com"

func claimTemplateName(svc *servingv1alpha1.InferenceService) string {
	return svc.Name + "-gpu"
}

// desiredClaimTemplate asks for exactly one device of the named class. When
// minDeviceMemory is set, a CEL selector also requires the device to report at
// least that much memory. A count says nothing about the card: an L4 and an
// H100 both count as one, and a model that fits one runs out of memory on the
// other.
func desiredClaimTemplate(svc *servingv1alpha1.InferenceService) *resourcev1ac.ResourceClaimTemplateApplyConfiguration {
	req := resourcev1ac.ExactDeviceRequest().
		WithDeviceClassName(svc.Spec.DeviceClass).
		WithAllocationMode(resourcev1.DeviceAllocationModeExactCount).
		WithCount(1)
	if m := svc.Spec.MinDeviceMemory; m != nil {
		req.WithSelectors(resourcev1ac.DeviceSelector().WithCEL(resourcev1ac.CELDeviceSelector().
			WithExpression(fmt.Sprintf(
				`device.capacity[%q].memory.compareTo(quantity(%q)) >= 0`,
				nvidiaDriverDomain, m.String()))))
	}
	return resourcev1ac.ResourceClaimTemplate(claimTemplateName(svc), svc.Namespace).
		WithLabels(podLabels(svc)).
		WithOwnerReferences(ownerRef(svc)).
		WithSpec(resourcev1ac.ResourceClaimTemplateSpec().
			WithSpec(resourcev1ac.ResourceClaimSpec().
				WithDevices(resourcev1ac.DeviceClaim().
					WithRequests(resourcev1ac.DeviceRequest().
						WithName("gpu").
						WithExactly(req)))))
}

// desiredScaledObject builds the KEDA ScaledObject for a vLLM service. KEDA
// takes the highest replica count any trigger asks for, so each trigger can
// scale up on its own. ADR-0001 explains the choice of signals.
func desiredScaledObject(svc *servingv1alpha1.InferenceService, prometheusAddress string) *unstructured.Unstructured {
	a := svc.Spec.Autoscaling
	// vLLM labels its metrics with the served model name, which the operator
	// sets to the object's name. The namespace label comes from the usual
	// Prometheus pod scrape relabelling.
	sel := fmt.Sprintf(`namespace=%q,model_name=%q`, svc.Namespace, svc.Name)

	// A replica removed while its replacement is still loading weights leaves
	// the service stuck in cold start, so the scale-down window is at least
	// the weight-load budget.
	settle := max(int64(300), int64(svc.Spec.WeightLoadSeconds))

	prom := func(query, threshold, metricType string) map[string]any {
		t := map[string]any{
			"type": "prometheus",
			"metadata": map[string]any{
				"serverAddress": prometheusAddress,
				"query":         query,
				"threshold":     threshold,
			},
		}
		if metricType != "" {
			t["metricType"] = metricType
		}
		return t
	}

	triggers := []any{
		// Waiting requests, summed across replicas. The default AverageValue
		// metric type divides the sum by the replica count, which is the
		// per-replica queue the target describes.
		prom(fmt.Sprintf(`sum(vllm:num_requests_waiting{%s})`, sel),
			fmt.Sprint(a.QueueDepthTarget), ""),
		// KV cache fill, a fraction from 0 to 1, averaged across replicas.
		prom(fmt.Sprintf(`sum(vllm:kv_cache_usage_perc{%s})`, sel),
			fmt.Sprintf("%.2f", float64(a.KVCacheUsagePercent)/100.0), ""),
		// p95 time to first token. Value, not AverageValue: a latency does not
		// get smaller when it is divided by the number of replicas.
		prom(fmt.Sprintf(
			"histogram_quantile(0.95, sum(rate(vllm:time_to_first_token_seconds_bucket{%s}[2m])) by (le)) * 1000", sel),
			fmt.Sprint(a.TTFTP95Milliseconds), "Value"),
	}

	u := &unstructured.Unstructured{Object: map[string]any{
		"metadata": map[string]any{
			"name":            svc.Name,
			"namespace":       svc.Namespace,
			"labels":          toAny(podLabels(svc)),
			"ownerReferences": []any{ownerRefMap(svc)},
		},
		"spec": map[string]any{
			"scaleTargetRef":  map[string]any{"name": svc.Name},
			"minReplicaCount": int64(a.MinReplicas),
			"maxReplicaCount": int64(a.MaxReplicas),
			"pollingInterval": int64(15),
			"cooldownPeriod":  settle,
			"advanced": map[string]any{
				"horizontalPodAutoscalerConfig": map[string]any{
					"behavior": map[string]any{
						"scaleUp": map[string]any{
							// queue depth is the demand signal, so react at once
							"stabilizationWindowSeconds": int64(0),
							"policies": []any{map[string]any{
								"type": "Pods", "value": int64(1), "periodSeconds": int64(60)}},
						},
						"scaleDown": map[string]any{
							"stabilizationWindowSeconds": settle,
							"policies": []any{map[string]any{
								"type": "Pods", "value": int64(1), "periodSeconds": settle}},
						},
					},
				},
			},
			"triggers": triggers,
		},
	}}
	u.SetGroupVersionKind(scaledObjectGVK)
	return u
}

// desiredInferencePool registers the pods with the Gateway API Inference
// Extension. Since v1.6 the endpoint picker reference is optional, so a pool
// with no picker is valid and a gateway can still route to it.
func desiredInferencePool(svc *servingv1alpha1.InferenceService) *unstructured.Unstructured {
	u := &unstructured.Unstructured{Object: map[string]any{
		"metadata": map[string]any{
			"name":            svc.Name,
			"namespace":       svc.Namespace,
			"labels":          toAny(podLabels(svc)),
			"ownerReferences": []any{ownerRefMap(svc)},
		},
		"spec": map[string]any{
			"selector":    map[string]any{"matchLabels": toAny(selectorLabels(svc))},
			"targetPorts": []any{map[string]any{"number": int64(servingPort)}},
		},
	}}
	u.SetGroupVersionKind(inferencePoolGVK)
	return u
}

func toAny(m map[string]string) map[string]any {
	out := make(map[string]any, len(m))
	for k, v := range m {
		out[k] = v
	}
	return out
}
