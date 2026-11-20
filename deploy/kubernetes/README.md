# Plain Kubernetes manifests

`manifests/` is the `lumewarp-stack` Helm chart rendered with `values.yaml`, one file per component: PostgreSQL, OpenFGA, the control plane and a gateway in namespace `lumewarp`, both schema migrations as Jobs, every credential a Secret referenced by name. Regenerate it with `generate.sh` (CI fails on drift).
Create the Secrets with `create-secrets.sh`, then `kubectl apply -f deploy/kubernetes/manifests/` (or the `lumewarp-<version>.yaml` release asset).
`create-secrets.sh` also creates `lumewarp-gateway-tokens` with the default zone's fixed
`LUMEWARP_GATEWAY_ZONE_DEFAULT_TOKEN`; both components read it. The full walkthrough
(Secrets, first sign-in, customizing, upgrades) is in
[docs/operations/kubernetes.md](../../docs/operations/kubernetes.md).
