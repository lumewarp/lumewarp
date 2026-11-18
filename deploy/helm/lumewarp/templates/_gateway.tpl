{{/*
The gateway workload, rendered once for both kinds so a change cannot land in one and be
missed in the other:

- gateway-deployment.yaml   the state directory is an emptyDir, one per pod (the default)
- gateway-statefulset.yaml  gateway.persistence.enabled: one ReadWriteOnce claim
                            per pod from a volumeClaimTemplate, so a restarted pod keeps
                            its cached snapshot

Every pod is a gateway: each process generates its own id at startup and they share one
token. Rate limits are counted per gateway unless they share a Redis (valkey.enabled or
gateway.sharedLimits.redis.existingSecret).

Usage: include "lumewarp.gateway.workload" (dict "root" $ "kind" "Deployment")
*/}}
{{- define "lumewarp.gateway.workload" -}}
{{- $root := .root -}}
{{- $kind := .kind -}}
{{- $stateful := eq $kind "StatefulSet" -}}
{{- $values := $root.Values.gateway -}}
{{- $persistence := $values.persistence -}}
{{- $autoscaling := $values.autoscaling -}}
{{- $sharedLimits := $values.sharedLimits -}}
{{- $valkey := $root.Values.valkey -}}
{{- if and $valkey.enabled $sharedLimits.redis.existingSecret -}}
{{- fail "valkey.enabled and gateway.sharedLimits.redis.existingSecret are mutually exclusive: the bundled Valkey and an external Redis URL cannot both configure the gateway" -}}
{{- end -}}
{{- $controlPlaneUrl := $values.controlPlaneUrl -}}
{{- if not $controlPlaneUrl -}}
{{- if not $root.Values.controlPlane.enabled -}}
{{- fail "gateway.controlPlaneUrl is required when controlPlane.enabled=false" -}}
{{- end -}}
{{- $scheme := ternary "https" "http" $root.Values.controlPlane.grpcTls.enabled -}}
{{- $controlPlaneUrl = printf "%s://%s:%v" $scheme (include "lumewarp.controlPlane.name" $root) $root.Values.controlPlane.service.grpcPort -}}
{{- end -}}
{{- $zone := $root.Values.global.zone -}}
{{- $plugins := $values.plugins -}}
{{- $pluginMountPath := "/etc/lumewarp/plugins" -}}
{{- $pluginDirs := list
      (dict "var" "LUMEWARP_ROUTER_PLUGIN_DIR" "subPath" "router")
      (dict "var" "LUMEWARP_AUTH_PLUGIN_DIR" "subPath" "auth")
      (dict "var" "LUMEWARP_A2A_MIDDLEWARE_DIR" "subPath" "a2a-middleware") }}
{{- if $plugins.volume }}
{{- if not (kindIs "map" $plugins.volume) }}
{{- fail "gateway.plugins.volume must be a Kubernetes volume source map, for example {persistentVolumeClaim: {claimName: gateway-plugins}}, or empty for no plugins" }}
{{- end }}
{{- end }}
{{- $memoryLimit := "" }}
{{- if not (and (kindIs "string" $plugins.memoryLimitBytes) (eq $plugins.memoryLimitBytes "")) }}
{{- $memoryLimit = toString $plugins.memoryLimitBytes }}
{{- if not (regexMatch "^[1-9][0-9]*$" $memoryLimit) }}
{{- fail (printf "gateway.plugins.memoryLimitBytes must be a positive integer number of bytes, got %s" $memoryLimit) }}
{{- end }}
{{- end }}
{{- /* A number from values.yaml, or a string from --set for a small decimal or scientific notation. Checked by value, since the spelling depends on how it got here. */}}
{{- $samplingValue := $values.otel.samplingRatio }}
{{- $samplingRatio := toString $samplingValue }}
{{- $samplingNumeric := or (kindIs "float64" $samplingValue) (kindIs "int" $samplingValue) (kindIs "int64" $samplingValue) (and (kindIs "string" $samplingValue) (regexMatch "^[+-]?([0-9]+(\\.[0-9]*)?|\\.[0-9]+)([eE][+-]?[0-9]+)?$" $samplingRatio)) }}
{{- if not $samplingNumeric }}
{{- fail (printf "gateway.otel.samplingRatio must be a number from 0 to 1, got %s" $samplingRatio) }}
{{- end }}
{{- if not (and (ge (float64 $samplingRatio) 0.0) (le (float64 $samplingRatio) 1.0)) }}
{{- fail (printf "gateway.otel.samplingRatio must be a number from 0 to 1, got %s" $samplingRatio) }}
{{- end }}
{{- $syncMs := toString $sharedLimits.syncMs }}
{{- if ne $syncMs "" }}
{{- if not (regexMatch "^[0-9]+$" $syncMs) }}
{{- fail (printf "gateway.sharedLimits.syncMs must be an integer number of milliseconds from 10 to 1000, got %s" $syncMs) }}
{{- end }}
{{- if not (and (ge (int $syncMs) 10) (le (int $syncMs) 1000)) }}
{{- fail (printf "gateway.sharedLimits.syncMs must be an integer number of milliseconds from 10 to 1000, got %s" $syncMs) }}
{{- end }}
{{- end }}
apiVersion: apps/v1
kind: {{ $kind }}
metadata:
  name: {{ include "lumewarp.gateway.name" $root }}
  labels:
    {{- include "lumewarp.gateway.labels" $root | nindent 4 }}
