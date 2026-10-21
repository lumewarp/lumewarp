import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Upstream } from "../api";
import { Badge, PageHeader } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { FormDialog, NumberField, required, SelectField, TextField, useForm } from "../components/Form";
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
}
const blank: Values = { name: "", kind: "vllm", base_url: "", api_key_ref: "", timeout_ms: 600000, stream_usage_mode: "include_usage" };

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
  const form = useForm<Values>(blank, { name: required("Name"), base_url: (v) => (v.trim() ? validUrl(v) : "Base URL is required") });
  const body = (v: Values) => ({ name: v.name, kind: v.kind, base_url: v.base_url, api_key_ref: v.api_key_ref.trim() || null, timeout_ms: v.timeout_ms ?? 600000, stream_usage_mode: v.stream_usage_mode });
  const create = useMut((v: Values) => api.createUpstream(body(v)), "Upstream created");
  const update = useMut(({ id, v }: { id: string; v: Values }) => api.updateUpstream(id, body(v)), "Upstream saved");
  const remove = useMut((id: string) => api.deleteUpstream(id), "Upstream deleted");

  const open = (u: Upstream | "new") => {
    form.reset(u === "new" ? blank : { name: u.name, kind: u.kind, base_url: u.base_url, api_key_ref: u.api_key_ref ?? "", timeout_ms: u.timeout_ms, stream_usage_mode: u.stream_usage_mode });
    setEditing(u);
  };
  const submit = async () => {
    if (!form.validate()) return false;
    return form.run(() => (editing === "new" ? create.mutateAsync(form.values) : update.mutateAsync({ id: (editing as Upstream).id, v: form.values })));
  };

  return (
    <>
      <PageHeader title="Upstreams" description="OpenAI-compatible endpoints (vLLM, OpenAI, …). Credentials are referenced by environment variable name; the secret itself is never stored here." actions={<CreateButton can={perms.isSuper} onClick={() => open("new")}>New upstream</CreateButton>} />
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
          { key: "ref", header: "API key env", cell: (r) => (r.api_key_ref ? <code>{r.api_key_ref}</code> : <span className="muted">none</span>) },
          { key: "timeout", header: "Timeout", align: "right", cell: (r) => `${r.timeout_ms} ms` },
          { key: "actions", header: "", cell: (r) => <RowActions canEdit={perms.isSuper} canDelete={perms.isSuper} onEdit={() => open(r)} onDelete={() => setDeleting(r)} /> },
        ]}
      />
      <FormDialog open={!!editing} title={editing === "new" ? "New upstream" : "Edit upstream"} submitLabel={editing === "new" ? "Create" : "Save"} onClose={() => setEditing(null)} onSubmit={submit}>
        <TextField label="Name" value={form.values.name} onChange={(v) => form.set("name", v)} error={form.errors.name} autoFocus />
        <SelectField label="Kind" value={form.values.kind} onChange={(v) => form.set("kind", v as Values["kind"])} options={KINDS} />
        <TextField label="Base URL" type="url" value={form.values.base_url} onChange={(v) => form.set("base_url", v)} error={form.errors.base_url} placeholder="http://vllm:8000/v1" />
        <TextField label="API key environment variable" value={form.values.api_key_ref} onChange={(v) => form.set("api_key_ref", v)} error={form.errors.api_key_ref} placeholder="OPENAI_API_KEY" hint="Name of the variable the gateway reads the credential from. Leave empty for none." />
        <NumberField label="Timeout (ms)" value={form.values.timeout_ms} onChange={(v) => form.set("timeout_ms", v)} error={form.errors.timeout_ms} min={1} />
        <SelectField label="Streaming usage" value={form.values.stream_usage_mode} onChange={(v) => form.set("stream_usage_mode", v as Values["stream_usage_mode"])} options={USAGE_MODES} />
      </FormDialog>
      <ConfirmDialog open={!!deleting} title="Delete upstream" message={`Delete “${deleting?.name}”? It must not be used by any deployment.`} onConfirm={() => remove.mutateAsync(deleting!.id)} onClose={() => setDeleting(null)} />
    </>
  );
}
