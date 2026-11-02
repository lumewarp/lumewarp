import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { api, type Team } from "../api";
import { PageHeader } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { FormDialog, required, TextField, useForm } from "../components/Form";
import { blankPolicy, Budget, PolicyFields, policyBody, policyFrom, PolicySummary, POLICY_ALIAS, type PolicyValues } from "../components/Policy";
import { CreateButton, RowActions } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { useCan, useMut, useTeams } from "../hooks";

type Values = PolicyValues & { name: string };
const blank: Values = { name: "", ...blankPolicy(true) };

export function Teams() {
  const perms = useCan();
  const teams = useTeams();
  const [editing, setEditing] = useState<Team | "new" | null>(null);
  const [deleting, setDeleting] = useState<Team | null>(null);
  const form = useForm<Values>(blank, { name: required("Name") });
  const create = useMut((v: Values) => api.createTeam({ name: v.name, ...policyBody(v) }), "Team created");
  const update = useMut(({ id, v }: { id: string; v: Values }) => api.updateTeam(id, { name: v.name, ...policyBody(v) }), "Team saved");
  const remove = useMut((id: string) => api.deleteTeam(id), "Team deleted");

  const open = (team: Team | "new") => {
    form.reset(team === "new" ? blank : { name: team.name, ...policyFrom(team, true) });
    setEditing(team);
  };
  const submit = async () => {
    if (!form.validate()) return false;
    return form.run(
      () => (editing === "new" ? create.mutateAsync(form.values) : update.mutateAsync({ id: (editing as Team).id, v: form.values })),
      POLICY_ALIAS,
    );
  };

  return (
    <>
      <PageHeader
        title="Teams"
        description="Teams group members, service accounts, keys, and usage."
        actions={<CreateButton can={perms.isSuper} onClick={() => open("new")}>New team</CreateButton>}
      />
      <DataTable
        rows={teams.data}
        loading={teams.isPending}
        error={teams.error}
        onRetry={() => void teams.refetch()}
        rowKey={(row) => row.id}
        empty="No teams yet."
        columns={[
          {
            key: "name",
            header: "Name",
            sort: (a, b) => a.name.localeCompare(b.name),
            cell: (row) => <Link to="/teams/$teamId" params={{ teamId: row.id }}><strong>{row.name}</strong></Link>,
          },
          { key: "policy", header: "Policy", cell: (row) => <PolicySummary p={row} /> },
          { key: "budget", header: "Budget", cell: (row) => <Budget p={row} /> },
          {
            key: "actions",
            header: "",
            cell: (row) => (
              <RowActions
                canEdit={perms.teamAdmin(row.id)}
                canDelete={perms.isSuper}
                onEdit={() => open(row)}
                onDelete={() => setDeleting(row)}
              />
            ),
          },
        ]}
      />
      <FormDialog
        open={!!editing}
        title={editing === "new" ? "New team" : "Edit team"}
        submitLabel={editing === "new" ? "Create" : "Save"}
        onClose={() => setEditing(null)}
        onSubmit={submit}
      >
        <TextField label="Name" value={form.values.name} onChange={(value) => form.set("name", value)} error={form.errors.name} autoFocus />
        <PolicyFields values={form.values} errors={form.errors} set={form.set} isTeam />
      </FormDialog>
      <ConfirmDialog
        open={!!deleting}
        title="Delete team"
        message={`Delete “${deleting?.name}”? It must have no keys or service accounts.`}
        onConfirm={() => remove.mutateAsync(deleting!.id)}
        onClose={() => setDeleting(null)}
      />
    </>
  );
}
