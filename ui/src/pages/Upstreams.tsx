import { useState } from "react";
import { api, type Upstream, type UpstreamKind } from "../api";
import { Badge, PageHeader } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { FormDialog, required, SelectField, TextField, useForm } from "../components/Form";
import { CreateButton, RowActions } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { useCrud } from "../hooks";

const blank = { name: "", kind: "vllm" as UpstreamKind, base_url: "", credential: "" };

export function Upstreams() {
  const { list, create, update, remove } = useCrud("upstreams", api.upstreams, "Upstream");
  const [editing, setEditing] = useState<Upstream | "new" | null>(null);
  const [deleting, setDeleting] = useState<Upstream | null>(null);
  const form = useForm(blank, {
    name: required("Name"),
    base_url: (v) => {
      try {
        const u = new URL(v);
        return u.protocol === "http:" || u.protocol === "https:" ? undefined : "Must be an http(s) URL";
      } catch {
        return "Enter a valid URL, e.g. http://host:8000/v1";
      }
    },
  });

  const open = (u: Upstream | "new") => {
    form.reset(u === "new" ? blank : { name: u.name, kind: u.kind, base_url: u.base_url, credential: "" });
    setEditing(u);
  };
  const submit = async () => {
    if (!form.validate()) return false;
    const input = { ...form.values, credential: form.values.credential || undefined };
    if (editing === "new") await create.mutateAsync(input);
    else if (editing) await update.mutateAsync({ id: editing.id, input });
  };

  return (
    <>
      <PageHeader title="Upstreams" description="OpenAI-compatible endpoints: vLLM or OpenAI (or anything speaking the same API)." actions={<CreateButton onClick={() => open("new")}>New upstream</CreateButton>} />
      <DataTable
        rows={list.data}
        loading={list.isPending}
        error={list.error}
        onRetry={() => void list.refetch()}
        rowKey={(r) => r.id}
        empty="No upstreams yet."
        columns={[
          { key: "name", header: "Name", sort: (a, b) => a.name.localeCompare(b.name), cell: (r) => <strong>{r.name}</strong> },
          { key: "kind", header: "Kind", cell: (r) => <Badge tone="info">{r.kind}</Badge> },
          { key: "url", header: "Base URL", cell: (r) => <code>{r.base_url}</code> },
          { key: "cred", header: "Credential", cell: (r) => (r.credential_set ? <Badge tone="ok">set</Badge> : <span className="muted">none</span>) },
          { key: "actions", header: "", cell: (r) => <RowActions onEdit={() => open(r)} onDelete={() => setDeleting(r)} /> },
        ]}
      />
      <FormDialog open={!!editing} title={editing === "new" ? "New upstream" : "Edit upstream"} onClose={() => setEditing(null)} onSubmit={submit}>
        <TextField label="Name" value={form.values.name} onChange={(v) => form.set("name", v)} error={form.errors.name} autoFocus />
        <SelectField label="Kind" value={form.values.kind} onChange={(v) => form.set("kind", v as UpstreamKind)} options={[{ value: "vllm", label: "vLLM" }, { value: "openai", label: "OpenAI" }]} />
        <TextField label="Base URL" type="url" value={form.values.base_url} onChange={(v) => form.set("base_url", v)} error={form.errors.base_url} placeholder="http://host:8000/v1" />
        <TextField label="Credential" type="password" value={form.values.credential} onChange={(v) => form.set("credential", v)} hint={editing !== "new" && editing?.credential_set ? "Leave empty to keep the current credential." : "Optional bearer token sent upstream. Write-only."} />
      </FormDialog>
      <ConfirmDialog open={!!deleting} title="Delete upstream" message={`Delete upstream “${deleting?.name}”? Deployments using it will fail validation.`} onConfirm={() => remove.mutateAsync(deleting!.id)} onClose={() => setDeleting(null)} />
    </>
  );
}
