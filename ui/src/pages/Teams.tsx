import { useState } from "react";
import { api, type Team } from "../api";
import { Badge, BudgetBar, PageHeader } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { FormDialog, ListField, NumberField, required, SelectField, TextField, useForm } from "../components/Form";
import { CreateButton, RowActions } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { useCrud, useList, useNames } from "../hooks";

const blank = { org_id: "", name: "", budget_usd: null as number | null, rpm: null as number | null, allowed_models: [] as string[] };

export function Teams() {
  const { list, create, update, remove } = useCrud("teams", api.teams, "Team");
  const orgs = useList("orgs", () => api.orgs.list());
  const orgName = useNames(orgs.data);
  const [editing, setEditing] = useState<Team | "new" | null>(null);
  const [deleting, setDeleting] = useState<Team | null>(null);
  const form = useForm(blank, { name: required("Name"), org_id: required("Organization") });

  const open = (t: Team | "new") => {
    form.reset(t === "new" ? { ...blank, org_id: orgs.data?.[0]?.id ?? "" } : { org_id: t.org_id, name: t.name, budget_usd: t.budget_usd, rpm: t.rpm, allowed_models: t.allowed_models });
    setEditing(t);
  };
  const submit = async () => {
    if (!form.validate()) return false;
    if (editing === "new") await create.mutateAsync(form.values);
    else if (editing) await update.mutateAsync({ id: editing.id, input: form.values });
  };

  return (
    <>
      <PageHeader title="Teams" description="Teams carry budgets, rate limits and model allow-lists, narrowing their organization's." actions={<CreateButton onClick={() => open("new")}>New team</CreateButton>} />
      <DataTable
        rows={list.data}
        loading={list.isPending}
        error={list.error}
        onRetry={() => void list.refetch()}
        rowKey={(r) => r.id}
        empty="No teams yet."
        columns={[
          { key: "name", header: "Team", sort: (a, b) => a.name.localeCompare(b.name), cell: (r) => <strong>{r.name}</strong> },
          { key: "org", header: "Organization", sort: (a, b) => orgName(a.org_id).localeCompare(orgName(b.org_id)), cell: (r) => orgName(r.org_id) },
          { key: "budget", header: "Monthly budget", cell: (r) => <BudgetBar spent={r.spent_usd} budget={r.budget_usd} /> },
          { key: "rpm", header: "RPM", align: "right", cell: (r) => r.rpm ?? "—" },
          { key: "models", header: "Models", cell: (r) => (r.allowed_models.length ? r.allowed_models.map((m) => <Badge key={m}>{m}</Badge>) : <span className="muted">all</span>) },
          { key: "actions", header: "", cell: (r) => <RowActions onEdit={() => open(r)} onDelete={() => setDeleting(r)} /> },
        ]}
      />
      <FormDialog open={!!editing} title={editing === "new" ? "New team" : "Edit team"} onClose={() => setEditing(null)} onSubmit={submit}>
        <SelectField label="Organization" value={form.values.org_id} onChange={(v) => form.set("org_id", v)} error={form.errors.org_id} options={(orgs.data ?? []).map((o) => ({ value: o.id, label: o.name }))} />
        <TextField label="Name" value={form.values.name} onChange={(v) => form.set("name", v)} error={form.errors.name} autoFocus />
        <NumberField label="Monthly budget (USD)" value={form.values.budget_usd} onChange={(v) => form.set("budget_usd", v)} />
        <NumberField label="Requests per minute" value={form.values.rpm} onChange={(v) => form.set("rpm", v)} />
        <ListField label="Allowed models" value={form.values.allowed_models} onChange={(v) => form.set("allowed_models", v)} hint="Comma-separated aliases; empty allows all the org allows." />
      </FormDialog>
      <ConfirmDialog open={!!deleting} title="Delete team" message={`Delete team “${deleting?.name}” and everything under it?`} onConfirm={() => remove.mutateAsync(deleting!.id)} onClose={() => setDeleting(null)} />
    </>
  );
}
