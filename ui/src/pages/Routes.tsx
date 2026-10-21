import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Route } from "../api";
import { Badge, PageHeader } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { Field, FormDialog, required, SelectField, TextField, useForm } from "../components/Form";
import { CreateButton, RowActions } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { useCan, useMut, useNames } from "../hooks";

const STRATEGIES = [
  { value: "weighted", label: "Weighted" },
  { value: "least_in_flight", label: "Fewest in-flight requests" },
];

interface Values {
  model: string;
  strategy: "weighted" | "least_in_flight";
  deployment_ids: string[];
}
const blank: Values = { model: "", strategy: "weighted", deployment_ids: [] };

export function Routes() {
  const perms = useCan();
  const list = useQuery({ queryKey: ["routes"], queryFn: () => api.routes() });
  const deployments = useQuery({ queryKey: ["deployments"], queryFn: () => api.deployments() });
  const depName = useNames(deployments.data);
  const [editing, setEditing] = useState<Route | "new" | null>(null);
  const [deleting, setDeleting] = useState<Route | null>(null);
  const form = useForm<Values>(blank, {
    model: required("Model alias"),
    deployment_ids: (v) => (v.length ? undefined : "Pick at least one deployment"),
  });
  const create = useMut((v: Values) => api.createRoute(v), "Route created");
  const update = useMut(({ id, v }: { id: string; v: Values }) => api.updateRoute(id, v), "Route saved");
  const remove = useMut((id: string) => api.deleteRoute(id), "Route deleted");

  const open = (r: Route | "new") => {
    form.reset(r === "new" ? blank : { model: r.model, strategy: r.strategy === "least_in_flight" ? "least_in_flight" : "weighted", deployment_ids: [...r.deployments].sort((a, b) => a.position - b.position).map((d) => d.deployment_id) });
    setEditing(r);
  };
  const submit = async () => {
    if (!form.validate()) return false;
    return form.run(() => (editing === "new" ? create.mutateAsync(form.values) : update.mutateAsync({ id: (editing as Route).id, v: form.values })), { deployment_ids: "deployment_ids" });
  };
  const toggle = (id: string) => {
    const ids = form.values.deployment_ids;
    form.set("deployment_ids", ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id]);
  };

  return (
    <>
      <PageHeader title="Models / Routes" description="A model alias that clients request, mapped to one or more deployments." actions={<CreateButton can={perms.isSuper} onClick={() => open("new")}>New route</CreateButton>} />
      <DataTable
        rows={list.data}
        loading={list.isPending}
        error={list.error}
        onRetry={() => void list.refetch()}
        rowKey={(r) => r.id}
        empty="No routes yet."
        columns={[
          { key: "model", header: "Model alias", sort: (a, b) => a.model.localeCompare(b.model), cell: (r) => <strong>{r.model}</strong> },
          { key: "strategy", header: "Strategy", cell: (r) => STRATEGIES.find((s) => s.value === r.strategy)?.label ?? r.strategy },
          { key: "deps", header: "Deployments", cell: (r) => [...r.deployments].sort((a, b) => a.position - b.position).map((d) => <Badge key={d.deployment_id} tone="info">{depName(d.deployment_id)}</Badge>) },
          { key: "actions", header: "", cell: (r) => <RowActions canEdit={perms.isSuper} canDelete={perms.isSuper} onEdit={() => open(r)} onDelete={() => setDeleting(r)} /> },
        ]}
      />
      <FormDialog open={!!editing} title={editing === "new" ? "New route" : "Edit route"} submitLabel={editing === "new" ? "Create" : "Save"} onClose={() => setEditing(null)} onSubmit={submit}>
        <TextField label="Model alias" value={form.values.model} onChange={(v) => form.set("model", v)} error={form.errors.model} autoFocus hint="The `model` value clients send." />
        <SelectField label="Strategy" value={form.values.strategy} onChange={(v) => form.set("strategy", v as Values["strategy"])} options={STRATEGIES} />
        <Field label="Deployments" error={form.errors.deployment_ids}>
          {(p) => (
            <div className="targets" id={p.id} aria-invalid={p["aria-invalid"]}>
              {(deployments.data ?? []).map((d) => (
                <label key={d.id} className="target-row">
                  <input type="checkbox" checked={form.values.deployment_ids.includes(d.id)} onChange={() => toggle(d.id)} />
                  <span>
                    {d.name} <span className="muted">· weight {d.weight}</span>
                  </span>
                </label>
              ))}
              {deployments.data?.length === 0 && <p className="muted">Create a deployment first.</p>}
            </div>
          )}
        </Field>
      </FormDialog>
      <ConfirmDialog open={!!deleting} title="Delete route" message={`Delete route “${deleting?.model}”? Clients requesting this model will get an error.`} onConfirm={() => remove.mutateAsync(deleting!.id)} onClose={() => setDeleting(null)} />
    </>
  );
}
