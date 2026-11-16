#!/usr/bin/env bash
# Render and lint the chart for a matrix of value sets and assert the shape of the result:
# the workload kind (Deployment vs StatefulSet), the replica count, the autoscaler and the
# disruption budget, every shared-limit environment variable, and that no password lands in
# a manifest. Uses helm from PATH, or the alpine/helm image through docker.
#
#   bash deploy/helm/check.sh
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

if command -v helm >/dev/null 2>&1; then
  helm_run=(helm)
  chart_arg="$here/lumewarp"
else
  if ! command -v docker >/dev/null 2>&1; then
    echo "check.sh needs helm on PATH, or docker for alpine/helm:3.16.4" >&2
    exit 1
  fi
  # Values arrive as --set, so the chart directory is the only mount.
  helm_run=(docker run --rm -v "$here:/helm:ro" alpine/helm:3.16.4)
  chart_arg=/helm/lumewarp
fi

failures=0
ok() { echo "ok   $*"; }
bad() {
  echo "FAIL $*"
  failures=$((failures + 1))
}

# render NAME ARGS... renders one case into $file, after linting the same values.
render() {
  local name="$1"
  shift
  file="$tmp/$name.yaml"
  if ! "${helm_run[@]}" lint -n lumewarp "$chart_arg" --strict "$@" >"$tmp/$name.lint" 2>&1; then
    bad "$name: helm lint --strict failed: $(tr '\n' ' ' <"$tmp/$name.lint" | cut -c1-300)"
  fi
  if "${helm_run[@]}" template lumewarp "$chart_arg" -n lumewarp "$@" >"$file" 2>"$file.err"; then
    echo "case $name"
  else
    bad "$name: rendered with an error: $(tr '\n' ' ' <"$file.err" | cut -c1-300)"
    : >"$file"
  fi
}

# reject NAME PATTERN ARGS... asserts that rendering fails with a message matching PATTERN.
reject() {
  local name="$1" pattern="$2"
  shift 2
  local out="$tmp/reject-$name.log"
  if "${helm_run[@]}" template lumewarp "$chart_arg" -n lumewarp "$@" >"$out" 2>&1; then
    bad "$name: accepted a value set that must be rejected"
  elif grep -qE "$pattern" "$out"; then
    echo "case reject-$name (expected failure)"
  else
    bad "$name: rejected for another reason: $(tr '\n' ' ' <"$out" | cut -c1-300)"
  fi
}

# doc TEMPLATE prints the document rendered from one template file (empty if it renders none).
doc() {
  sed -n "/^# Source: .*\/templates\/$1\$/,/^---\$/p" "$file"
}

expect() { # DESCRIPTION, ERE, TEXT
  if printf '%s\n' "$3" | grep -qE "$2"; then ok "$1"; else bad "$1"; fi
}
refute() { # DESCRIPTION, ERE, TEXT
  if printf '%s\n' "$3" | grep -qE "$2"; then bad "$1"; else ok "$1"; fi
}

# --- defaults: unchanged Deployment, one replica, emptyDir -------------------------------
render defaults
workload="$(doc gateway-deployment.yaml)"
whole="$(cat "$file")"
expect "defaults: gateway is a Deployment" '^kind: Deployment' "$workload"
expect "defaults: one replica" '^  replicas: 1$' "$workload"
expect "defaults: per-pod emptyDir for the state directory" '^          emptyDir: \{\}$' "$workload"
expect "defaults: readiness stays on /readyz" 'path: /readyz' "$workload"
refute "defaults: no StatefulSet" '^kind: StatefulSet' "$whole"
refute "defaults: no volumeClaimTemplates" 'volumeClaimTemplates' "$whole"
refute "defaults: no PersistentVolumeClaim" '^kind: PersistentVolumeClaim' "$whole"
refute "defaults: no HorizontalPodAutoscaler" '^kind: HorizontalPodAutoscaler' "$whole"
refute "defaults: no PodDisruptionBudget" '^kind: PodDisruptionBudget' "$whole"
refute "defaults: no Valkey" 'valkey/valkey' "$whole"
refute "defaults: no shared-limit settings" 'LUMEWARP_SHARED_LIMITS_' "$whole"

