import { useState } from "react";
import { api, type Org } from "../api";
import { fmtDate, PageHeader } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { FormDialog, required, TextField, useForm } from "../components/Form";
import { blankPolicy, Budget, PolicyFields, policyBody, policyFrom, PolicySummary, POLICY_ALIAS, type PolicyValues } from "../components/Policy";
import { CreateButton, RowActions } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { useCan, useMut, useTree } from "../hooks";

type Values = PolicyValues & { name: string };
const blank: Values = { name: "", ...blankPolicy(true) };

export function Orgs() {
  const perms = useCan();
  const tree = useTree();
  const [editing, setEditing] = useState<Org | "new" | null>(null);
  const [deleting, setDeleting] = useState<Org | null>(null);
  const form = useForm<Values>(blank, { name: required("Name") });
  const create = useMut((v: Values) => api.createOrg({ name: v.name, ...policyBody(v) }), "Organization created");
  const update = useMut(({ id, v }: { id: string; v: Values }) => api.updateOrg(id, { name: v.name, ...policyBody(v) }), "Organization saved");
  const remove = useMut((id: string) => api.deleteOrg(id), "Organization deleted");

  const open = (o: Org | "new") => {
    form.reset(o === "new" ? blank : { name: o.name, ...policyFrom(o) });
    setEditing(o);
  };
  const submit = async () => {
    if (!form.validate()) return false;
    return form.run(() => (editing === "new" ? create.mutateAsync(form.values) : update.mutateAsync({ id: (editing as Org).id, v: form.values })), POLICY_ALIAS);
  };

  return (
    <>
      <PageHeader title="Organizations" description="Top of the hierarchy. Teams, projects and keys can only narrow what their organization allows." actions={<CreateButton can={perms.isSuper} onClick={() => open("new")}>New organization</CreateButton>} />
      <DataTable
        rows={tree.data?.orgs}
        loading={tree.isPending}
        error={tree.error}
        onRetry={() => void tree.refetch()}
        rowKey={(r) => r.id}
        empty="No organizations yet."
        columns={[
          { key: "name", header: "Name", sort: (a, b) => a.name.localeCompare(b.name), cell: (r) => <strong>{r.name}</strong> },
          { key: "policy", header: "Policy", cell: (r) => <PolicySummary p={r} /> },
          { key: "budget", header: "Budget", cell: (r) => <Budget p={r} /> },
          { key: "created", header: "Created", sort: (a, b) => a.created_at.localeCompare(b.created_at), cell: (r) => fmtDate(r.created_at) },
          { key: "actions", header: "", cell: (r) => <RowActions canEdit={perms.orgAdmin(r.id)} canDelete={perms.isSuper} onEdit={() => open(r)} onDelete={() => setDeleting(r)} /> },
        ]}
      />
      <FormDialog open={!!editing} title={editing === "new" ? "New organization" : "Edit organization"} submitLabel={editing === "new" ? "Create" : "Save"} onClose={() => setEditing(null)} onSubmit={submit}>
        <TextField label="Name" value={form.values.name} onChange={(v) => form.set("name", v)} error={form.errors.name} autoFocus />
        <PolicyFields values={form.values} errors={form.errors} set={form.set} isOrg />
      </FormDialog>
      <ConfirmDialog open={!!deleting} title="Delete organization" message={`Delete “${deleting?.name}”? It must have no teams.`} onConfirm={() => remove.mutateAsync(deleting!.id)} onClose={() => setDeleting(null)} />
    </>
  );
}
