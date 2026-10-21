import { useState } from "react";
import { api, type Project } from "../api";
import { BudgetBar, fmtDate, PageHeader } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { FormDialog, NumberField, required, SelectField, TextField, useForm } from "../components/Form";
import { CreateButton, RowActions } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { useCrud, useList, useNames } from "../hooks";

const blank = { team_id: "", name: "", budget_usd: null as number | null };

export function Projects() {
  const { list, create, update, remove } = useCrud("projects", api.projects, "Project");
  const teams = useList("teams", () => api.teams.list());
  const teamName = useNames(teams.data);
  const [editing, setEditing] = useState<Project | "new" | null>(null);
  const [deleting, setDeleting] = useState<Project | null>(null);
  const form = useForm(blank, { name: required("Name"), team_id: required("Team") });

  const open = (p: Project | "new") => {
    form.reset(p === "new" ? { ...blank, team_id: teams.data?.[0]?.id ?? "" } : { team_id: p.team_id, name: p.name, budget_usd: p.budget_usd });
    setEditing(p);
  };
  const submit = async () => {
    if (!form.validate()) return false;
    if (editing === "new") await create.mutateAsync(form.values);
    else if (editing) await update.mutateAsync({ id: editing.id, input: form.values });
  };

  return (
    <>
      <PageHeader title="Projects" description="Keys belong to a project; its budget caps all of them together." actions={<CreateButton onClick={() => open("new")}>New project</CreateButton>} />
      <DataTable
        rows={list.data}
        loading={list.isPending}
        error={list.error}
        onRetry={() => void list.refetch()}
        rowKey={(r) => r.id}
        empty="No projects yet."
        columns={[
          { key: "name", header: "Project", sort: (a, b) => a.name.localeCompare(b.name), cell: (r) => <strong>{r.name}</strong> },
          { key: "team", header: "Team", sort: (a, b) => teamName(a.team_id).localeCompare(teamName(b.team_id)), cell: (r) => teamName(r.team_id) },
          { key: "budget", header: "Monthly budget", cell: (r) => <BudgetBar spent={r.spent_usd} budget={r.budget_usd} /> },
          { key: "created", header: "Created", cell: (r) => fmtDate(r.created_at) },
          { key: "actions", header: "", cell: (r) => <RowActions onEdit={() => open(r)} onDelete={() => setDeleting(r)} /> },
        ]}
      />
      <FormDialog open={!!editing} title={editing === "new" ? "New project" : "Edit project"} onClose={() => setEditing(null)} onSubmit={submit}>
        <SelectField label="Team" value={form.values.team_id} onChange={(v) => form.set("team_id", v)} error={form.errors.team_id} options={(teams.data ?? []).map((t) => ({ value: t.id, label: t.name }))} />
        <TextField label="Name" value={form.values.name} onChange={(v) => form.set("name", v)} error={form.errors.name} autoFocus />
        <NumberField label="Monthly budget (USD)" value={form.values.budget_usd} onChange={(v) => form.set("budget_usd", v)} />
      </FormDialog>
      <ConfirmDialog open={!!deleting} title="Delete project" message={`Delete project “${deleting?.name}” and its keys?`} onConfirm={() => remove.mutateAsync(deleting!.id)} onClose={() => setDeleting(null)} />
    </>
  );
}
