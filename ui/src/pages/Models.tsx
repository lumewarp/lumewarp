import { useMemo, useState } from "react";
import { type Model } from "../api";
import { api } from "../api";
import { Badge, fmtUsd, nanoToUsd, PageHeader, usdToNano } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { CheckField, FormDialog, NumberField, required, TextField, useForm } from "../components/Form";
import { CreateButton, RowActions } from "../components/RowActions";
import { SearchBox } from "../components/Search";
import { DataTable } from "../components/Table";
import { ComboboxField } from "../components/ui/combobox-field";
import { useAliases, useCan, useModels, useMut, useNames, useRouters, useUpstreams } from "../hooks";
import { matches, usedBy } from "../lib/catalog";

interface Values {
  name: string;
  upstream_id: string;
  upstream_model: string;
  price_in: number | null;
  price_out: number | null;
  keep_old_name_as_alias: boolean;
}
const blank: Values = { name: "", upstream_id: "", upstream_model: "", price_in: null, price_out: null, keep_old_name_as_alias: true };

/** Prices are entered as USD per 1M tokens and stored as integer nano-USD per 1M tokens. */
const body = (v: Values) => ({
  name: v.name.trim(),
  upstream_id: v.upstream_id,
  upstream_model: v.upstream_model.trim(),
  price_input_nano_usd_per_mtok: usdToNano(v.price_in),
  price_output_nano_usd_per_mtok: usdToNano(v.price_out),
});
const ALIAS = { price_input_nano_usd_per_mtok: "price_in", price_output_nano_usd_per_mtok: "price_out" } as const;

