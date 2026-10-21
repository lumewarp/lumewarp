import { useState } from "react";
import { api, type Team } from "../api";
import { PageHeader } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { FormDialog, required, SelectField, TextField, useForm } from "../components/Form";
import { blankPolicy, Budget, PolicyFields, policyBody, policyFrom, PolicySummary, POLICY_ALIAS, type PolicyValues } from "../components/Policy";
import { CreateButton, RowActions } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { useCan, useMut, useNames, useTree } from "../hooks";

type Values = PolicyValues & { name: string; org_id: string };
const blank: Values = { name: "", org_id: "", ...blankPolicy() };

export function Teams() {
  const perms = useCan();
  const tree = useTree();
  const orgName = useNames(tree.data?.orgs);
  const [editing, setEditing] = useState<Team | "new" | null>(null);
  const [deleting, setDeleting] = useState<Team | null>(null);
  const form = useForm<Values>(blank, { name: required("Name"), org_id: required("Organization") });
  const create = useMut((v: Values) => api.createTeam(v.org_id, { name: v.name, ...policyBody(v) }), "Team created");
  const update = useMut(({ id, v }: { id: string; v: Values }) => api.updateTeam(id, { name: v.name, ...policyBody(v) }), "Team saved");
  const remove = useMut((id: string) => api.deleteTeam(id), "Team deleted");

  const creatableOrgs = (tree.data?.orgs ?? []).filter((o) => perms.orgAdmin(o.id));
  const open = (t: Team | "new") => {
    form.reset(t === "new" ? { ...blank, org_id: creatableOrgs[0]?.id ?? "" } : { name: t.name, org_id: t.org_id, ...policyFrom(t) });
    setEditing(t);
  };
  const submit = async () => {
    if (!form.validate()) return false;
    return form.run(() => (editing === "new" ? create.mutateAsync(form.values) : update.mutateAsync({ id: (editing as Team).id, v: form.values })), POLICY_ALIAS);
  };

  return (
    <>
      <PageHeader title="Teams" description="Teams own projects. Limits and budgets here must stay within the organization's." actions={<CreateButton can={creatableOrgs.length > 0} onClick={() => open("new")}>New team</CreateButton>} />
      <DataTable
        rows={tree.data?.teams}
        loading={tree.isPending}
        error={tree.error}
        onRetry={() => void tree.refetch()}
        rowKey={(r) => r.id}
        empty="No teams yet."
        columns={[
          { key: "name", header: "Name", sort: (a, b) => a.name.localeCompare(b.name), cell: (r) => <strong>{r.name}</strong> },
          { key: "org", header: "Organization", sort: (a, b) => orgName(a.org_id).localeCompare(orgName(b.org_id)), cell: (r) => orgName(r.org_id) },
          { key: "policy", header: "Policy", cell: (r) => <PolicySummary p={r} /> },
          { key: "budget", header: "Budget", cell: (r) => <Budget p={r} /> },
          { key: "actions", header: "", cell: (r) => <RowActions canEdit={perms.teamAdmin(r)} canDelete={perms.orgAdmin(r.org_id)} onEdit={() => open(r)} onDelete={() => setDeleting(r)} /> },
        ]}
      />
      <FormDialog open={!!editing} title={editing === "new" ? "New team" : "Edit team"} submitLabel={editing === "new" ? "Create" : "Save"} onClose={() => setEditing(null)} onSubmit={submit}>
        {editing === "new" && <SelectField label="Organization" value={form.values.org_id} onChange={(v) => form.set("org_id", v)} error={form.errors.org_id} options={creatableOrgs.map((o) => ({ value: o.id, label: o.name }))} />}
        <TextField label="Name" value={form.values.name} onChange={(v) => form.set("name", v)} error={form.errors.name} autoFocus />
        <PolicyFields values={form.values} errors={form.errors} set={form.set} />
      </FormDialog>
      <ConfirmDialog open={!!deleting} title="Delete team" message={`Delete “${deleting?.name}”? It must have no projects.`} onConfirm={() => remove.mutateAsync(deleting!.id)} onClose={() => setDeleting(null)} />
    </>
  );
}
