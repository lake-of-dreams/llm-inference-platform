package controller

import (
	"context"
	"fmt"
	"maps"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	resourcev1 "k8s.io/api/resource/v1"
	"k8s.io/apimachinery/pkg/api/equality"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	appsv1ac "k8s.io/client-go/applyconfigurations/apps/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	servingv1alpha1 "github.com/lake-of-dreams/llm-inference-platform/api/v1alpha1"
)

const (
	// FieldOwner is the name the operator applies under. The API server records
	// which manager owns each field, and that record is what keeps the operator
	// and the autoscaler from fighting over replicas (ADR-0004).
	FieldOwner = "inference-operator"

	// initialReplicasOwner sets the replica count once, at creation, and owns
	// no other field. See createDeployment.
	initialReplicasOwner = "inference-operator-initial-replicas"

	// Resync floor. Watches catch nearly everything. This is for out-of-band
	// edits that raise no event on an object the operator owns.
	driftRequeue = 5 * time.Minute

	// Condition types reported in status.
	CondAdmitted    = "Admitted"
	CondReady       = "Ready"
	CondAutoscaling = "Autoscaling"
	CondRouting     = "Routing"
)

type InferenceServiceReconciler struct {
	client.Client
	Scheme *runtime.Scheme

	// PrometheusAddress is where KEDA reads vLLM metrics from.
	PrometheusAddress string
}

// +kubebuilder:rbac:groups=serving.platform.io,resources=inferenceservices,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=serving.platform.io,resources=inferenceservices/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=serving.platform.io,resources=inferenceservices/finalizers,verbs=update
// +kubebuilder:rbac:groups=apps,resources=deployments,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=core,resources=services,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=resource.k8s.io,resources=resourceclaimtemplates,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=keda.sh,resources=scaledobjects,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=inference.networking.k8s.io,resources=inferencepools,verbs=get;list;watch;create;update;patch;delete

func (r *InferenceServiceReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	var svc servingv1alpha1.InferenceService
	if err := r.Get(ctx, req.NamespacedName, &svc); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	before := svc.Status.DeepCopy()

	result, err := r.reconcile(ctx, &svc)

	svc.Status.ObservedGeneration = svc.Generation
	if !equality.Semantic.DeepEqual(before, &svc.Status) {
		if serr := r.Status().Update(ctx, &svc); serr != nil && err == nil {
			return ctrl.Result{}, serr
		}
	}
	return result, err
}

func (r *InferenceServiceReconciler) reconcile(ctx context.Context, svc *servingv1alpha1.InferenceService) (ctrl.Result, error) {
	l := log.FromContext(ctx)

	// The CRD's CEL rules say the same thing, but the CRD installed in a
	// cluster is not always the one shipped with this operator.
	if msg := policyViolation(svc); msg != "" {
		r.condition(svc, CondAdmitted, metav1.ConditionFalse, "PolicyViolation", msg)
		svc.Status.Phase = "Rejected"
		// nothing to retry until the spec changes, and a spec change requeues
		return ctrl.Result{}, nil
	}
	r.condition(svc, CondAdmitted, metav1.ConditionTrue, "PolicySatisfied", "spec passes placement policy")

	// The claim template has to exist before a pod that names it can schedule.
	if svc.Spec.DeviceClass != "" {
		if err := r.Apply(ctx, desiredClaimTemplate(svc), client.FieldOwner(FieldOwner), client.ForceOwnership); err != nil {
			return ctrl.Result{}, fmt.Errorf("apply resource claim template: %w", err)
		}
	} else if err := r.deleteIfOwned(ctx, svc, &resourcev1.ResourceClaimTemplate{}, claimTemplateName(svc)); err != nil {
		return ctrl.Result{}, err
	}

	dep, requeue, err := r.reconcileDeployment(ctx, svc)
	if err != nil || requeue {
		return ctrl.Result{RequeueAfter: 5 * time.Second}, err
	}

	if err := r.Apply(ctx, desiredService(svc), client.FieldOwner(FieldOwner), client.ForceOwnership); err != nil {
		return ctrl.Result{}, fmt.Errorf("apply service: %w", err)
	}

	if err := r.reconcileAutoscaling(ctx, svc); err != nil {
		return ctrl.Result{}, err
	}
	if err := r.reconcileRouting(ctx, svc); err != nil {
		return ctrl.Result{}, err
	}

	svc.Status.ReadyReplicas = dep.Status.ReadyReplicas
	svc.Status.Endpoint = fmt.Sprintf("http://%s.%s.svc.cluster.local:%d/v1", svc.Name, svc.Namespace, servingPort)
	if dep.Status.ReadyReplicas > 0 {
		svc.Status.Phase = "Ready"
		r.condition(svc, CondReady, metav1.ConditionTrue, "MinimumReplicasAvailable", "serving traffic")
	} else {
		svc.Status.Phase = "Loading"
		// Running but not ready is normal for minutes while weights load.
		r.condition(svc, CondReady, metav1.ConditionFalse, "WeightsLoading", "no ready replicas yet")
	}
	l.V(1).Info("reconciled", "phase", svc.Status.Phase)
	return ctrl.Result{RequeueAfter: driftRequeue}, nil
}

