# Plain Kubernetes manifests

`base/` is the `lumewarp` Helm chart rendered with `values.yaml` (namespace `lumewarp`, Secrets referenced by name, no monitoring CRDs, no Valkey) and split one file per kind; regenerate it with `deploy/kubernetes/generate.sh` (CI fails on drift). `overlays/example/` shows an image tag, gateway replicas and SSO env.
Apply with `kubectl apply -k deploy/kubernetes/overlays/example` after creating the Secrets, or use the `lumewarp-<version>.yaml` release asset.
The full walkthrough (Secrets, bootstrap order, the migration Job) is in [docs/operations/kubernetes.md](../../docs/operations/kubernetes.md).
