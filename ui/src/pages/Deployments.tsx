import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Deployment } from "../api";
import { fmtUsd, nanoToUsd, PageHeader, usdToNano } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { FormDialog, NumberField, required, SelectField, TextField, useForm } from "../components/Form";
import { CreateButton, RowActions } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { useCan, useMut, useNames } from "../hooks";

interface Values {
  name: string;
  upstream_id: string;
  upstream_model: string;
  weight: number | null;
  price_in: number | null;
  price_out: number | null;
}
const blank: Values = { name: "", upstream_id: "", upstream_model: "", weight: 1, price_in: null, price_out: null };

/** Prices are entered as USD per 1M tokens and stored as integer nano-USD per 1M tokens. */
const body = (v: Values) => ({
  name: v.name,
  upstream_id: v.upstream_id,
  upstream_model: v.upstream_model,
  weight: v.weight ?? 1,
  price_input_nano_usd_per_mtok: usdToNano(v.price_in),
  price_output_nano_usd_per_mtok: usdToNano(v.price_out),
});
const ALIAS = { price_input_nano_usd_per_mtok: "price_in", price_output_nano_usd_per_mtok: "price_out" } as const;

export function Deployments() {
  const perms = useCan();
  const list = useQuery({ queryKey: ["deployments"], queryFn: () => api.deployments() });
  const upstreams = useQuery({ queryKey: ["upstreams"], queryFn: () => api.upstreams() });
  const upstreamName = useNames(upstreams.data);
  const [editing, setEditing] = useState<Deployment | "new" | null>(null);
  const [deleting, setDeleting] = useState<Deployment | null>(null);
  const form = useForm<Values>(blank, { name: required("Name"), upstream_id: required("Upstream"), upstream_model: required("Upstream model") });
  const create = useMut((v: Values) => api.createDeployment(body(v)), "Deployment created");
  const update = useMut(({ id, v }: { id: string; v: Values }) => api.updateDeployment(id, body(v)), "Deployment saved");
  const remove = useMut((id: string) => api.deleteDeployment(id), "Deployment deleted");

  const open = (d: Deployment | "new") => {
    form.reset(
      d === "new"
        ? { ...blank, upstream_id: upstreams.data?.[0]?.id ?? "" }
        : { name: d.name, upstream_id: d.upstream_id, upstream_model: d.upstream_model, weight: d.weight, price_in: nanoToUsd(d.price_input_nano_usd_per_mtok), price_out: nanoToUsd(d.price_output_nano_usd_per_mtok) },
    );
    setEditing(d);
  };
  const submit = async () => {
    if (!form.validate()) return false;
    return form.run(() => (editing === "new" ? create.mutateAsync(form.values) : update.mutateAsync({ id: (editing as Deployment).id, v: form.values })), ALIAS);
  };

  return (
    <>
      <PageHeader title="Deployments" description="A model served by an upstream, with a routing weight and the price used to compute request cost." actions={<CreateButton can={perms.isSuper} onClick={() => open("new")}>New deployment</CreateButton>} />
      <DataTable
        rows={list.data}
        loading={list.isPending}
        error={list.error}
        onRetry={() => void list.refetch()}
        rowKey={(r) => r.id}
        empty="No deployments yet."
        columns={[
          { key: "name", header: "Name", sort: (a, b) => a.name.localeCompare(b.name), cell: (r) => <strong>{r.name}</strong> },
          { key: "upstream", header: "Upstream", cell: (r) => upstreamName(r.upstream_id) },
          { key: "model", header: "Upstream model", cell: (r) => <code>{r.upstream_model}</code> },
          { key: "weight", header: "Weight", align: "right", cell: (r) => r.weight },
          { key: "in", header: "Input / 1M tok", align: "right", cell: (r) => fmtUsd(r.price_input_nano_usd_per_mtok, "unpriced") },
          { key: "out", header: "Output / 1M tok", align: "right", cell: (r) => fmtUsd(r.price_output_nano_usd_per_mtok, "unpriced") },
          { key: "actions", header: "", cell: (r) => <RowActions canEdit={perms.isSuper} canDelete={perms.isSuper} onEdit={() => open(r)} onDelete={() => setDeleting(r)} /> },
        ]}
      />
      <FormDialog open={!!editing} title={editing === "new" ? "New deployment" : "Edit deployment"} submitLabel={editing === "new" ? "Create" : "Save"} onClose={() => setEditing(null)} onSubmit={submit}>
        <TextField label="Name" value={form.values.name} onChange={(v) => form.set("name", v)} error={form.errors.name} autoFocus />
        <SelectField label="Upstream" value={form.values.upstream_id} onChange={(v) => form.set("upstream_id", v)} error={form.errors.upstream_id} options={(upstreams.data ?? []).map((u) => ({ value: u.id, label: u.name }))} />
        <TextField label="Upstream model" value={form.values.upstream_model} onChange={(v) => form.set("upstream_model", v)} error={form.errors.upstream_model} placeholder="meta-llama/Llama-3.1-8B-Instruct" hint="The model name sent to the upstream." />
        <NumberField label="Weight" value={form.values.weight} onChange={(v) => form.set("weight", v)} error={form.errors.weight} min={1} hint="Share of traffic under the weighted strategy." />
        <NumberField label="Input price (USD per 1M tokens)" value={form.values.price_in} onChange={(v) => form.set("price_in", v)} error={form.errors.price_in} step="any" />
        <NumberField label="Output price (USD per 1M tokens)" value={form.values.price_out} onChange={(v) => form.set("price_out", v)} error={form.errors.price_out} step="any" hint="Leave prices empty if the cost is unknown; usage is then counted as unpriced." />
      </FormDialog>
      <ConfirmDialog open={!!deleting} title="Delete deployment" message={`Delete “${deleting?.name}”? It must not be part of any route.`} onConfirm={() => remove.mutateAsync(deleting!.id)} onClose={() => setDeleting(null)} />
    </>
  );
}
