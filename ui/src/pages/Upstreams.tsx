import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Upstream } from "../api";
import { Badge, PageHeader } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { CheckField, FormDialog, NumberField, required, SelectField, TextField, useForm } from "../components/Form";
import { CreateButton, RowActions } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { useCan, useMut } from "../hooks";

const KINDS = [
  { value: "vllm", label: "vLLM" },
  { value: "openai", label: "OpenAI" },
  { value: "openai_compatible", label: "OpenAI-compatible" },
];
const USAGE_MODES = [
  { value: "include_usage", label: "Inject stream_options.include_usage" },
  { value: "passthrough", label: "Pass through unchanged" },
];

interface Values {
  name: string;
  kind: Upstream["kind"];
  base_url: string;
  api_key_ref: string;
  timeout_ms: number | null;
  stream_usage_mode: Upstream["stream_usage_mode"];
  health_enabled: boolean;
  health_path: string;
  health_interval_ms: number | null;
  health_timeout_ms: number | null;
  health_unhealthy: number | null;
  health_healthy: number | null;
  breaker_enabled: boolean;
  breaker_failures: number | null;
  breaker_open_ms: number | null;
  metrics_url: string;
}
const blank: Values = {
  name: "",
  kind: "vllm",
  base_url: "",
  api_key_ref: "",
  timeout_ms: 600000,
  stream_usage_mode: "include_usage",
  health_enabled: false,
  health_path: "",
  health_interval_ms: 10000,
  health_timeout_ms: 2000,
  health_unhealthy: null,
  health_healthy: null,
  breaker_enabled: false,
  breaker_failures: null,
  breaker_open_ms: null,
  metrics_url: "",
};

const fromUpstream = (u: Upstream): Values => ({
  name: u.name,
  kind: u.kind,
  base_url: u.base_url,
  api_key_ref: u.api_key_ref ?? "",
  timeout_ms: u.timeout_ms,
  stream_usage_mode: u.stream_usage_mode,
  health_enabled: !!u.health_check,
  health_path: u.health_check?.path ?? "",
  health_interval_ms: u.health_check?.interval_ms ?? blank.health_interval_ms,
  health_timeout_ms: u.health_check?.timeout_ms ?? blank.health_timeout_ms,
  health_unhealthy: u.health_check?.unhealthy_threshold ?? null,
  health_healthy: u.health_check?.healthy_threshold ?? null,
  breaker_enabled: !!u.circuit_breaker,
  breaker_failures: u.circuit_breaker?.failure_threshold ?? null,
  breaker_open_ms: u.circuit_breaker?.open_ms ?? null,
  metrics_url: u.metrics_url ?? "",
});

const body = (v: Values) => ({
  name: v.name,
  kind: v.kind,
  base_url: v.base_url,
  api_key_ref: v.api_key_ref.trim() || null,
  timeout_ms: v.timeout_ms ?? 600000,
  stream_usage_mode: v.stream_usage_mode,
  health_check: v.health_enabled
    ? {
        path: v.health_path.trim() || null,
        interval_ms: v.health_interval_ms ?? 0,
        timeout_ms: v.health_timeout_ms ?? 0,
        unhealthy_threshold: v.health_unhealthy,
        healthy_threshold: v.health_healthy,
      }
    : null,
  circuit_breaker: v.breaker_enabled ? { failure_threshold: v.breaker_failures, open_ms: v.breaker_open_ms } : null,
  metrics_url: v.metrics_url.trim() || null,
});

/** Leaf field names of 422 messages ("health_check.interval_ms: …") mapped to form fields. */
const ALIAS = {
  path: "health_path",
  interval_ms: "health_interval_ms",
  unhealthy_threshold: "health_unhealthy",
  healthy_threshold: "health_healthy",
  health_check: "health_timeout_ms",
  failure_threshold: "breaker_failures",
  open_ms: "breaker_open_ms",
} as const;

const validUrl = (v: string) => {
  try {
    const u = new URL(v);
    return u.protocol === "http:" || u.protocol === "https:" ? undefined : "Must be an http(s) URL";
  } catch {
    return "Must be a valid URL";
  }
};