export function Models() {
  const perms = useCan();
  const list = useModels();
  const upstreams = useUpstreams();
  const routers = useRouters();
  const aliases = useAliases();
  const upstreamName = useNames(upstreams.data);
  const [search, setSearch] = useState("");
  const [editing, setEditing] = useState<Model | "new" | null>(null);
  const [deleting, setDeleting] = useState<Model | null>(null);
  const form = useForm<Values>(blank, { name: required("Name"), upstream_id: required("Upstream"), upstream_model: required("Upstream model") });
  const create = useMut((v: Values) => api.createModel(body(v)), "Model created");
  const update = useMut(({ id, v, renamed }: { id: string; v: Values; renamed: boolean }) => api.updateModel(id, { ...body(v), keep_old_name_as_alias: renamed && v.keep_old_name_as_alias }), "Model saved");
  const remove = useMut((id: string) => api.deleteModel(id), "Model deleted");

  const rows = useMemo(
    () => (list.data ?? []).filter((m) => matches(search, m.name, m.upstream_model)),
    [list.data, search],
  );

  const open = (d: Model | "new") => {
    form.reset(
      d === "new"
        ? { ...blank, upstream_id: upstreams.data?.[0]?.id ?? "" }
        : { name: d.name, upstream_id: d.upstream_id, upstream_model: d.upstream_model, price_in: nanoToUsd(d.price_input_nano_usd_per_mtok), price_out: nanoToUsd(d.price_output_nano_usd_per_mtok), keep_old_name_as_alias: true },
    );
    setEditing(d);
  };
  const renamed = editing !== null && editing !== "new" && form.values.name.trim() !== editing.name;
  const submit = async () => {
    if (!form.validate()) return false;
    return form.run(() => (editing === "new" ? create.mutateAsync(form.values) : update.mutateAsync({ id: (editing as Model).id, v: form.values, renamed })), ALIAS);
  };
  const loadError = list.error ?? upstreams.error ?? routers.error ?? aliases.error;

  return (
    <>
      <PageHeader title="Models" description="A public name mapped to one model on one upstream, with the price used to compute request cost." actions={<CreateButton can={perms.isInstallationAdmin} onClick={() => open("new")}>New model</CreateButton>} />
      <SearchBox value={search} onChange={setSearch} label="Search models" placeholder="Name or upstream model" />
      <DataTable
        rows={rows}
        loading={list.isPending || upstreams.isPending || routers.isPending || aliases.isPending}
        error={loadError}
        onRetry={() => {
          void list.refetch();
          void upstreams.refetch();
          void routers.refetch();
          void aliases.refetch();
        }}
        rowKey={(r) => r.id}
        empty={search ? "No models match your search." : "No models yet."}
        columns={[
          { key: "name", header: "Name", sort: (a, b) => a.name.localeCompare(b.name), cell: (r) => <strong>{r.name}</strong> },
          {
            key: "maps",
            header: "Maps to",
            cell: (r) => (
              <div className="flex flex-col">
                <span>{upstreamName(r.upstream_id)}</span>
                <code className="text-muted-foreground">{r.upstream_model}</code>
              </div>
            ),
          },
          {
            key: "used",
            header: "Used by",
            cell: (r) => {
              const u = usedBy(r, routers.data ?? [], aliases.data ?? []);
              if (!u.routers.length && !u.aliases.length) return <span className="text-muted-foreground">—</span>;
              return (
                <div className="flex flex-wrap gap-1">
                  {u.routers.map((n) => (
                    <Badge key={`r-${n}`} tone="info">
                      {n}
                    </Badge>
                  ))}
                  {u.aliases.map((n) => (
                    <Badge key={`a-${n}`}>↳ {n}</Badge>
                  ))}
                </div>
              );
            },
          },
          { key: "in", header: "Input / 1M tok", align: "right", hideBelow: "lg", cell: (r) => fmtUsd(r.price_input_nano_usd_per_mtok, "unpriced") },
          { key: "out", header: "Output / 1M tok", align: "right", hideBelow: "lg", cell: (r) => fmtUsd(r.price_output_nano_usd_per_mtok, "unpriced") },
          { key: "actions", header: "", cell: (r) => <RowActions canEdit={perms.isInstallationAdmin} canDelete={perms.isInstallationAdmin} onEdit={() => open(r)} onDelete={() => setDeleting(r)} /> },
        ]}
      />
      <FormDialog open={!!editing} title={editing === "new" ? "New model" : "Edit model"} submitLabel={editing === "new" ? "Create" : "Save"} onClose={() => setEditing(null)} onSubmit={submit}>
        <TextField label="Name" value={form.values.name} onChange={(v) => form.set("name", v)} error={form.errors.name} autoFocus hint="The `model` value clients send. Shared with aliases and routers: it must be unique." />
        {renamed && <CheckField label="Keep old name as alias" checked={form.values.keep_old_name_as_alias} onChange={(c) => form.set("keep_old_name_as_alias", c)} hint="Clients still calling the old name keep working, with the same access." />}
        <ComboboxField label="Upstream" value={form.values.upstream_id} onChange={(v) => form.set("upstream_id", v)} error={form.errors.upstream_id} options={(upstreams.data ?? []).map((u) => ({ value: u.id, label: u.name }))} />
        <TextField label="Upstream model" value={form.values.upstream_model} onChange={(v) => form.set("upstream_model", v)} error={form.errors.upstream_model} placeholder="meta-llama/Llama-3.1-8B-Instruct" hint="The model name sent to the upstream." />
        <NumberField label="Input price (USD per 1M tokens)" value={form.values.price_in} onChange={(v) => form.set("price_in", v)} error={form.errors.price_in} step="any" placeholder="unpriced" />
        <NumberField label="Output price (USD per 1M tokens)" value={form.values.price_out} onChange={(v) => form.set("price_out", v)} error={form.errors.price_out} step="any" placeholder="unpriced" hint="Leave prices empty if the cost is unknown; usage is then counted as unpriced." />
      </FormDialog>
      <ConfirmDialog open={!!deleting} title="Delete model" message={`Delete “${deleting?.name}”? It must not be used by any router, fallback or alias.`} onConfirm={() => remove.mutateAsync(deleting!.id)} onClose={() => setDeleting(null)} />
    </>
  );
}