spec:
  {{- if not $autoscaling.enabled }}
  replicas: {{ $values.replicaCount }}
  {{- end }}
  {{- if $stateful }}
  # Required by a StatefulSet: the headless Service gives each pod a stable DNS name. It
  # carries no client traffic; the gateway Service does.
  serviceName: {{ include "lumewarp.gateway.headlessServiceName" $root }}
  # The pods are identical and independent copies of the same gateway, so they start
  # together instead of waiting for pod 0 to become Ready.
  podManagementPolicy: Parallel
  {{- end }}
  selector:
    matchLabels:
      {{- include "lumewarp.gateway.selectorLabels" $root | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "lumewarp.gateway.selectorLabels" $root | nindent 8 }}
      annotations:
        checksum/config: {{ include (print $root.Template.BasePath "/gateway-configmap.yaml") $root | sha256sum }}
        {{- with $values.podAnnotations }}
        {{- toYaml . | nindent 8 }}
        {{- end }}
    spec:
      automountServiceAccountToken: false
      {{- with $root.Values.global.imagePullSecrets }}
      imagePullSecrets:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- $podSecurityContext := mergeOverwrite (dict "runAsNonRoot" true "runAsUser" 65532 "runAsGroup" 65532 "fsGroup" 65532 "fsGroupChangePolicy" "OnRootMismatch") ($values.podSecurityContext | default (dict)) }}
      securityContext:
        {{- toYaml $podSecurityContext | nindent 8 }}
      terminationGracePeriodSeconds: {{ $values.terminationGracePeriodSeconds }}
      containers:
        - name: gateway
          image: {{ include "lumewarp.gateway.image" $root | quote }}
          imagePullPolicy: {{ $values.image.pullPolicy }}
          # The image must put the gateway binary on PATH.
          command:
            - lumewarp
          args:
            - --control-plane=$(LUMEWARP_CONTROL_PLANE_URL)
            - --gateway-token-env=LUMEWARP_GATEWAY_TOKEN
            - --state-dir=/var/lib/lumewarp
            - --zone=$(LUMEWARP_ZONE)
            - --local-config=/etc/lumewarp/config.yaml
          ports:
            - name: http
              containerPort: {{ $values.service.port }}
              protocol: TCP
            - name: admin
              containerPort: {{ $values.service.adminPort }}
              protocol: TCP
          env:
            - name: LUMEWARP_CONTROL_PLANE_URL
              value: {{ $controlPlaneUrl | quote }}
            - name: LUMEWARP_ZONE
              value: {{ $zone | quote }}
            - name: LUMEWARP_GATEWAY_TOKEN
              valueFrom:
                secretKeyRef:
                  name: {{ required "gateway.token.secretName is required" $values.token.secretName }}
                  key: {{ $values.token.key }}
            {{- with $values.otel.endpoint }}
            - name: OTEL_EXPORTER_OTLP_ENDPOINT
              value: {{ . | quote }}
            - name: OTEL_TRACES_SAMPLER
              value: traceidratio
            - name: OTEL_TRACES_SAMPLER_ARG
              value: {{ $samplingRatio | quote }}
            {{- end }}
            {{- if $root.Values.global.encryptionKey.secretName }}
            - name: LUMEWARP_ENCRYPTION_KEY
              valueFrom:
                secretKeyRef:
                  name: {{ $root.Values.global.encryptionKey.secretName }}
                  key: {{ $root.Values.global.encryptionKey.key }}
            {{- end }}
            {{- if $values.publicUrl }}
            - name: LUMEWARP_GATEWAY_PUBLIC_URL
              value: {{ $values.publicUrl | quote }}
            {{- end }}
            {{- if $values.idKey.secretName }}
            - name: LUMEWARP_GATEWAY_ID_KEY
              valueFrom:
                secretKeyRef:
                  name: {{ $values.idKey.secretName }}
                  key: {{ required "gateway.idKey.key is required when gateway.idKey.secretName is set" $values.idKey.key }}
            {{- end }}
            {{- if $plugins.volume }}
            {{- range $pluginDirs }}
            - name: {{ .var }}
              value: {{ printf "%s/%s" $pluginMountPath .subPath | quote }}
            {{- end }}
            {{- end }}
            {{- if $memoryLimit }}
            - name: LUMEWARP_PLUGIN_MEMORY_LIMIT_BYTES
              value: {{ $memoryLimit | quote }}
            {{- end }}
            {{- if $valkey.enabled }}
            # The password is interpolated by the kubelet from the variable above, so it
            # never appears in this manifest.
            - name: LUMEWARP_SHARED_LIMITS_REDIS_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: {{ include "lumewarp.valkey.secretName" $root }}
                  key: {{ include "lumewarp.valkey.secretKey" $root }}
            - name: LUMEWARP_SHARED_LIMITS_REDIS_URL
              value: {{ printf "redis://:$(LUMEWARP_SHARED_LIMITS_REDIS_PASSWORD)@%s:%v/0" (include "lumewarp.valkey.name" $root) $valkey.service.port | quote }}
            {{- else if $sharedLimits.redis.existingSecret }}
            - name: LUMEWARP_SHARED_LIMITS_REDIS_URL
              valueFrom:
                secretKeyRef:
                  name: {{ $sharedLimits.redis.existingSecret }}
                  key: {{ default "url" $sharedLimits.redis.existingSecretKey }}
            {{- end }}
            {{- if ne $syncMs "" }}
            - name: LUMEWARP_SHARED_LIMITS_SYNC_MS
              value: {{ $syncMs | quote }}
            {{- end }}
            {{- with $sharedLimits.namespace }}
            - name: LUMEWARP_SHARED_LIMITS_NAMESPACE
              value: {{ . | quote }}
            {{- end }}
            {{- with $values.extraEnv }}
            {{- toYaml . | nindent 12 }}
            {{- end }}
          {{- with $plugins.secretEnvs }}
          # Every key becomes LUMEWARP_PLUGIN_SECRET_<NAME>; see gateway.plugins.secretEnvs.
          envFrom:
            {{- range . }}
            - secretRef:
                name: {{ . | quote }}
            {{- end }}
          {{- end }}
          # /readyz is 503 until a valid snapshot is loaded (cold boot with the control
          # plane unreachable) or after a rejected snapshot. Liveness stays 200 so the
          # pod is not restarted for being unready.
          readinessProbe:
            httpGet:
              path: /readyz
              port: admin
            initialDelaySeconds: 2
            periodSeconds: 5
            timeoutSeconds: 2
            failureThreshold: 3
          livenessProbe:
            httpGet:
              path: /healthz
              port: admin
            initialDelaySeconds: 10
            periodSeconds: 10
            timeoutSeconds: 2
            failureThreshold: 3
          resources:
            {{- toYaml $values.resources | nindent 12 }}
          {{- with $values.securityContext }}
          securityContext:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          volumeMounts:
            - name: config
              mountPath: /etc/lumewarp
              readOnly: true
            - name: state
              mountPath: /var/lib/lumewarp
            {{- if $plugins.volume }}
            {{- range $pluginDirs }}
            - name: plugins
              mountPath: {{ printf "%s/%s" $pluginMountPath .subPath }}
              subPath: {{ .subPath }}
              readOnly: true
            {{- end }}
            {{- end }}
      volumes:
        - name: config
          configMap:
            name: {{ include "lumewarp.gateway.name" $root }}-config
        {{- if not $stateful }}
        - name: state
          emptyDir: {}
        {{- end }}
        {{- with $plugins.volume }}
        - name: plugins
          {{- toYaml . | nindent 10 }}
        {{- end }}
      {{- with $values.nodeSelector }}
      nodeSelector:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with $values.tolerations }}
      tolerations:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with $values.affinity }}
      affinity:
        {{- toYaml . | nindent 8 }}
      {{- end }}
  {{- if $stateful }}
  # One ReadWriteOnce claim per pod: the state directory is a cache, so the volume holds a
  # snapshot, not shared state. Only stable labels here: a StatefulSet rejects most template
  # edits, and helm.sh/chart or app.kubernetes.io/version would change on every upgrade.
  volumeClaimTemplates:
    - metadata:
        name: state
        labels:
          {{- include "lumewarp.gateway.selectorLabels" $root | nindent 10 }}
        {{- with $persistence.annotations }}
        annotations:
          {{- toYaml . | nindent 10 }}
        {{- end }}
      spec:
        accessModes:
          - ReadWriteOnce
        {{- with $persistence.storageClass }}
        storageClassName: {{ . | quote }}
        {{- end }}
        resources:
          requests:
            storage: {{ $persistence.size }}
  {{- end }}
{{- end }}
