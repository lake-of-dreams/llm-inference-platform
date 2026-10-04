// Package v1alpha1 contains the InferenceService API.
//
// Weight-load time, queue targets and GPU claims are spec fields rather than
// annotations. That lets a CEL rule reject a bad combination when the object is
// admitted, before any pod is scheduled. CEL is the Common Expression Language
// that the API server evaluates on every create and update.
//
// +kubebuilder:object:generate=true
// +groupName=serving.platform.io
package v1alpha1

import (
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"sigs.k8s.io/controller-runtime/pkg/scheme"
)

var (
	GroupVersion  = schema.GroupVersion{Group: "serving.platform.io", Version: "v1alpha1"}
	SchemeBuilder = &scheme.Builder{GroupVersion: GroupVersion}
	AddToScheme   = SchemeBuilder.AddToScheme
)

// DataClassification constrains where a model may run. The API server enforces
// it at admission, so a Restricted model never lands on a node pool that has not
// declared the matching tier.
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

// InferenceServiceSpec is the desired state of one served model.
//
// The validation rules below run in the API server. A Restricted model with no
// deviceClass is rejected before a pod exists, and the rule still holds while
// the operator is down.
//
// +kubebuilder:validation:XValidation:rule="self.classification != 'Restricted' || (has(self.deviceClass) && size(self.deviceClass) > 0)",message="Restricted classification requires an explicit deviceClass"
// +kubebuilder:validation:XValidation:rule="self.backend != 'Ollama' || self.classification != 'Restricted'",message="Ollama backend is not approved for Restricted data"
// +kubebuilder:validation:XValidation:rule="self.backend != 'vLLM' || (has(self.deviceClass) && size(self.deviceClass) > 0)",message="vLLM backend requires a deviceClass: the vLLM image is built for CUDA and does not serve on CPU"
// +kubebuilder:validation:XValidation:rule="!has(self.minDeviceMemory) || (has(self.deviceClass) && size(self.deviceClass) > 0)",message="minDeviceMemory only applies with a deviceClass"
// +kubebuilder:validation:XValidation:rule="!has(self.autoscaling) || self.autoscaling.maxReplicas >= self.autoscaling.minReplicas",message="maxReplicas must be >= minReplicas"
type InferenceServiceSpec struct {
	// Model is the Hugging Face ID for vLLM, or the local tag for Ollama.
	// +kubebuilder:validation:MinLength=1
	Model string `json:"model"`

	// Backend is the serving runtime.
	// +kubebuilder:validation:Enum=vLLM;Ollama
	// +kubebuilder:default=vLLM
	Backend BackendType `json:"backend,omitempty"`

	// Classification drives the placement policy at admission.
	// +kubebuilder:validation:Enum=Public;Internal;Restricted
	// +kubebuilder:default=Internal
	Classification DataClassification `json:"classification,omitempty"`

	// MaxModelLen caps the context window, and so the KV cache. On a 4 GB card
	// this is the difference between starting and running out of memory.
	// +kubebuilder:validation:Minimum=256
	// +kubebuilder:default=4096
	MaxModelLen int32 `json:"maxModelLen,omitempty"`

	// GPUMemoryUtilization is the percent of GPU memory vLLM claims up front.
	// +kubebuilder:validation:Minimum=10
	// +kubebuilder:validation:Maximum=95
	// +kubebuilder:default=85
	GPUMemoryUtilization int32 `json:"gpuMemoryUtilization,omitempty"`

	// DeviceClass names a Dynamic Resource Allocation DeviceClass
	// (resource.k8s.io/v1). The operator creates a ResourceClaimTemplate that
	// asks for one device of this class. Empty means no accelerator, which only
	// the Ollama backend can use.
	DeviceClass string `json:"deviceClass,omitempty"`

	// MinDeviceMemory is the smallest device memory the claim will accept. The
	// scheduler then cannot place the pod on a card too small to hold the
	// weights and the KV cache at this context length. The check reads the
	// memory capacity the NVIDIA DRA driver publishes.
	// +optional
	MinDeviceMemory *resource.Quantity `json:"minDeviceMemory,omitempty"`

	// ModelCacheClaimName names a PersistentVolumeClaim that keeps downloaded
	// weights across pod restarts. Empty means a scratch volume, so every new
	// pod downloads the model again.
	// +optional
	ModelCacheClaimName string `json:"modelCacheClaimName,omitempty"`

	// WeightLoadSeconds sizes the startupProbe budget. Set it too low and the
	// liveness probe fires mid-load and restarts the container forever, with a
	// clean application log. See ADR-0002.
	// +kubebuilder:validation:Minimum=10
	// +kubebuilder:default=600
	WeightLoadSeconds int32 `json:"weightLoadSeconds,omitempty"`

	// +kubebuilder:default={}
	Autoscaling AutoscalingSpec `json:"autoscaling,omitempty"`

	// CostPerHourMilliUSD is the marginal cost of one replica in thousandths of
	// a US dollar per hour. The gateway ranks backends on it.
	// +kubebuilder:default=0
	CostPerHourMilliUSD int64 `json:"costPerHourMilliUSD,omitempty"`
}