// reconcileDeployment creates the Deployment with its initial replica count,
// then applies the rest of its spec. It returns requeue=true when it had to
// delete an old Deployment whose selector can no longer be changed.
func (r *InferenceServiceReconciler) reconcileDeployment(ctx context.Context, svc *servingv1alpha1.InferenceService) (*appsv1.Deployment, bool, error) {
	l := log.FromContext(ctx)
	desired := desiredDeployment(svc)
	key := types.NamespacedName{Name: svc.Name, Namespace: svc.Namespace}

	var current appsv1.Deployment
	err := r.Get(ctx, key, &current)
	switch {
	case apierrors.IsNotFound(err):
		if err := r.createDeployment(ctx, svc); err != nil {
			return nil, false, err
		}
		l.Info("created deployment", "name", svc.Name)
	case err != nil:
		return nil, false, err
	case current.DeletionTimestamp != nil:
		// Still on its way out from an earlier recreate. Wait for it to go.
		return nil, true, nil
	default:
		// Deployments created by earlier operator versions carry backend and
		// classification in the selector. A selector cannot be edited, so the
		// only way forward is to delete the Deployment and create it again.
		want := selectorLabels(svc)
		if current.Spec.Selector == nil || !maps.Equal(current.Spec.Selector.MatchLabels, want) {
			l.Info("selector is immutable and out of date, recreating deployment", "name", svc.Name)
			r.condition(svc, CondReady, metav1.ConditionFalse, "RecreatingDeployment",
				"deployment selector changed; the deployment is being recreated")
			return nil, true, client.IgnoreNotFound(r.Delete(ctx, &current,
				client.PropagationPolicy(metav1.DeletePropagationBackground)))
		}
	}

	// Server-side apply sends the whole desired object every time. The API
	// server stores nothing when it matches what is there, so an unchanged
	// reconcile costs one request and no write. Replicas is not in the applied
	// object, so it is never reset.
	if err := r.Apply(ctx, desired, client.FieldOwner(FieldOwner), client.ForceOwnership); err != nil {
		return nil, false, fmt.Errorf("apply deployment: %w", err)
	}
	if err := r.Get(ctx, key, &current); err != nil {
		return nil, false, err
	}
	return &current, false, nil
}

// createDeployment is the one place replicas is ever written. It applies the
// full Deployment under the operator's own name, then applies replicas alone
// under a second name, initialReplicasOwner.
//
// The split is what keeps later applies honest. Server-side apply removes a
// field only when no other manager still owns it. If the operator's own name
// owned replicas, its next apply, which leaves replicas out, would remove the
// count and Kubernetes would reset it to one. Under the second name the count
// stays until the autoscaler takes it over. The second name never writes again.
func (r *InferenceServiceReconciler) createDeployment(ctx context.Context, svc *servingv1alpha1.InferenceService) error {
	if err := r.Apply(ctx, desiredDeployment(svc), client.FieldOwner(FieldOwner), client.ForceOwnership); err != nil {
		return fmt.Errorf("create deployment: %w", err)
	}
	initial := appsv1ac.Deployment(svc.Name, svc.Namespace).
		WithSpec(appsv1ac.DeploymentSpec().WithReplicas(svc.Spec.Autoscaling.MinReplicas))
	if err := r.Apply(ctx, initial, client.FieldOwner(initialReplicasOwner)); err != nil {
		return fmt.Errorf("set initial replicas: %w", err)
	}
	return nil
}

