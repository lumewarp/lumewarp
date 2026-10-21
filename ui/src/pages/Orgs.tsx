import { useState } from "react";
import { api, type Org } from "../api";
import { BudgetBar, fmtDate, PageHeader } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { FormDialog, NumberField, required, TextField, useForm } from "../components/Form";
import { CreateButton, RowActions } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { useCrud } from "../hooks";

const blank = { name: "", slug: "", budget_usd: null as number | null };

export function Orgs() {
  const { list, create, update, remove } = useCrud("orgs", api.orgs, "Organization");
  const [editing, setEditing] = useState<Org | "new" | null>(null);
  const [deleting, setDeleting] = useState<Org | null>(null);
  const form = useForm(blank, {
    name: required("Name"),
    slug: (v) => (/^[a-z0-9-]+$/.test(v) ? undefined : "Lowercase letters, digits and dashes only"),
  });

  const open = (o: Org | "new") => {
    form.reset(o === "new" ? blank : { name: o.name, slug: o.slug, budget_usd: o.budget_usd });
    setEditing(o);
  };
  const submit = async () => {
    if (!form.validate()) return false;
    if (editing === "new") await create.mutateAsync(form.values);
    else if (editing) await update.mutateAsync({ id: editing.id, input: form.values });
  };

  return (
    <>
      <PageHeader title="Organizations" description="Top of the hierarchy: Organization → Team → Project → API key." actions={<CreateButton onClick={() => open("new")}>New organization</CreateButton>} />
      <DataTable
        rows={list.data}
        loading={list.isPending}
        error={list.error}
        onRetry={() => void list.refetch()}
        rowKey={(r) => r.id}
        empty="No organizations yet."
        columns={[
          { key: "name", header: "Name", sort: (a, b) => a.name.localeCompare(b.name), cell: (r) => <strong>{r.name}</strong> },
          { key: "slug", header: "Slug", cell: (r) => <code>{r.slug}</code> },
          { key: "budget", header: "Monthly budget", sort: (a, b) => a.spent_usd - b.spent_usd, cell: (r) => <BudgetBar spent={r.spent_usd} budget={r.budget_usd} /> },
          { key: "created", header: "Created", sort: (a, b) => a.created_at.localeCompare(b.created_at), cell: (r) => fmtDate(r.created_at) },
          { key: "actions", header: "", cell: (r) => <RowActions onEdit={() => open(r)} onDelete={() => setDeleting(r)} /> },
        ]}
      />
      <FormDialog open={!!editing} title={editing === "new" ? "New organization" : "Edit organization"} onClose={() => setEditing(null)} onSubmit={submit}>
        <TextField label="Name" value={form.values.name} onChange={(v) => form.set("name", v)} error={form.errors.name} autoFocus />
        <TextField label="Slug" value={form.values.slug} onChange={(v) => form.set("slug", v)} error={form.errors.slug} />
        <NumberField label="Monthly budget (USD)" value={form.values.budget_usd} onChange={(v) => form.set("budget_usd", v)} hint="Hard limit; children can only narrow it." />
      </FormDialog>
      <ConfirmDialog open={!!deleting} title="Delete organization" message={`Delete “${deleting?.name}”? Its teams, projects and keys stop working.`} onConfirm={() => remove.mutateAsync(deleting!.id)} onClose={() => setDeleting(null)} />
    </>
  );
}
