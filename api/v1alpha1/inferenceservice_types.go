// Package v1alpha1 contains the InferenceService API.
//
// Weight-load time, queue targets and GPU claims are spec fields, not
// annotations, so a CEL rule can reject a bad combination at admission instead
// of after a pod has been scheduled.
package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"sigs.k8s.io/controller-runtime/pkg/scheme"
)

var (
	GroupVersion  = schema.GroupVersion{Group: "serving.platform.io", Version: "v1alpha1"}
	SchemeBuilder = &scheme.Builder{GroupVersion: GroupVersion}
	AddToScheme   = SchemeBuilder.AddToScheme
)

// DataClassification constrains where a model may run. Enforced at admission,
// so a Restricted model never lands on a node pool that has not declared the
// matching tier.
type DataClassification string

const (
	ClassificationPublic     DataClassification = "Public"
	ClassificationInternal   DataClassification = "Internal"
	ClassificationRestricted DataClassification = "Restricted"
)

type BackendType string

const (
	BackendVLLM   BackendType = "vLLM"
	BackendOllama BackendType = "Ollama"
)

type InferenceServiceSpec struct {
	// HuggingFace ID for vLLM, local tag for Ollama.
	// +kubebuilder:validation:MinLength=1
	Model string `json:"model"`

	// Serving runtime.
	// +kubebuilder:validation:Enum=vLLM;Ollama
	// +kubebuilder:default=vLLM
	Backend BackendType `json:"backend,omitempty"`

	// Drives the placement policy at admission.
	// +kubebuilder:validation:Enum=Public;Internal;Restricted
	// +kubebuilder:default=Internal
	Classification DataClassification `json:"classification,omitempty"`

	// MaxModelLen caps the context window, and so the KV cache. On a 4 GB card
	// this is the difference between starting and OOMing.
	// +kubebuilder:validation:Minimum=256
	// +kubebuilder:default=4096
	MaxModelLen int32 `json:"maxModelLen,omitempty"`

	// Percent of VRAM vLLM pre-allocates up front.
	// +kubebuilder:validation:Minimum=10
	// +kubebuilder:validation:Maximum=95
	// +kubebuilder:default=85
	GPUMemoryUtilization int32 `json:"gpuMemoryUtilization,omitempty"`

	// Names a DRA DeviceClass (resource.k8s.io/v1, GA in 1.34). Empty means
	// CPU-only serving.
	DeviceClass string `json:"deviceClass,omitempty"`

	// WeightLoadSeconds sizes the startupProbe budget. Set it too low and
	// liveness fires mid-load and restarts the container forever, with a clean
	// application log. See ADR-0002.
	// +kubebuilder:validation:Minimum=10
	// +kubebuilder:default=600
	WeightLoadSeconds int32 `json:"weightLoadSeconds,omitempty"`

	Autoscaling AutoscalingSpec `json:"autoscaling,omitempty"`

	// Marginal cost of one replica in thousandths of a USD per hour. The gateway
	// ranks backends on it.
	// +kubebuilder:default=0
	CostPerHourMilliUSD int64 `json:"costPerHourMilliUSD,omitempty"`
}

type AutoscalingSpec struct {
	// +kubebuilder:validation:Minimum=0
	// +kubebuilder:default=1
	MinReplicas int32 `json:"minReplicas,omitempty"`
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:default=4
	MaxReplicas int32 `json:"maxReplicas,omitempty"`

	// Target for vllm:num_requests_waiting. Queue depth moves before latency
	// does. GPU utilisation can fall while requests pile up, because decode is
	// memory-bandwidth bound. ADR-0001.
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:default=4
	QueueDepthTarget int32 `json:"queueDepthTarget,omitempty"`

	// SLO guardrail, wired up as a second KEDA trigger rather than a replacement.
	// +kubebuilder:validation:Minimum=50
	// +kubebuilder:default=2000
	TTFTP95Milliseconds int32 `json:"ttftP95Milliseconds,omitempty"`
}

type InferenceServiceStatus struct {
	ObservedGeneration int32              `json:"observedGeneration,omitempty"`
	ReadyReplicas      int32              `json:"readyReplicas,omitempty"`
	Endpoint           string             `json:"endpoint,omitempty"`
	Phase              string             `json:"phase,omitempty"`
	Conditions         []metav1.Condition `json:"conditions,omitempty"`
}

type InferenceService struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              InferenceServiceSpec   `json:"spec,omitempty"`
	Status            InferenceServiceStatus `json:"status,omitempty"`
}

type InferenceServiceList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []InferenceService `json:"items"`
}

func init() { SchemeBuilder.Register(&InferenceService{}, &InferenceServiceList{}) }