type AutoscalingSpec struct {
	// MinReplicas is written to the Deployment once, at creation. After that
	// the autoscaler owns the replica count. See ADR-0004.
	//
	// The floor is one. Every autoscaling signal comes from a running vLLM
	// replica, so at zero replicas nothing reports demand and nothing would
	// ever scale the service back up.
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:default=1
	MinReplicas int32 `json:"minReplicas,omitempty"`
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:default=4
	MaxReplicas int32 `json:"maxReplicas,omitempty"`

	// QueueDepthTarget is the number of waiting requests per replica the
	// autoscaler aims for. Queue depth moves before latency does. GPU
	// utilisation can fall while requests pile up, because decode is limited
	// by memory bandwidth. ADR-0001.
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:default=4
	QueueDepthTarget int32 `json:"queueDepthTarget,omitempty"`

	// TTFTP95Milliseconds is the latency objective for time to first token at
	// the 95th percentile. It is a second trigger next to queue depth.
	// +kubebuilder:validation:Minimum=50
	// +kubebuilder:default=2000
	TTFTP95Milliseconds int32 `json:"ttftP95Milliseconds,omitempty"`

	// KVCacheUsagePercent is the average KV cache fill per replica above which
	// the autoscaler adds a replica. A full cache makes vLLM pause requests
	// before the waiting queue shows it.
	// +kubebuilder:validation:Minimum=10
	// +kubebuilder:validation:Maximum=100
	// +kubebuilder:default=80
	KVCacheUsagePercent int32 `json:"kvCacheUsagePercent,omitempty"`
}

type InferenceServiceStatus struct {
	ObservedGeneration int64  `json:"observedGeneration,omitempty"`
	ReadyReplicas      int32  `json:"readyReplicas,omitempty"`
	Endpoint           string `json:"endpoint,omitempty"`
	Phase              string `json:"phase,omitempty"`
	// +listType=map
	// +listMapKey=type
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// InferenceService declares one served model. The operator turns it into a
// Deployment, a Service, a GPU claim template, a KEDA ScaledObject and an
// InferencePool.
//
// There is deliberately no scale subresource. A scale subresource would let
// `kubectl scale` or an autoscaler write the replica count back into this
// spec, and ADR-0004 keeps the replica count out of the operator's hands.
//
// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:shortName=isvc
// +kubebuilder:printcolumn:name="Backend",type=string,JSONPath=`.spec.backend`
// +kubebuilder:printcolumn:name="Model",type=string,JSONPath=`.spec.model`
// +kubebuilder:printcolumn:name="Class",type=string,JSONPath=`.spec.classification`
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=`.status.phase`
// +kubebuilder:printcolumn:name="Ready",type=integer,JSONPath=`.status.readyReplicas`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`
type InferenceService struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              InferenceServiceSpec   `json:"spec,omitempty"`
	Status            InferenceServiceStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true
type InferenceServiceList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []InferenceService `json:"items"`
}

func init() { SchemeBuilder.Register(&InferenceService{}, &InferenceServiceList{}) }
