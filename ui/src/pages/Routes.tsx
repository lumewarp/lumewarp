import { useState } from "react";
import { api, type Route, type RouteTarget, type Strategy } from "../api";
import { Badge, PageHeader } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { Field, FormDialog, NumberField, required, SelectField, TextField, useForm } from "../components/Form";
import { CreateButton, RowActions } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { useCrud, useList, useNames } from "../hooks";

const STRATEGIES: { value: Strategy; label: string }[] = [
  { value: "weighted", label: "Weighted" },
  { value: "least_latency", label: "Lowest latency (EWMA)" },
  { value: "least_inflight", label: "Fewest in-flight" },
];
const blank = { alias: "", strategy: "weighted" as Strategy, targets: [] as RouteTarget[], fallback_alias: null as string | null, max_retries: 1 };

export function Routes() {
  const { list, create, update, remove } = useCrud("routes", api.routes, "Route");
  const deployments = useList("deployments", () => api.deployments.list());
  const depName = useNames(deployments.data);
  const [editing, setEditing] = useState<Route | "new" | null>(null);
  const [deleting, setDeleting] = useState<Route | null>(null);
  const form = useForm(blank, {
    alias: required("Alias"),
    targets: (v) => (v.length ? undefined : "Add at least one deployment"),
    max_retries: (v) => (v >= 0 && v <= 5 ? undefined : "Between 0 and 5"),
  });

  const open = (r: Route | "new") => {
    form.reset(r === "new" ? blank : { alias: r.alias, strategy: r.strategy, targets: r.targets.map((t) => ({ ...t })), fallback_alias: r.fallback_alias, max_retries: r.max_retries });
    setEditing(r);
  };
  const submit = async () => {
    if (!form.validate()) return false;
    if (editing === "new") await create.mutateAsync(form.values);
    else if (editing) await update.mutateAsync({ id: editing.id, input: form.values });
  };

  const targets = form.values.targets;
  const setTarget = (i: number, t: RouteTarget) => form.set("targets", targets.map((x, j) => (j === i ? t : x)));
  const unused = (deployments.data ?? []).filter((d) => !targets.some((t) => t.deployment_id === d.id));

  return (
    <>
      <PageHeader title="Models / Routes" description="A model alias maps to deployments, a balancing strategy and an optional fallback." actions={<CreateButton onClick={() => open("new")}>New route</CreateButton>} />
      <DataTable
        rows={list.data}
        loading={list.isPending}
        error={list.error}
        onRetry={() => void list.refetch()}
        rowKey={(r) => r.id}
        empty="No routes yet."
        columns={[
          { key: "alias", header: "Alias", sort: (a, b) => a.alias.localeCompare(b.alias), cell: (r) => <strong>{r.alias}</strong> },
          { key: "strategy", header: "Strategy", cell: (r) => <Badge tone="info">{STRATEGIES.find((s) => s.value === r.strategy)?.label}</Badge> },
          { key: "targets", header: "Deployments", cell: (r) => r.targets.map((t) => <Badge key={t.deployment_id}>{depName(t.deployment_id)}{r.strategy === "weighted" ? ` ×${t.weight}` : ""}</Badge>) },
          { key: "fallback", header: "Fallback", cell: (r) => r.fallback_alias ?? <span className="muted">none</span> },
          { key: "retries", header: "Retries", align: "right", cell: (r) => r.max_retries },
          { key: "actions", header: "", cell: (r) => <RowActions onEdit={() => open(r)} onDelete={() => setDeleting(r)} /> },
        ]}
      />
      <FormDialog open={!!editing} variant="drawer" title={editing === "new" ? "New route" : "Edit route"} onClose={() => setEditing(null)} onSubmit={submit}>
        <TextField label="Model alias" value={form.values.alias} onChange={(v) => form.set("alias", v)} error={form.errors.alias} hint="What clients put in the `model` field." autoFocus />
        <SelectField label="Strategy" value={form.values.strategy} onChange={(v) => form.set("strategy", v as Strategy)} options={STRATEGIES} />
        <Field label="Deployments" error={form.errors.targets as string | undefined}>
          {(p) => (
            <div className="targets" id={p.id} aria-describedby={p["aria-describedby"]}>
              {targets.map((t, i) => (
                <div key={t.deployment_id} className="target-row">
                  <span>{depName(t.deployment_id)}</span>
                  {form.values.strategy === "weighted" && (
                    <input className="input input-sm" type="number" min={1} aria-label={`Weight for ${depName(t.deployment_id)}`} value={t.weight} onChange={(e) => setTarget(i, { ...t, weight: Math.max(1, Number(e.target.value) || 1) })} />
                  )}
                  <button type="button" className="btn btn-sm btn-danger-ghost" onClick={() => form.set("targets", targets.filter((_, j) => j !== i))}>
                    Remove
                  </button>
                </div>
              ))}
              {unused.length > 0 && (
                <select className="input" aria-label="Add deployment" value="" onChange={(e) => e.target.value && form.set("targets", [...targets, { deployment_id: e.target.value, weight: 1 }])}>
                  <option value="">Add deployment…</option>
                  {unused.map((d) => (
                    <option key={d.id} value={d.id}>
                      {d.name}
                    </option>
                  ))}
                </select>
              )}
            </div>
          )}
        </Field>
        <SelectField label="Fallback route" value={form.values.fallback_alias ?? ""} onChange={(v) => form.set("fallback_alias", v || null)} options={[{ value: "", label: "None" }, ...(list.data ?? []).filter((r) => editing === "new" || r.id !== (editing as Route | null)?.id).map((r) => ({ value: r.alias, label: r.alias }))]} />
        <NumberField label="Max retries" value={form.values.max_retries} min={0} onChange={(v) => form.set("max_retries", v ?? 0)} error={form.errors.max_retries} hint="Bounded retries before falling back." />
      </FormDialog>
      <ConfirmDialog open={!!deleting} title="Delete route" message={`Delete route “${deleting?.alias}”? Clients using this model alias will get errors.`} onConfirm={() => remove.mutateAsync(deleting!.id)} onClose={() => setDeleting(null)} />
    </>
  );
}