# --- replicas ----------------------------------------------------------------------------
render replicas --set gateway.replicaCount=3
workload="$(doc gateway-deployment.yaml)"
expect "replicas=3: three replicas" '^  replicas: 3$' "$workload"
expect "replicas=3: still a Deployment" '^kind: Deployment' "$workload"
expect "replicas=3: still an emptyDir" 'emptyDir: \{\}' "$workload"

# --- autoscaling: HPA, and the workload carries no replica count --------------------------
render autoscaling --set gateway.autoscaling.enabled=true
hpa="$(doc gateway-hpa.yaml)"
workload="$(doc gateway-deployment.yaml)"
expect "autoscaling: HPA rendered" '^kind: HorizontalPodAutoscaler' "$hpa"
expect "autoscaling: scales the gateway Deployment" 'kind: Deployment' "$hpa"
expect "autoscaling: minReplicas" '^  minReplicas: 2$' "$hpa"
expect "autoscaling: maxReplicas" '^  maxReplicas: 5$' "$hpa"
expect "autoscaling: CPU target" 'averageUtilization: 80' "$hpa"
refute "autoscaling: the workload has no replicas" '^  replicas:' "$workload"

# --- persistence: StatefulSet, one claim per pod, governing headless Service --------------
render persistence --set gateway.persistence.enabled=true \
  --set gateway.persistence.storageClass=fast \
  --set gateway.persistence.size=5Gi
workload="$(doc gateway-statefulset.yaml)"
whole="$(cat "$file")"
replaced="$(doc gateway-deployment.yaml)"
expect "persistence: gateway is a StatefulSet" '^kind: StatefulSet' "$workload"
expect "persistence: governing Service" '^  serviceName: lumewarp-gateway-headless$' "$workload"
expect "persistence: volumeClaimTemplates" '^  volumeClaimTemplates:$' "$workload"
expect "persistence: ReadWriteOnce" '^          - ReadWriteOnce$' "$workload"
expect "persistence: storageClass" '^        storageClassName: "fast"$' "$workload"
expect "persistence: size" '^            storage: 5Gi$' "$workload"
refute "persistence: no Deployment left" '^kind: Deployment' "$replaced"
expect "persistence: headless Service is headless" '^  clusterIP: None$' "$whole"
expect "persistence: headless Service is named" '^  name: lumewarp-gateway-headless$' "$whole"
refute "persistence: no standalone PVC object" '^kind: PersistentVolumeClaim' "$whole"
refute "persistence: no state volume in the pod spec" '^        - name: state$' "$workload"
# A StatefulSet rejects most edits to volumeClaimTemplates, so the claim template may only
# carry labels that never change between releases.
claims="$(sed -n '/^  volumeClaimTemplates:$/,/^---$/p' "$file")"
refute "persistence: claim template has no chart-version label" 'helm.sh/chart' "$claims"
refute "persistence: claim template has no app version label" 'app.kubernetes.io/version' "$claims"
expect "persistence: claim template keeps the selector labels" '^          app.kubernetes.io/component: gateway$' "$claims"

# --- persistence plus the ServiceMonitor: one scrape target per pod, not two --------------
render persistence-monitor --set gateway.persistence.enabled=true \
  --set monitoring.serviceMonitor.enabled=true
whole="$(cat "$file")"
headless="$(sed -n '/^  name: lumewarp-gateway-headless$/,/^---$/p' "$file")"
expect "headless Service exposes http" '^    - name: http$' "$headless"
refute "headless Service does not expose admin" '^    - name: admin$' "$headless"
admin_services="$(grep -cE '^    - name: admin$' "$file")"
if [ "$admin_services" = 1 ]; then
  ok "persistence + ServiceMonitor: exactly one Service exposes the admin port"
