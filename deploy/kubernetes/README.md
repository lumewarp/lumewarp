# Plain Kubernetes manifests

`manifests/` is the `lumewarp-stack` Helm chart rendered with `values.yaml`, one file per component: PostgreSQL, OpenFGA, the control plane and a gateway in namespace `lumewarp`, both schema migrations as Jobs, every credential a Secret referenced by name. Regenerate it with `generate.sh` (CI fails on drift).
Create the Secrets with `create-secrets.sh`, then `kubectl apply -f deploy/kubernetes/manifests/` (or the `lumewarp-<version>.yaml` release asset).
The full walkthrough (Secrets, bootstrap order, customizing, upgrades) is in [docs/operations/kubernetes.md](../../docs/operations/kubernetes.md).
