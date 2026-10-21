import { useState } from "react";
import { api, type Project } from "../api";
import { PageHeader } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { FormDialog, required, SelectField, TextField, useForm } from "../components/Form";
import { blankPolicy, Budget, PolicyFields, policyBody, policyFrom, PolicySummary, POLICY_ALIAS, type PolicyValues } from "../components/Policy";
import { CreateButton, RowActions } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { useCan, useMut, useNames, useTree } from "../hooks";

type Values = PolicyValues & { name: string; team_id: string };
const blank: Values = { name: "", team_id: "", ...blankPolicy() };

export function Projects() {
  const perms = useCan();
  const tree = useTree();
  const teamName = useNames(tree.data?.teams);
  const [editing, setEditing] = useState<Project | "new" | null>(null);
  const [deleting, setDeleting] = useState<Project | null>(null);
  const form = useForm<Values>(blank, { name: required("Name"), team_id: required("Team") });
  const create = useMut((v: Values) => api.createProject(v.team_id, { name: v.name, ...policyBody(v) }), "Project created");
  const update = useMut(({ id, v }: { id: string; v: Values }) => api.updateProject(id, { name: v.name, ...policyBody(v) }), "Project saved");
  const remove = useMut((id: string) => api.deleteProject(id), "Project deleted");

  const teamsOf = (p: { team_id: string }) => tree.data?.teams.find((t) => t.id === p.team_id);
  const creatableTeams = (tree.data?.teams ?? []).filter((t) => perms.teamAdmin(t));
  const mayWrite = (p: Project) => {
    const t = teamsOf(p);
    return !!t && perms.teamAdmin(t);
  };
  const open = (p: Project | "new") => {
    form.reset(p === "new" ? { ...blank, team_id: creatableTeams[0]?.id ?? "" } : { name: p.name, team_id: p.team_id, ...policyFrom(p) });
    setEditing(p);
  };
  const submit = async () => {
    if (!form.validate()) return false;
    return form.run(() => (editing === "new" ? create.mutateAsync(form.values) : update.mutateAsync({ id: (editing as Project).id, v: form.values })), POLICY_ALIAS);
  };

  return (
    <>
      <PageHeader title="Projects" description="Projects group API keys under a team." actions={<CreateButton can={creatableTeams.length > 0} onClick={() => open("new")}>New project</CreateButton>} />
      <DataTable
        rows={tree.data?.projects}
        loading={tree.isPending}
        error={tree.error}
        onRetry={() => void tree.refetch()}
        rowKey={(r) => r.id}
        empty="No projects yet."
        columns={[
          { key: "name", header: "Name", sort: (a, b) => a.name.localeCompare(b.name), cell: (r) => <strong>{r.name}</strong> },
          { key: "team", header: "Team", sort: (a, b) => teamName(a.team_id).localeCompare(teamName(b.team_id)), cell: (r) => teamName(r.team_id) },
          { key: "policy", header: "Policy", cell: (r) => <PolicySummary p={r} /> },
          { key: "budget", header: "Budget", cell: (r) => <Budget p={r} /> },
          { key: "actions", header: "", cell: (r) => <RowActions canEdit={mayWrite(r)} canDelete={mayWrite(r)} onEdit={() => open(r)} onDelete={() => setDeleting(r)} /> },
        ]}
      />
      <FormDialog open={!!editing} title={editing === "new" ? "New project" : "Edit project"} submitLabel={editing === "new" ? "Create" : "Save"} onClose={() => setEditing(null)} onSubmit={submit}>
        {editing === "new" && <SelectField label="Team" value={form.values.team_id} onChange={(v) => form.set("team_id", v)} error={form.errors.team_id} options={creatableTeams.map((t) => ({ value: t.id, label: t.name }))} />}
        <TextField label="Name" value={form.values.name} onChange={(v) => form.set("name", v)} error={form.errors.name} autoFocus />
        <PolicyFields values={form.values} errors={form.errors} set={form.set} />
      </FormDialog>
      <ConfirmDialog open={!!deleting} title="Delete project" message={`Delete “${deleting?.name}”? Projects that ever had keys cannot be deleted.`} onConfirm={() => remove.mutateAsync(deleting!.id)} onClose={() => setDeleting(null)} />
    </>
  );
}
