import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { api, hasPolicy, type TeamAny } from "../api";
import { PageHeader } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { FormDialog, required, SelectField, TextField, useForm } from "../components/Form";
import { blankPolicy, Budget, PolicyFields, policyBody, policyFrom, PolicySummary, POLICY_ALIAS, type PolicyValues } from "../components/Policy";
import { CreateButton, RowActions } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { useCan, useMut, useNames, useOrgs, useTeams } from "../hooks";

type Values = PolicyValues & { name: string; org_id: string };
const blank: Values = { name: "", org_id: "", ...blankPolicy(true) };

export function Teams() {
  const perms = useCan();
  const teams = useTeams();
  // Team creation is org level. With exactly one org the dialog picks it; several orgs
  // need an explicit choice. The org layer (the column) is hidden for a single-org
  // installation, except on the Orgs page itself.
  const orgs = useOrgs(perms.canCreateTeam);
  const showOrg = perms.isOrgAdmin && (orgs.data?.length ?? 0) > 1;
  const orgName = useNames(orgs.data);
  const [editing, setEditing] = useState<TeamAny | "new" | null>(null);
  const [deleting, setDeleting] = useState<TeamAny | null>(null);
  const form = useForm<Values>(blank, { name: required("Name") });
  const create = useMut(
    (v: Values) =>
      api.createTeam({ org_id: v.org_id || orgs.data?.[0]?.id || "", name: v.name, ...policyBody(v) }),
    "Team created",
  );
  // A team admin may rename a team; its quota stays with the team's org admins.
  const update = useMut(
    ({ id, v }: { id: string; v: Values }) =>
      api.updateTeam(id, perms.canSetTeamQuota(id) ? { name: v.name, ...policyBody(v) } : { name: v.name }),
    "Team saved",
  );
  const remove = useMut((id: string) => api.deleteTeam(id), "Team archived");

  const open = (team: TeamAny | "new") => {
    form.reset(
      team === "new"
        ? { ...blank, org_id: orgs.data?.[0]?.id ?? "" }
        : { name: team.name, org_id: team.org_id, ...(hasPolicy(team) ? policyFrom(team, true) : blankPolicy(true)) },
    );
    setEditing(team);
  };
  // With no org yet there is nothing to create a team in; with one it is picked for the
  // caller. Keep Create disabled until the org list answers so the body never carries "".
  const orgReady = editing !== "new" || !!form.values.org_id || !!orgs.data?.[0];
  const submit = async () => {
    if (!form.validate()) return false;
    return form.run(
      () => (editing === "new" ? create.mutateAsync(form.values) : update.mutateAsync({ id: (editing as TeamAny).id, v: form.values })),
      POLICY_ALIAS,
    );
  };

  return (
    <>
      <PageHeader
        title="Teams"
        description="Teams group members, service accounts, keys, and usage."
        actions={<CreateButton can={perms.canCreateTeam} onClick={() => open("new")}>New team</CreateButton>}
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
          ...(showOrg ? [{ key: "org", header: "Org", cell: (row: TeamAny) => orgName(row.org_id) }] : []),
          { key: "policy", header: "Policy", cell: (row) => (hasPolicy(row) ? <PolicySummary p={row} /> : "—") },
          { key: "budget", header: "Budget", cell: (row) => (hasPolicy(row) ? <Budget p={row} /> : "—") },
          {
            key: "actions",
            header: "",
            cell: (row) => (
              <RowActions
                canEdit={perms.teamAdmin(row.id)}
                canDelete={perms.canDeleteTeam(row.id)}
                deleteLabel="Archive"
                deleteReason="Only an org admin or an installation admin can archive a team"
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
        submitDisabled={!orgReady}
        onClose={() => setEditing(null)}
        onSubmit={submit}
      >
        <TextField label="Name" value={form.values.name} onChange={(value) => form.set("name", value)} error={form.errors.name} autoFocus />
        {editing === "new" && (orgs.data?.length ?? 0) > 1 && (
          <SelectField
            label="Org"
            value={form.values.org_id}
            onChange={(value) => form.set("org_id", value)}
            options={(orgs.data ?? []).map((org) => ({ value: org.id, label: org.name }))}
          />
        )}
        {editing === "new" && orgs.data?.length === 1 && (
          <p className="m-0 text-sm text-muted-foreground">Created in {orgs.data[0].name}.</p>
        )}
        {editing === "new" && orgs.data?.length === 0 && (
          <p className="m-0 text-sm text-muted-foreground">
            There is no org yet. An installation admin can create one, then a team belongs to it.
          </p>
        )}
        {(editing === "new" ? perms.canCreateTeam : editing ? perms.canSetTeamQuota(editing.id) : false) && (
          <PolicyFields values={form.values} errors={form.errors} set={form.set} isTeam />
        )}
      </FormDialog>
      <ConfirmDialog
        open={!!deleting}
        title="Archive team"
        confirmLabel="Archive"
        message={`Archive “${deleting?.name}”? This cannot be undone. Its keys are revoked, its service accounts deactivated, and the name becomes free again.`}
        onConfirm={() => remove.mutateAsync(deleting!.id)}
        onClose={() => setDeleting(null)}
      />
    </>
  );
}