export function Upstreams() {
  const perms = useCan();
  const list = useQuery({ queryKey: ["upstreams"], queryFn: () => api.upstreams() });
  const [editing, setEditing] = useState<Upstream | "new" | null>(null);
  const [deleting, setDeleting] = useState<Upstream | null>(null);
  const form = useForm<Values>(blank, {
    name: required("Name"),
    base_url: (v) => (v.trim() ? validUrl(v) : "Base URL is required"),
    metrics_url: (v) => (v.trim() ? validUrl(v) : undefined),
    health_interval_ms: (v, all) => (all.health_enabled && (v ?? 0) < 1000 ? "At least 1000 ms" : undefined),
    health_timeout_ms: (v, all) => (all.health_enabled && ((v ?? 0) < 1 || (v ?? 0) >= (all.health_interval_ms ?? 0)) ? "Must be above 0 and below the interval" : undefined),
    health_path: (v, all) => (all.health_enabled && v.trim() && (!v.startsWith("/") || /[?#\s]/.test(v)) ? "An absolute path such as /health, without query" : undefined),
  });
  const create = useMut((v: Values) => api.createUpstream(body(v)), "Upstream created");
  const update = useMut(({ id, v }: { id: string; v: Values }) => api.updateUpstream(id, body(v)), "Upstream saved");
  const remove = useMut((id: string) => api.deleteUpstream(id), "Upstream deleted");

  const open = (u: Upstream | "new") => {
    form.reset(u === "new" ? blank : fromUpstream(u));
    setEditing(u);
  };
  const submit = async () => {
    if (!form.validate()) return false;
    return form.run(() => (editing === "new" ? create.mutateAsync(form.values) : update.mutateAsync({ id: (editing as Upstream).id, v: form.values })), ALIAS);
  };

  return (
    <>
      <PageHeader title="Upstreams" description="OpenAI-compatible endpoints (vLLM, OpenAI, …). Credentials are referenced by environment variable name; the secret itself is never stored here." actions={<CreateButton can={perms.isInstallationAdmin} onClick={() => open("new")}>New upstream</CreateButton>} />
      <DataTable
        rows={list.data}
        loading={list.isPending}
        error={list.error}
        onRetry={() => void list.refetch()}
        rowKey={(r) => r.id}
        empty="No upstreams yet."
        columns={[
          { key: "name", header: "Name", sort: (a, b) => a.name.localeCompare(b.name), cell: (r) => <strong>{r.name}</strong> },
          { key: "kind", header: "Kind", cell: (r) => <Badge tone="info">{KINDS.find((k) => k.value === r.kind)?.label ?? r.kind}</Badge> },
          { key: "url", header: "Base URL", cell: (r) => <code>{r.base_url}</code> },
          { key: "ref", header: "API key env", cell: (r) => (r.api_key_ref ? <code>{r.api_key_ref}</code> : <span className="text-muted-foreground">none</span>) },
          { key: "timeout", header: "Timeout", align: "right", cell: (r) => `${r.timeout_ms} ms` },
          {
            key: "resilience",
            header: "Resilience",
            cell: (r) =>
              r.health_check || r.circuit_breaker || r.metrics_url ? (
                <>
                  {r.health_check && <Badge tone="info">health {r.health_check.interval_ms / 1000}s</Badge>}
                  {r.circuit_breaker && <Badge tone="info">breaker {r.circuit_breaker.failure_threshold ?? 5}×</Badge>}
                  {r.metrics_url && <Badge>metrics</Badge>}
                </>
              ) : (
                <span className="text-muted-foreground">—</span>
              ),
          },
          { key: "actions", header: "", cell: (r) => <RowActions canEdit={perms.isInstallationAdmin} canDelete={perms.isInstallationAdmin} onEdit={() => open(r)} onDelete={() => setDeleting(r)} /> },
        ]}
      />
      <FormDialog variant="drawer" open={!!editing} title={editing === "new" ? "New upstream" : "Edit upstream"} submitLabel={editing === "new" ? "Create" : "Save"} onClose={() => setEditing(null)} onSubmit={submit}>
        <TextField label="Name" value={form.values.name} onChange={(v) => form.set("name", v)} error={form.errors.name} autoFocus />
        <SelectField label="Kind" value={form.values.kind} onChange={(v) => form.set("kind", v as Values["kind"])} options={KINDS} />
        <TextField label="Base URL" type="url" value={form.values.base_url} onChange={(v) => form.set("base_url", v)} error={form.errors.base_url} placeholder="http://vllm:8000/v1" />
        <TextField label="API key environment variable" value={form.values.api_key_ref} onChange={(v) => form.set("api_key_ref", v)} error={form.errors.api_key_ref} placeholder="OPENAI_API_KEY" hint="Name of the variable the gateway reads the credential from. Leave empty for none." />
        <NumberField label="Timeout (ms)" value={form.values.timeout_ms} onChange={(v) => form.set("timeout_ms", v)} error={form.errors.timeout_ms} min={1} />
        <SelectField label="Streaming usage" value={form.values.stream_usage_mode} onChange={(v) => form.set("stream_usage_mode", v as Values["stream_usage_mode"])} options={USAGE_MODES} />
        <fieldset className="m-0 flex min-w-0 flex-col gap-3 rounded-xl border p-3">
          <legend className="px-1 font-semibold">Active health check</legend>
          <CheckField label="Probe this upstream" checked={form.values.health_enabled} onChange={(v) => form.set("health_enabled", v)} hint="GET base URL + path on an interval; 2xx within the timeout counts as healthy. Unhealthy upstreams are skipped while another candidate is available." />
          {form.values.health_enabled && (
            <>
              <TextField label="Probe path" value={form.values.health_path} onChange={(v) => form.set("health_path", v)} error={form.errors.health_path} placeholder="/models" hint="Relative to the base URL. Empty uses /models." />
              <div className="grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-3">
                <NumberField label="Probe interval (ms)" value={form.values.health_interval_ms} onChange={(v) => form.set("health_interval_ms", v)} error={form.errors.health_interval_ms} min={1000} placeholder="10000" />
                <NumberField label="Probe timeout (ms)" value={form.values.health_timeout_ms} onChange={(v) => form.set("health_timeout_ms", v)} error={form.errors.health_timeout_ms} min={1} placeholder="2000" />
                <NumberField label="Failures to mark down" value={form.values.health_unhealthy} onChange={(v) => form.set("health_unhealthy", v)} error={form.errors.health_unhealthy} min={1} placeholder="default (3)" />
                <NumberField label="Successes to mark up" value={form.values.health_healthy} onChange={(v) => form.set("health_healthy", v)} error={form.errors.health_healthy} min={1} placeholder="default (1)" />
              </div>
            </>
          )}
        </fieldset>
        <fieldset className="m-0 flex min-w-0 flex-col gap-3 rounded-xl border p-3">
          <legend className="px-1 font-semibold">Circuit breaker</legend>
          <CheckField label="Open the circuit after repeated failures" checked={form.values.breaker_enabled} onChange={(v) => form.set("breaker_enabled", v)} hint="Applies to each model on this upstream. While open the model gets no traffic; after the open time one trial request decides whether it closes." />
          {form.values.breaker_enabled && (
            <div className="grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-3">
              <NumberField label="Consecutive failures" value={form.values.breaker_failures} onChange={(v) => form.set("breaker_failures", v)} error={form.errors.breaker_failures} min={1} placeholder="default (5)" />
              <NumberField label="Open time (ms)" value={form.values.breaker_open_ms} onChange={(v) => form.set("breaker_open_ms", v)} error={form.errors.breaker_open_ms} min={1} placeholder="default (30000)" />
            </div>
          )}
        </fieldset>
        <TextField label="Metrics URL" type="url" value={form.values.metrics_url} onChange={(v) => form.set("metrics_url", v)} error={form.errors.metrics_url} placeholder="http://vllm:8000/metrics" hint="vLLM Prometheus endpoint for the “vLLM load” strategy. Empty derives it from the base URL (…/v1 → …/metrics)." />
      </FormDialog>
      <ConfirmDialog open={!!deleting} title="Delete upstream" message={`Delete “${deleting?.name}”? It must not be used by any model.`} onConfirm={() => remove.mutateAsync(deleting!.id)} onClose={() => setDeleting(null)} />
    </>
  );
}