func (r *InferenceServiceReconciler) reconcileAutoscaling(ctx context.Context, svc *servingv1alpha1.InferenceService) error {
	if svc.Spec.Backend == servingv1alpha1.BackendOllama {
		// Ollama does not export the vLLM queue and cache metrics, so there is
		// nothing to scale on. The Deployment keeps minReplicas.
		r.condition(svc, CondAutoscaling, metav1.ConditionFalse, "BackendNotSupported",
			"Ollama exports no queue metrics; replicas stay at minReplicas")
		return r.deleteUnstructuredIfOwned(ctx, svc, scaledObjectGVK)
	}
	err := r.Apply(ctx, client.ApplyConfigurationFromUnstructured(desiredScaledObject(svc, r.PrometheusAddress)),
		client.FieldOwner(FieldOwner), client.ForceOwnership)
	switch {
	case meta.IsNoMatchError(err):
		r.condition(svc, CondAutoscaling, metav1.ConditionFalse, "KEDANotInstalled",
			"the keda.sh ScaledObject CRD is not installed; replicas stay at minReplicas")
		return nil
	case err != nil:
		return fmt.Errorf("apply scaledobject: %w", err)
	}
	r.condition(svc, CondAutoscaling, metav1.ConditionTrue, "ScaledObjectApplied",
		"KEDA scales on queue depth, KV cache usage and p95 TTFT")
	return nil
}

func (r *InferenceServiceReconciler) reconcileRouting(ctx context.Context, svc *servingv1alpha1.InferenceService) error {
	err := r.Apply(ctx, client.ApplyConfigurationFromUnstructured(desiredInferencePool(svc)),
		client.FieldOwner(FieldOwner), client.ForceOwnership)
	switch {
	case meta.IsNoMatchError(err):
		r.condition(svc, CondRouting, metav1.ConditionFalse, "InferencePoolNotInstalled",
			"the InferencePool CRD is not installed; the Service is the only entry point")
		return nil
	case err != nil:
		return fmt.Errorf("apply inferencepool: %w", err)
	}
	r.condition(svc, CondRouting, metav1.ConditionTrue, "InferencePoolApplied", "pods are registered in an InferencePool")
	return nil
}

// deleteIfOwned removes an object this InferenceService created earlier and no
// longer needs, such as the claim template after deviceClass is cleared.
func (r *InferenceServiceReconciler) deleteIfOwned(ctx context.Context, svc *servingv1alpha1.InferenceService, obj client.Object, name string) error {
	err := r.Get(ctx, types.NamespacedName{Name: name, Namespace: svc.Namespace}, obj)
	if apierrors.IsNotFound(err) || meta.IsNoMatchError(err) {
		return nil
	}
	if err != nil {
		return err
	}
	if !metav1.IsControlledBy(obj, svc) {
		return nil
	}
	return client.IgnoreNotFound(r.Delete(ctx, obj))
}

func (r *InferenceServiceReconciler) deleteUnstructuredIfOwned(ctx context.Context, svc *servingv1alpha1.InferenceService, gvk schema.GroupVersionKind) error {
	u := &unstructured.Unstructured{}
	u.SetGroupVersionKind(gvk)
	return r.deleteIfOwned(ctx, svc, u, svc.Name)
}

func policyViolation(svc *servingv1alpha1.InferenceService) string {
	s := svc.Spec
	switch {
	case s.Classification == servingv1alpha1.ClassificationRestricted && s.DeviceClass == "":
		return "Restricted classification requires an explicit deviceClass"
	case s.Backend == servingv1alpha1.BackendOllama && s.Classification == servingv1alpha1.ClassificationRestricted:
		return "Ollama backend is not approved for Restricted data"
	case s.Backend != servingv1alpha1.BackendOllama && s.DeviceClass == "":
		return "vLLM backend requires a deviceClass: the vLLM image is built for CUDA and does not serve on CPU"
	}
	return ""
}

func (r *InferenceServiceReconciler) condition(svc *servingv1alpha1.InferenceService, t string, s metav1.ConditionStatus, reason, msg string) {
	meta.SetStatusCondition(&svc.Status.Conditions, metav1.Condition{
		Type: t, Status: s, Reason: reason, Message: msg, ObservedGeneration: svc.Generation,
	})
}

// SetupWithManager registers the watches. The KEDA and InferencePool watches
// are added only when their CRDs exist at start-up, because a watch on a
// missing kind stops the manager from starting.
func (r *InferenceServiceReconciler) SetupWithManager(mgr ctrl.Manager) error {
	b := ctrl.NewControllerManagedBy(mgr).
		For(&servingv1alpha1.InferenceService{}).
		Owns(&appsv1.Deployment{}).
		Owns(&corev1.Service{}).
		Owns(&resourcev1.ResourceClaimTemplate{})
	for _, gvk := range []schema.GroupVersionKind{scaledObjectGVK, inferencePoolGVK} {
		if _, err := mgr.GetRESTMapper().RESTMapping(gvk.GroupKind(), gvk.Version); err != nil {
			mgr.GetLogger().Info("CRD not installed, not watching it", "kind", gvk.Kind)
			continue
		}
		u := &unstructured.Unstructured{}
		u.SetGroupVersionKind(gvk)
		b = b.Owns(u)
	}
	return b.Complete(r)
}
