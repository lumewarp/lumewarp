# Plain Kubernetes manifests

`base/` is the `lumewarp-stack` Helm chart rendered with `values.yaml`: PostgreSQL, OpenFGA, the control plane and a gateway in namespace `lumewarp`, both schema migrations as Jobs, every credential a Secret referenced by name. Regenerate it with `generate.sh` (CI fails on drift). `overlays/example/` shows an image tag, gateway replicas and SSO env.
Create the Secrets with `create-secrets.sh`, then `kubectl apply -k deploy/kubernetes/base` (or the `lumewarp-<version>.yaml` release asset).
The full walkthrough (Secrets, bootstrap order, upgrades) is in [docs/operations/kubernetes.md](../../docs/operations/kubernetes.md).