else
  bad "persistence + ServiceMonitor: $admin_services Services expose the admin port, expected 1 (Prometheus would scrape every pod twice)"
fi
expect "persistence + ServiceMonitor: the monitor scrapes the admin port" '^    - port: admin$' \
  "$(doc servicemonitor.yaml)"

# --- bundled Valkey ----------------------------------------------------------------------
render valkey --set valkey.enabled=true --set gateway.replicaCount=3 \
  --set gateway.sharedLimits.syncMs=200 --set gateway.sharedLimits.namespace=prod
workload="$(doc gateway-deployment.yaml)"
valkey="$(doc valkey-deployment.yaml)"
whole="$(cat "$file")"
expect "valkey: Valkey Deployment" '^  name: lumewarp-valkey$' "$valkey"
expect "valkey: pinned image" 'image: "valkey/valkey:8.1.11-alpine"' "$valkey"
expect "valkey: no persistence" '\-\-save "" --appendonly no' "$valkey"
expect "valkey: password read from the mounted Secret" 'password="\$\(cat /etc/lumewarp-valkey/password\)"' "$valkey"
expect "valkey: config file on the writable data volume" "printf 'requirepass %s" "$valkey"
expect "valkey: server started from the config file" 'exec valkey-server /data/valkey.conf' "$valkey"
expect "valkey: non-root" '^ +runAsUser: 999$' "$valkey"
expect "valkey: read-only root filesystem" '^ +readOnlyRootFilesystem: true$' "$valkey"
expect "valkey: /data is an emptyDir" '^          emptyDir: \{\}$' "$valkey"
expect "valkey: ClusterIP Service" '^  name: lumewarp-valkey$' "$(doc valkey-service.yaml)"
expect "valkey: gateway URL built from the Service" 'value: "redis://:\$\(LUMEWARP_SHARED_LIMITS_REDIS_PASSWORD\)@lumewarp-valkey:6379/0"' "$workload"
expect "valkey: password from the Secret" 'name: lumewarp-valkey' "$workload"
expect "valkey: chart-created Secret" '^  password: "' "$(doc valkey-secret.yaml)"
expect "valkey: sync interval" '^              value: "200"$' "$workload"
expect "valkey: namespace" '^              value: "prod"$' "$workload"
refute "valkey: no literal password in the URL" 'redis://:[^$]' "$whole"
refute "valkey: the password is not a command-line argument" '\-\-requirepass' "$whole"

# The kubelet expands $(VAR) from the variables declared before it, so the password has to
# come first.
password_line="$(grep -n 'name: LUMEWARP_SHARED_LIMITS_REDIS_PASSWORD' "$file" | head -1 | cut -d: -f1)"
url_line="$(grep -n 'name: LUMEWARP_SHARED_LIMITS_REDIS_URL' "$file" | head -1 | cut -d: -f1)"
if [ -n "$password_line" ] && [ -n "$url_line" ] && [ "$password_line" -lt "$url_line" ]; then
  ok "valkey: the password variable is declared before the URL that interpolates it"
else
  bad "valkey: the URL must follow the password variable in the container env"
fi

# The generated password exists once, base64, in the Secret and nowhere in plain text.
password_b64="$(doc valkey-secret.yaml | sed -n 's/^  password: "\(.*\)"$/\1/p' | head -1)"
if [ -n "$password_b64" ]; then
  password="$(printf '%s' "$password_b64" | base64 -d 2>/dev/null)"
  if [ -n "$password" ] && grep -qF -- "$password" "$file"; then
    bad "valkey: the generated password appears in plain text in the rendered output"
  else
    ok "valkey: the generated password appears only base64-encoded in the Secret"
  fi
else
  bad "valkey: could not read the generated password from the Secret"
fi

# --- Valkey with a password Secret the operator supplies ----------------------------------
render valkey-existing-secret --set valkey.enabled=true --set valkey.auth.existingSecret=valkey-auth \
  --set gateway.replicaCount=2
