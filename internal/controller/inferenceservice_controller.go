package controller

import (
	"context"
	"fmt"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	servingv1alpha1 "github.com/lake-of-dreams/llm-inference-platform/api/v1alpha1"
)

// Resync floor. Watches catch nearly everything. This is for out-of-band edits
// that raise no event on an object we own.
const driftRequeue = 5 * time.Minute

type InferenceServiceReconciler struct {
	client.Client
	Scheme *runtime.Scheme
}

// +kubebuilder:rbac:groups=serving.platform.io,resources=inferenceservices,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=serving.platform.io,resources=inferenceservices/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=apps,resources=deployments,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=core,resources=services,verbs=get;list;watch;create;update;patch;delete

func (r *InferenceServiceReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	l := log.FromContext(ctx)

	var svc servingv1alpha1.InferenceService
	if err := r.Get(ctx, req.NamespacedName, &svc); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	// Restricted weights do not go onto whatever node happens to have room, so
	// a deviceClass is required. The CRD's CEL rule says the same thing, but the
	// CRD installed in a cluster is not always the one we shipped.
	if svc.Spec.Classification == servingv1alpha1.ClassificationRestricted && svc.Spec.DeviceClass == "" {
		r.setCondition(&svc, "Admitted", metav1.ConditionFalse, "PolicyViolation",
			"Restricted classification requires an explicit deviceClass")
		svc.Status.Phase = "Rejected"
		_ = r.Status().Update(ctx, &svc)
		// nothing to requeue for until the spec changes
		return ctrl.Result{}, nil
	}

	desired := r.desiredDeployment(&svc)
	if err := ctrl.SetControllerReference(&svc, desired, r.Scheme); err != nil {
		return ctrl.Result{}, err
	}

	var current appsv1.Deployment
	err := r.Get(ctx, types.NamespacedName{Name: desired.Name, Namespace: desired.Namespace}, &current)
	switch {
	case apierrors.IsNotFound(err):
		l.Info("creating deployment", "name", desired.Name)
		if err := r.Create(ctx, desired); err != nil {
			return ctrl.Result{}, err
		}
	case err != nil:
		return ctrl.Result{}, err
	default:
		// Don't rewrite an identical spec on every loop. Beyond the wasted API
		// server calls, a blind update puts .spec.replicas back to minReplicas and
		// KEDA immediately scales up again. Replicas is out of the comparison and
		// never written after creation. ADR-0004.
		if specEquivalent(&current, desired) {
			return r.finish(ctx, &svc, &current)
		}
		l.Info("drift detected, patching", "name", desired.Name)
		patched := current.DeepCopy()
		patched.Spec.Template = desired.Spec.Template
		patched.Spec.Selector = desired.Spec.Selector
		if err := r.Update(ctx, patched); err != nil {
			return ctrl.Result{}, err
		}
		current = *patched
	}

	if err := r.ensureService(ctx, &svc); err != nil {
		return ctrl.Result{}, err
	}
	return r.finish(ctx, &svc, &current)
}

func (r *InferenceServiceReconciler) finish(ctx context.Context, svc *servingv1alpha1.InferenceService, dep *appsv1.Deployment) (ctrl.Result, error) {
	svc.Status.ObservedGeneration = int32(svc.Generation)
	svc.Status.ReadyReplicas = dep.Status.ReadyReplicas
	svc.Status.Endpoint = fmt.Sprintf("http://%s.%s.svc.cluster.local:8000/v1", svc.Name, svc.Namespace)
	if dep.Status.ReadyReplicas > 0 {
		svc.Status.Phase = "Ready"
		r.setCondition(svc, "Ready", metav1.ConditionTrue, "MinimumReplicasAvailable", "serving traffic")
	} else {
		svc.Status.Phase = "Loading"
		// running but not ready. normal for minutes while weights load
		r.setCondition(svc, "Ready", metav1.ConditionFalse, "WeightsLoading", "no ready replicas yet")
	}
	if err := r.Status().Update(ctx, svc); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{RequeueAfter: driftRequeue}, nil
}

func (r *InferenceServiceReconciler) setCondition(svc *servingv1alpha1.InferenceService, t string, s metav1.ConditionStatus, reason, msg string) {
	for i := range svc.Status.Conditions {
		if svc.Status.Conditions[i].Type == t {
			if svc.Status.Conditions[i].Status != s {
				svc.Status.Conditions[i].LastTransitionTime = metav1.Now()
			}
			svc.Status.Conditions[i].Status = s
			svc.Status.Conditions[i].Reason = reason
			svc.Status.Conditions[i].Message = msg
			svc.Status.Conditions[i].ObservedGeneration = svc.Generation
			return
		}
	}
	svc.Status.Conditions = append(svc.Status.Conditions, metav1.Condition{
		Type: t, Status: s, Reason: reason, Message: msg,
		LastTransitionTime: metav1.Now(), ObservedGeneration: svc.Generation,
	})
}

func (r *InferenceServiceReconciler) ensureService(ctx context.Context, svc *servingv1alpha1.InferenceService) error {
	desired := &corev1.Service{
		ObjectMeta: metav1.ObjectMeta{Name: svc.Name, Namespace: svc.Namespace, Labels: labels(svc)},
		Spec: corev1.ServiceSpec{
			Selector: labels(svc),
			Ports:    []corev1.ServicePort{{Name: "http", Port: 8000, TargetPort: intstrFromInt(8000)}},
		},
	}
	if err := ctrl.SetControllerReference(svc, desired, r.Scheme); err != nil {
		return err
	}
	var cur corev1.Service
	err := r.Get(ctx, types.NamespacedName{Name: desired.Name, Namespace: desired.Namespace}, &cur)
	if apierrors.IsNotFound(err) {
		return r.Create(ctx, desired)
	}
	return err
}

func (r *InferenceServiceReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&servingv1alpha1.InferenceService{}).
		Owns(&appsv1.Deployment{}).
		Owns(&corev1.Service{}).
		Complete(r)
}

func labels(svc *servingv1alpha1.InferenceService) map[string]string {
	return map[string]string{
		"app.kubernetes.io/name":        "inference-service",
		"app.kubernetes.io/instance":    svc.Name,
		"serving.platform.io/backend":   string(svc.Spec.Backend),
		"serving.platform.io/dataclass": string(svc.Spec.Classification),
	}
}

func qty(s string) resource.Quantity { return resource.MustParse(s) }
