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

{{/*
Gateway credentials. The control plane reads every LUMEWARP_GATEWAY_ZONE_<ID>_TOKEN in
one Secret; each gateway pod reads the key of the zone it serves. `global.zone` is always
included, and `global.gatewayTokens.zones` adds more zones.
*/}}
{{- define "lumewarp.gatewayTokenZones" -}}
{{- join "," (uniq (concat (list .Values.global.zone) .Values.global.gatewayTokens.zones)) -}}
{{- end -}}

{{- define "lumewarp.gatewayTokenKey" -}}
LUMEWARP_GATEWAY_ZONE_{{ .zone | upper | replace "-" "_" }}_TOKEN
{{- end -}}

{{- define "lumewarp.gatewayTokens.secretName" -}}
{{- default (printf "%s-gateway-tokens" (include "lumewarp.fullname" .)) .Values.global.gatewayTokens.secretName -}}
{{- end -}}

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
{{- with .Values.global.commonLabels }}
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
The headless Service that governs the gateway StatefulSet (gateway.persistence).
*/}}
{{- define "lumewarp.gateway.headlessServiceName" -}}
{{- printf "%s-headless" (include "lumewarp.gateway.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "lumewarp.valkey.name" -}}
{{- printf "%s-valkey" (include "lumewarp.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "lumewarp.valkey.labels" -}}
{{ include "lumewarp.labels" . }}
app.kubernetes.io/component: valkey
{{- end }}

{{- define "lumewarp.valkey.selectorLabels" -}}
{{ include "lumewarp.selectorLabels" . }}
app.kubernetes.io/component: valkey
{{- end }}

{{/*
The Secret holding the Valkey password: the one the chart creates, or valkey.auth.existingSecret.
*/}}
{{- define "lumewarp.valkey.secretName" -}}
{{- default (include "lumewarp.valkey.name" .) .Values.valkey.auth.existingSecret }}
{{- end }}

{{- define "lumewarp.valkey.secretKey" -}}
{{- default "password" .Values.valkey.auth.existingSecretKey }}
{{- end }}

{{/*
The control-plane image reference.
*/}}
{{- define "lumewarp.controlPlane.image" -}}
{{- printf "%s:%s" .Values.controlPlane.image.repository (default (default .Chart.AppVersion .Values.global.imageTag) .Values.controlPlane.image.tag) }}
{{- end }}

{{/*
The gateway image reference.
*/}}
{{- define "lumewarp.gateway.image" -}}
{{- printf "%s:%s" .Values.gateway.image.repository (default (default .Chart.AppVersion .Values.global.imageTag) .Values.gateway.image.tag) }}
{{- end }}

{{/*
Authorization backend env for the control plane and its `openfga init` container.
*/}}
{{- define "lumewarp.controlPlane.authzEnv" -}}
{{- $authz := .Values.controlPlane.authz -}}
{{- if ne $authz.backend "builtin" }}
- name: LUMEWARP_AUTHZ_BACKEND
  value: {{ $authz.backend | quote }}
{{- end }}
{{- if eq $authz.backend "openfga" }}
{{- $fga := $authz.openfga }}
- name: LUMEWARP_OPENFGA_URL
  value: {{ required "controlPlane.authz.openfga.url is required for the openfga backend" $fga.url | quote }}
{{- if $fga.storeId }}
- name: LUMEWARP_OPENFGA_STORE_ID
  value: {{ $fga.storeId | quote }}
{{- else }}
- name: LUMEWARP_OPENFGA_STORE
  value: {{ required "controlPlane.authz.openfga.store or storeId is required" $fga.store | quote }}
{{- end }}
{{- if $fga.modelId }}
- name: LUMEWARP_OPENFGA_MODEL_ID
  value: {{ $fga.modelId | quote }}
{{- end }}
{{- if $fga.apiTokenSecret.name }}
- name: LUMEWARP_OPENFGA_API_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ $fga.apiTokenSecret.name }}
      key: {{ $fga.apiTokenSecret.key }}
{{- end }}
{{- with $fga.objectTypes }}
- name: LUMEWARP_OPENFGA_OBJECT_TYPES
  value: {{ toJson . | quote }}
{{- end }}
{{- with $fga.relations }}
- name: LUMEWARP_OPENFGA_RELATIONS
  value: {{ toJson . | quote }}
{{- end }}
{{- end }}
{{- end -}}