whole="$(cat "$file")"
expect "valkey existingSecret: mounts the named Secret" '^            secretName: valkey-auth$' "$whole"
refute "valkey existingSecret: the chart creates no Secret" '^kind: Secret' "$whole"

# --- external Redis ----------------------------------------------------------------------
render external-redis --set gateway.sharedLimits.redis.existingSecret=external-redis \
  --set gateway.sharedLimits.redis.existingSecretKey=redis-url --set gateway.replicaCount=2
workload="$(doc gateway-deployment.yaml)"
whole="$(cat "$file")"
expect "external redis: URL from the Secret" 'name: external-redis' "$workload"
expect "external redis: named key" '^                  key: redis-url$' "$workload"
refute "external redis: no Valkey image" 'valkey/valkey' "$whole"
refute "external redis: the chart creates no Secret" '^kind: Secret' "$whole"
refute "external redis: no requirepass anywhere" 'requirepass' "$whole"

# --- disruption budget -------------------------------------------------------------------
render pdb --set gateway.replicaCount=3 --set gateway.podDisruptionBudget.enabled=true
pdb="$(doc gateway-poddisruptionbudget.yaml)"
expect "pdb: rendered with 3 replicas" '^kind: PodDisruptionBudget' "$pdb"
expect "pdb: minAvailable" '^  minAvailable: 1$' "$pdb"

render pdb-max --set gateway.replicaCount=2 --set gateway.podDisruptionBudget.enabled=true \
  --set-string gateway.podDisruptionBudget.minAvailable= \
  --set gateway.podDisruptionBudget.maxUnavailable=1
pdb="$(doc gateway-poddisruptionbudget.yaml)"
expect "pdb maxUnavailable: rendered" '^  maxUnavailable: 1$' "$pdb"
refute "pdb maxUnavailable: no minAvailable" 'minAvailable' "$pdb"

render pdb-autoscaling --set gateway.autoscaling.enabled=true \
  --set gateway.podDisruptionBudget.enabled=true
expect "pdb: rendered for an autoscaled gateway" '^kind: PodDisruptionBudget' \
  "$(doc gateway-poddisruptionbudget.yaml)"

# --- everything at once ------------------------------------------------------------------
render combined --set valkey.enabled=true --set gateway.persistence.enabled=true \
  --set gateway.replicaCount=3 --set gateway.podDisruptionBudget.enabled=true
whole="$(cat "$file")"
expect "combined: StatefulSet" '^kind: StatefulSet' "$(doc gateway-statefulset.yaml)"
expect "combined: PodDisruptionBudget" '^kind: PodDisruptionBudget' \
  "$(doc gateway-poddisruptionbudget.yaml)"
expect "combined: Valkey" 'image: "valkey/valkey:8.1.11-alpine"' "$(doc valkey-deployment.yaml)"
expect "combined: shared-limit URL" 'LUMEWARP_SHARED_LIMITS_REDIS_URL' \
  "$(doc gateway-statefulset.yaml)"
expect "combined: 3 replicas" '^  replicas: 3$' "$(doc gateway-statefulset.yaml)"

# --- OIDC: environment rendered only when set ---------------------------------------------
render oidc-off
refute "oidc off: no OIDC variables" 'LUMEWARP_OIDC_|LUMEWARP_SECRET_KEY|LUMEWARP_PUBLIC_URL' "$(cat "$file")"
render oidc --set controlPlane.oidc.issuer=https://idp.example.com \
  --set controlPlane.oidc.clientId=lumewarp \
  --set controlPlane.oidc.clientSecret.secretName=oidc \
  --set controlPlane.oidc.secretKey.secretName=oidc \
  --set controlPlane.oidc.secretKey.key=session-key \
  --set controlPlane.oidc.publicUrl=https://lw.example.com
