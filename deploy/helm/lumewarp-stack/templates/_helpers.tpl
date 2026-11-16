{{- define "stack.postgres.name" -}}
{{- printf "%s-postgres" .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "stack.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/name: lumewarp-stack
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- with .Values.global.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{- define "stack.postgres.labels" -}}
{{ include "stack.labels" . }}
app.kubernetes.io/component: postgres
{{- end }}

{{- define "stack.postgres.selectorLabels" -}}
app.kubernetes.io/name: lumewarp-stack
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: postgres
{{- end }}

{{/* The generated password Secret; the Secret keeps its value across upgrades (lookup). */}}
{{- define "stack.postgres.secretName" -}}
{{- default (printf "%s-postgres" .Release.Name) .Values.postgres.auth.existingSecret }}
{{- end }}

{{- define "stack.postgres.secretKey" -}}
{{- if .Values.postgres.auth.existingSecret }}{{ .Values.postgres.auth.existingSecretKey }}{{ else }}password{{ end }}
{{- end }}

{{/*
The password: the existing Secret's value, or a new random one. `helm template` and
`helm install --dry-run` have no cluster to ask and render a new random value.
*/}}
{{- define "stack.postgres.password" -}}
{{- $existing := lookup "v1" "Secret" .Release.Namespace (printf "%s-postgres" .Release.Name) }}
{{- if and $existing (hasKey ($existing.data | default (dict)) "password") }}
{{- index $existing.data "password" | b64dec }}
{{- else }}
{{- randAlphaNum 32 }}
{{- end }}
{{- end }}

{{- define "stack.controlPlane.image" -}}
{{- $cp := .Values.lumewarp.controlPlane.image -}}
{{- printf "%s:%s" $cp.repository (default (default .Chart.AppVersion .Values.global.imageTag) $cp.tag) }}
{{- end }}

{{/* Names of the lumewarp subchart's workloads (its fullname rule, as the chart is unaliased). */}}
{{- define "stack.lumewarpFullname" -}}
{{- $v := .Values.lumewarp -}}
{{- if $v.fullnameOverride }}{{ $v.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else if contains (default "lumewarp" $v.nameOverride) .Release.Name }}{{ .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}{{ printf "%s-%s" .Release.Name (default "lumewarp" $v.nameOverride) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- define "stack.controlPlaneName" -}}{{ printf "%s-control-plane" (include "stack.lumewarpFullname" .) | trunc 63 | trimSuffix "-" }}{{- end }}
{{- define "stack.gatewayName" -}}{{ printf "%s-gateway" (include "stack.lumewarpFullname" .) | trunc 63 | trimSuffix "-" }}{{- end }}
