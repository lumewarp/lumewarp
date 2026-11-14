{{/*
Expand the chart name.
*/}}
{{- define "lumewarp.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully qualified release name.
*/}}
{{- define "lumewarp.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "lumewarp.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels. Component-specific labels are added by the helpers below.
*/}}
{{- define "lumewarp.labels" -}}
helm.sh/chart: {{ include "lumewarp.chart" . }}
{{ include "lumewarp.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- with .Values.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{- define "lumewarp.selectorLabels" -}}
app.kubernetes.io/name: {{ include "lumewarp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "lumewarp.controlPlane.name" -}}
{{- printf "%s-control-plane" (include "lumewarp.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "lumewarp.controlPlane.labels" -}}
{{ include "lumewarp.labels" . }}
app.kubernetes.io/component: control-plane
{{- end }}

{{- define "lumewarp.controlPlane.selectorLabels" -}}
{{ include "lumewarp.selectorLabels" . }}
app.kubernetes.io/component: control-plane
{{- end }}

{{- define "lumewarp.gateway.name" -}}
{{- printf "%s-gateway" (include "lumewarp.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "lumewarp.gateway.labels" -}}
{{ include "lumewarp.labels" . }}
app.kubernetes.io/component: gateway
{{- end }}

{{- define "lumewarp.gateway.selectorLabels" -}}
{{ include "lumewarp.selectorLabels" . }}
app.kubernetes.io/component: gateway
{{- end }}

{{/*
The headless Service that governs the gateway StatefulSet (gateway.stateDir.persistence).
*/}}
{{- define "lumewarp.gateway.headlessServiceName" -}}
{{- printf "%s-headless" (include "lumewarp.gateway.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "lumewarp.redis.name" -}}
{{- printf "%s-redis" (include "lumewarp.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "lumewarp.redis.labels" -}}
{{ include "lumewarp.labels" . }}
app.kubernetes.io/component: redis
{{- end }}

{{- define "lumewarp.redis.selectorLabels" -}}
{{ include "lumewarp.selectorLabels" . }}
app.kubernetes.io/component: redis
{{- end }}

{{/*
The Secret holding the Valkey password: the one the chart creates, or redis.auth.existingSecret.
*/}}
{{- define "lumewarp.redis.secretName" -}}
{{- default (include "lumewarp.redis.name" .) .Values.redis.auth.existingSecret }}
{{- end }}

{{- define "lumewarp.redis.secretKey" -}}
{{- default "password" .Values.redis.auth.existingSecretKey }}
{{- end }}

{{/*
The control-plane image reference.
*/}}
{{- define "lumewarp.controlPlane.image" -}}
{{- printf "%s:%s" .Values.controlPlane.image.repository (default .Chart.AppVersion .Values.controlPlane.image.tag) }}
{{- end }}

{{/*
The gateway image reference.
*/}}
{{- define "lumewarp.gateway.image" -}}
{{- printf "%s:%s" .Values.gateway.image.repository (default .Chart.AppVersion .Values.gateway.image.tag) }}
{{- end }}