cp="$(doc control-plane-deployment.yaml)"
expect "oidc: issuer" '^ +value: "https://idp.example.com"$' "$cp"
expect "oidc: client id" 'name: LUMEWARP_OIDC_CLIENT_ID' "$cp"
expect "oidc: client secret from the Secret" 'name: oidc' "$cp"
expect "oidc: secret key from the named key" 'key: session-key' "$cp"
expect "oidc: public URL" '^ +value: "https://lw.example.com"$' "$cp"
expect "oidc: LUMEWARP_SECRET_KEY" 'name: LUMEWARP_SECRET_KEY' "$cp"
render oidc-public-client --set controlPlane.oidc.issuer=https://idp.example.com \
  --set controlPlane.oidc.clientId=lumewarp --set controlPlane.oidc.secretKey.secretName=oidc
cp="$(doc control-plane-deployment.yaml)"
refute "oidc public client: no client secret variable" 'LUMEWARP_OIDC_CLIENT_SECRET' "$cp"

# --- listen addresses follow the Service ports ---------------------------------------------
render ports --set gateway.service.port=8080 --set gateway.service.adminPort=9090
cm="$(doc gateway-configmap.yaml)"
expect "ports: listen derived from service.port" 'listen: "0.0.0.0:8080"' "$cm"
expect "ports: admin listen derived from service.adminPort" 'admin_listen: "0.0.0.0:9090"' "$cm"
expect "ports: container ports follow" '^ +containerPort: 9090$' "$(doc gateway-deployment.yaml)"
render defaults
cm="$(doc gateway-configmap.yaml)"
expect "defaults: listen on 4000" 'listen: "0.0.0.0:4000"' "$cm"
expect "defaults: admin listen on 9464" 'admin_listen: "0.0.0.0:9464"' "$cm"

# --- global: scope, image tag, plugins ------------------------------------------------------
render global --set global.scope=prod --set global.imageTag=9.9.9 \
  --set gateway.plugins.volume.persistentVolumeClaim.claimName=plug
whole="$(cat "$file")"
expect "global: scope on the control plane" '^ +value: "prod"$' "$(doc control-plane-deployment.yaml)"
expect "global: scope on the gateway" '^ +value: "prod"$' "$(doc gateway-deployment.yaml)"
expect "global: imageTag on both components" 'lumewarp-gateway:9.9.9' "$whole"
expect "global: imageTag on the control plane" 'lumewarp-control-plane:9.9.9' "$whole"
expect "plugins: fixed directories" 'value: "/etc/lumewarp/plugins/a2a-middleware"' "$(doc gateway-deployment.yaml)"

# --- rejected value sets -----------------------------------------------------------------
reject redis-conflict 'mutually exclusive' --set valkey.enabled=true \
  --set gateway.sharedLimits.redis.existingSecret=external-redis
reject sync-ms-range 'syncMs must be an integer number of milliseconds from 10 to 1000' \
  --set gateway.sharedLimits.syncMs=5
reject sync-ms-type 'syncMs must be an integer number of milliseconds from 10 to 1000' \
  --set gateway.sharedLimits.syncMs=fast
reject pdb-both 'takes either minAvailable or maxUnavailable' --set gateway.replicaCount=2 \
  --set gateway.podDisruptionBudget.enabled=true \
  --set gateway.podDisruptionBudget.maxUnavailable=1
reject autoscaling-order 'maxReplicas must be greater than or equal to minReplicas' \
  --set gateway.autoscaling.enabled=true --set gateway.autoscaling.minReplicas=5 \
  --set gateway.autoscaling.maxReplicas=2
reject oidc-without-secret-key 'controlPlane.oidc.secretKey.secretName is required' \
  --set controlPlane.oidc.issuer=https://idp.example.com
reject controlplane-url-required 'gateway.controlPlaneUrl is required' --set controlPlane.enabled=false
reject openfga-url-required 'openfga.url is required' --set controlPlane.authz.backend=openfga

if [ "$failures" -ne 0 ]; then
  echo "$failures check(s) failed"
  exit 1
fi
echo "all chart render cases passed"
