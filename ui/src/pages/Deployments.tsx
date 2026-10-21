import { useState } from "react";
import { api, type Deployment } from "../api";
import { PageHeader } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { FormDialog, NumberField, required, SelectField, TextField, useForm } from "../components/Form";
import { CreateButton, RowActions } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { useCrud, useList, useNames } from "../hooks";

const blank = { name: "", upstream_id: "", upstream_model: "", price_in: 0, price_out: 0, max_parallel: null as number | null };
const price = (n: number) => `$${n}`;

export function Deployments() {
  const { list, create, update, remove } = useCrud("deployments", api.deployments, "Deployment");
  const upstreams = useList("upstreams", () => api.upstreams.list());
  const upName = useNames(upstreams.data);
  const [editing, setEditing] = useState<Deployment | "new" | null>(null);
  const [deleting, setDeleting] = useState<Deployment | null>(null);
  const form = useForm(blank, {
    name: required("Name"),
    upstream_id: required("Upstream"),
    upstream_model: required("Upstream model"),
    price_in: (v) => (v >= 0 ? undefined : "Must be ≥ 0"),
    price_out: (v) => (v >= 0 ? undefined : "Must be ≥ 0"),
  });

  const open = (d: Deployment | "new") => {
    form.reset(d === "new" ? { ...blank, upstream_id: upstreams.data?.[0]?.id ?? "" } : { name: d.name, upstream_id: d.upstream_id, upstream_model: d.upstream_model, price_in: d.price_in, price_out: d.price_out, max_parallel: d.max_parallel });
    setEditing(d);
  };
  const submit = async () => {
    if (!form.validate()) return false;
    if (editing === "new") await create.mutateAsync(form.values);
    else if (editing) await update.mutateAsync({ id: editing.id, input: form.values });
  };

  return (
    <>
      <PageHeader title="Deployments" description="A model on an upstream, with the unit price used to compute spend." actions={<CreateButton onClick={() => open("new")}>New deployment</CreateButton>} />
      <DataTable
        rows={list.data}
        loading={list.isPending}
        error={list.error}
        onRetry={() => void list.refetch()}
        rowKey={(r) => r.id}
        empty="No deployments yet."
        columns={[
          { key: "name", header: "Name", sort: (a, b) => a.name.localeCompare(b.name), cell: (r) => <strong>{r.name}</strong> },
          { key: "upstream", header: "Upstream", cell: (r) => upName(r.upstream_id) },
          { key: "model", header: "Upstream model", cell: (r) => <code>{r.upstream_model}</code> },
          { key: "in", header: "Input / 1M", align: "right", sort: (a, b) => a.price_in - b.price_in, cell: (r) => price(r.price_in) },
          { key: "out", header: "Output / 1M", align: "right", sort: (a, b) => a.price_out - b.price_out, cell: (r) => price(r.price_out) },
          { key: "par", header: "Max parallel", align: "right", cell: (r) => r.max_parallel ?? "—" },
          { key: "actions", header: "", cell: (r) => <RowActions onEdit={() => open(r)} onDelete={() => setDeleting(r)} /> },
        ]}
      />
      <FormDialog open={!!editing} title={editing === "new" ? "New deployment" : "Edit deployment"} onClose={() => setEditing(null)} onSubmit={submit}>
        <TextField label="Name" value={form.values.name} onChange={(v) => form.set("name", v)} error={form.errors.name} autoFocus />
        <SelectField label="Upstream" value={form.values.upstream_id} onChange={(v) => form.set("upstream_id", v)} error={form.errors.upstream_id} options={(upstreams.data ?? []).map((u) => ({ value: u.id, label: u.name }))} />
        <TextField label="Upstream model" value={form.values.upstream_model} onChange={(v) => form.set("upstream_model", v)} error={form.errors.upstream_model} hint="The model name sent upstream." />
        <NumberField label="Input price (USD / 1M tokens)" step={0.01} value={form.values.price_in} onChange={(v) => form.set("price_in", v ?? 0)} error={form.errors.price_in} />
        <NumberField label="Output price (USD / 1M tokens)" step={0.01} value={form.values.price_out} onChange={(v) => form.set("price_out", v ?? 0)} error={form.errors.price_out} />
        <NumberField label="Max parallel requests" value={form.values.max_parallel} onChange={(v) => form.set("max_parallel", v)} />
      </FormDialog>
      <ConfirmDialog open={!!deleting} title="Delete deployment" message={`Delete deployment “${deleting?.name}”? Routes referencing it become invalid.`} onConfirm={() => remove.mutateAsync(deleting!.id)} onClose={() => setDeleting(null)} />
    </>
  );
}
