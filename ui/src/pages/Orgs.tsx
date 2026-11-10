import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { useState } from "react";

import { api, hasPolicy, type Org, type OrgAdmin, type TeamAny } from "../api";
import { Badge, fmtDate, fmtUsd, InheritedBadge, PageHeader } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { FormDialog, required, TextField, useForm } from "../components/Form";
import { blankPolicy, Budget, PolicyFields, policyBody, policyFrom, PolicySummary, POLICY_ALIAS, type PolicyValues } from "../components/Policy";
import { CreateButton, RowActions } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { Card } from "../components/ui/card";
import { ComboboxField } from "../components/ui/combobox-field";
import { DescriptionDetails, DescriptionList, DescriptionTerm } from "../components/ui/description-list";
import { HelpTip } from "../components/ui/help-tip";
import { useCan, useMe, useMut, useOrgs, useTeams } from "../hooks";

type OrgValues = PolicyValues & { name: string };

const blankOrg = (): OrgValues => ({ name: "", ...blankPolicy(true) });
const orgValues = (org: Org): OrgValues => ({ name: org.name, ...policyFrom(org, true) });

/** Create or rename/set the policy of an org; installation level only. */
function OrgEditor({ org, onClose }: { org: Org | "new"; onClose: () => void }) {
  const form = useForm<OrgValues>(org === "new" ? blankOrg() : orgValues(org), { name: required("Name") });
  const save = useMut(
    (values: OrgValues) =>
      org === "new"
        ? api.createOrg({ name: values.name, ...policyBody(values) })
        : api.updateOrg(org.id, { name: values.name, ...policyBody(values) }),
    org === "new" ? "Org created" : "Org saved",
  );
  const submit = async () => {
    if (!form.validate()) return false;
    return form.run(() => save.mutateAsync(form.values), POLICY_ALIAS);
  };
  return (
    <FormDialog
      open
      title={org === "new" ? "New org" : "Edit org"}
      submitLabel={org === "new" ? "Create" : "Save"}
      onClose={onClose}
      onSubmit={submit}
    >
      <TextField
        label="Name"
        value={form.values.name}
        onChange={(value) => form.set("name", value)}
        error={form.errors.name}
        autoFocus
      />
      <PolicyFields values={form.values} errors={form.errors} set={form.set} isTeam />

    </FormDialog>
  );
}

type TeamValues = PolicyValues & { name: string };
const blankTeam = (): TeamValues => ({ name: "", ...blankPolicy(true) });

/** A new team in one org; the org admin may set its totals and model list. */
function NewTeamDialog({ org, onClose }: { org: Org; onClose: () => void }) {
  const form = useForm<TeamValues>(blankTeam(), { name: required("Name") });
  const create = useMut(
    (values: TeamValues) => api.createTeam({ org_id: org.id, name: values.name, ...policyBody(values) }),
    "Team created",
  );
  const submit = async () => {
    if (!form.validate()) return false;
    return form.run(() => create.mutateAsync(form.values), POLICY_ALIAS);
  };
  return (
    <FormDialog open title={`New team in ${org.name}`} submitLabel="Create" onClose={onClose} onSubmit={submit}>
      <TextField
        label="Name"
        value={form.values.name}
        onChange={(value) => form.set("name", value)}
        error={form.errors.name}
        autoFocus
      />
      <PolicyFields values={form.values} errors={form.errors} set={form.set} isTeam parent={org} />
    </FormDialog>
  );
}

function AssignAdminDialog({ org, onClose }: { org: Org; onClose: () => void }) {
  const me = useMe();
  const admins = useQuery({ queryKey: ["org-admins", org.id], queryFn: () => api.orgAdmins(org.id) });
  const users = useQuery({ queryKey: ["user-directory"], queryFn: () => api.users() });
  const form = useForm<{ user_id: string }>({ user_id: "" }, { user_id: (value) => (value ? undefined : "Choose a user") });
  const assign = useMut((id: string) => api.putOrgAdmin(org.id, id), "Org admin assigned");
  // An installation admin is listed by inheritance and may still be given a local grant.
  const existing = new Set((admins.data ?? []).filter((admin) => admin.direct).map((admin) => admin.user_id));
  const options = (users.data ?? [])
    .filter((user) => user.is_active && !existing.has(user.id))
    .map((user) => ({ value: user.id, label: user.email }));
  const readOnly = me.data?.manages_grants === false;
  return (
    <FormDialog
      open
      title={`Add an org admin to ${org.name}`}
      submitLabel="Add org admin"
      submitDisabled={readOnly || options.length === 0}
      onClose={onClose}
      onSubmit={async () => {
        if (!form.validate()) return false;
        return form.run(() => assign.mutateAsync(form.values.user_id));
      }}
    >
      {readOnly ? (
        <Alert variant="warn" role="status">
          <AlertDescription>Membership is managed by your identity provider / IAM.</AlertDescription>
        </Alert>
      ) : options.length ? (
        <ComboboxField
          label="User"
          value={form.values.user_id}
          onChange={(value) => form.set("user_id", value)}
          error={form.errors.user_id}
          options={[{ value: "", label: "Select a user" }, ...options]}
          hint="A user must be active and have signed in once before being assigned as an org admin."
        />
      ) : (
        <p className="m-0 text-sm text-muted-foreground">
          No eligible users: every active user who has signed in is already an org admin.
        </p>
      )}
    </FormDialog>
  );
}

export function Orgs() {
  const perms = useCan();
  const navigate = useNavigate();
  const teams = useTeams();
  const orgs = useOrgs(perms.isOrgAdmin);
  const [editing, setEditing] = useState<Org | "new" | null>(null);
  const [deleting, setDeleting] = useState<Org | null>(null);
  const remove = useMut((id: string) => api.deleteOrg(id), "Org archived");
  const liveTeams = (orgId: string) => (teams.data ?? []).filter((team) => team.org_id === orgId).length;

  return (
    <>
      <PageHeader
        title="Orgs"
        actions={
          <CreateButton can={perms.canManageOrg} onClick={() => setEditing("new")}>
            New org
          </CreateButton>
        }
      />
      <DataTable
        rows={orgs.data}
        loading={orgs.isPending || teams.isPending}
        error={orgs.error ?? teams.error}
        onRetry={() => {
          void orgs.refetch();
          void teams.refetch();
        }}
        rowKey={(org) => org.id}
        empty="No orgs yet."
        columns={[
          {
            key: "name",
            header: "Name",
            sort: (a, b) => a.name.localeCompare(b.name),
            cell: (org) => (
              <Link to="/orgs/$orgId" params={{ orgId: org.id }}>
                <strong>{org.name}</strong>
              </Link>
            ),
          },
          { key: "teams", header: "Teams", align: "right", cell: (org) => liveTeams(org.id).toLocaleString() },
          { key: "policy", header: "Policy", cell: (org) => <PolicySummary p={org} /> },
          { key: "budget", header: "Budget", cell: (org) => <Budget p={org} /> },
          {
            key: "actions",
            header: "",
            cell: (org) => {
              const live = liveTeams(org.id);
              return (
                <RowActions
                  canEdit={perms.canManageOrg}
                  canDelete={perms.canManageOrg && !teams.isPending && live === 0}
                  deleteLabel="Archive"
                  deleteReason={
                    !perms.canManageOrg
                      ? "Only an installation admin can archive an org"
                      : teams.isPending
                        ? "Loading the org's teams…"
                        : `This org still has ${live} live ${live === 1 ? "team" : "teams"}; archive them first`
                  }
                  onEdit={() => void navigate({ to: "/orgs/$orgId", params: { orgId: org.id } })}
                  onDelete={() => setDeleting(org)}
                />
              );
            },
          },
        ]}
      />
      {editing && <OrgEditor key={editing === "new" ? "new" : editing.id} org={editing} onClose={() => setEditing(null)} />}
      <ConfirmDialog
        open={!!deleting}
        title="Archive org"
        confirmLabel="Archive"
        message={`Archive “${deleting?.name}”? This cannot be undone. An org can only be archived when it has no live team, and no team may be created in it afterwards.`}
        onConfirm={() => remove.mutateAsync(deleting!.id)}
        onClose={() => setDeleting(null)}
      />
    </>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <Card className="gap-0 p-3.5">
      <div className="text-muted-foreground">{label}</div>
      <div className="text-2xl font-bold tabular-nums">{value}</div>
    </Card>
  );
}

/** The org detail: policy, teams, org admins, and this month's usage by team. */
export function OrgDetail() {
  const { orgId } = useParams({ strict: false }) as { orgId: string };
  const perms = useCan();
  const me = useMe();
  const navigate = useNavigate();
  const org = useQuery({ queryKey: ["org", orgId], queryFn: () => api.org(orgId) });
  const teams = useTeams();
  const usage = useQuery({ queryKey: ["org-usage", orgId], queryFn: () => api.orgUsage(orgId) });
  const admins = useQuery({
    queryKey: ["org-admins", orgId],
    queryFn: () => api.orgAdmins(orgId),
    enabled: perms.canManageOrg,
  });
  const [editing, setEditing] = useState<Org | "new" | null>(null);
  const [creatingTeam, setCreatingTeam] = useState(false);
  const [archivingTeam, setArchivingTeam] = useState<TeamAny | null>(null);
  const [assigning, setAssigning] = useState(false);
  const [removingAdmin, setRemovingAdmin] = useState<OrgAdmin | null>(null);
  const [archivingOrg, setArchivingOrg] = useState(false);
  const removeTeam = useMut((id: string) => api.deleteTeam(id), "Team archived");
  const removeOrg = useMut(
    (id: string) => api.deleteOrg(id),
    () => {
      void navigate({ to: "/orgs" });
    },
  );
  const removeAdmin = useMut(
    (admin: OrgAdmin) => api.deleteOrgAdmin(orgId, admin.user_id),
    "Org admin removed",
  );

  if (org.isPending) return <div className="p-8 text-center text-muted-foreground" aria-busy="true">Loading org…</div>;
  if (org.error || !org.data) {
    return (
      <Alert variant="destructive" data-testid="state-error">
        <AlertDescription>{org.error?.message ?? "Org not found."}</AlertDescription>
      </Alert>
    );
  }
  const data = org.data;
  const orgTeams = (teams.data ?? []).filter((team) => team.org_id === data.id);
  const readOnly = me.data?.manages_grants === false;
  const usageRows = usage.data?.teams;

  return (
    <>
      <PageHeader
        title={data.name}
        hint="Org policy caps every team below it and can only be changed by an installation admin."
        actions={
          <>
            <Button variant="outline" asChild>
              <Link to="/orgs">All orgs</Link>
            </Button>
            {perms.canManageOrg && !data.deleted_at && <Button onClick={() => setEditing(data)}>Edit org</Button>}
          </>
        }
      />
      <Card className="mb-5 gap-3 p-4 shadow-none">
        <DescriptionList>
          <DescriptionTerm>Name</DescriptionTerm>
          <DescriptionDetails>{data.name}</DescriptionDetails>
          <DescriptionTerm>Status</DescriptionTerm>
          <DescriptionDetails>
            <Badge tone={data.deleted_at ? "danger" : "ok"}>{data.deleted_at ? "Archived" : "Active"}</Badge>
          </DescriptionDetails>
          <DescriptionTerm>Policy</DescriptionTerm>
          <DescriptionDetails><PolicySummary p={data} /></DescriptionDetails>
          <DescriptionTerm>Budget</DescriptionTerm>
          <DescriptionDetails><Budget p={data} /></DescriptionDetails>
          <DescriptionTerm>Created</DescriptionTerm>
          <DescriptionDetails>{fmtDate(data.created_at)}</DescriptionDetails>
        </DescriptionList>
        {!perms.canManageOrg && <p className="m-0 text-[13px] text-muted-foreground">The org policy is read-only for an org admin; an installation admin changes it.</p>}
      </Card>

      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <h2 className="m-0">Teams</h2>
        <CreateButton can={!data.deleted_at && perms.canCreateTeamInOrg(data.id)} onClick={() => setCreatingTeam(true)}>
          New team
        </CreateButton>
      </div>
      <DataTable
        rows={orgTeams}
        loading={teams.isPending}
        error={teams.error}
        onRetry={() => void teams.refetch()}
        rowKey={(team) => team.id}
        empty="No live teams in this org."
        columns={[
          {
            key: "name",
            header: "Name",
            sort: (a, b) => a.name.localeCompare(b.name),
            cell: (team) => (
              <Link to="/teams/$teamId" params={{ teamId: team.id }}>
                <strong>{team.name}</strong>
              </Link>
            ),
          },
          { key: "policy", header: "Policy", cell: (team) => (hasPolicy(team) ? <PolicySummary p={team} /> : "—") },
          {
            key: "actions",
            header: "",
            cell: (team) => (
              <RowActions
                canEdit={perms.canSetTeamQuota(team.id)}
                canDelete={perms.canDeleteTeam(team.id)}
                deleteLabel="Archive"
                deleteReason="Only an org admin or an installation admin can archive a team"
                onEdit={() => void navigate({ to: "/teams/$teamId", params: { teamId: team.id } })}
                onDelete={() => setArchivingTeam(team)}
              />
            ),
          },
        ]}
      />

      {perms.canManageOrg && (
        <>
          <div className="mt-6 mb-3 flex flex-wrap items-center justify-between gap-3">
            <h2 className="m-0">Org admins</h2>
            <CreateButton can={!readOnly} onClick={() => setAssigning(true)}>
              Add org admin
            </CreateButton>
          </div>
          {readOnly && (
            <Alert variant="warn" role="status" className="mb-3">
              <AlertDescription>Membership is managed by your identity provider / IAM.</AlertDescription>
            </Alert>
          )}
          <DataTable
            rows={admins.data}
            loading={admins.isPending}
            error={admins.error}
            onRetry={() => void admins.refetch()}
            rowKey={(admin) => admin.user_id}
            empty="No org admins assigned."
            columns={[
              {
                key: "email",
                header: "Email",
                sort: (a, b) => a.email.localeCompare(b.email),
                cell: (admin) => (
                  <>
                    <strong>{admin.email}</strong> <InheritedBadge from={admin.inherited_from} />
                  </>
                ),
              },
              { key: "name", header: "Name", cell: (admin) => admin.display_name || "—" },
              {
                key: "status",
                header: "Status",
                cell: (admin) => <Badge tone={admin.is_active ? "ok" : "danger"}>{admin.is_active ? "Active" : "Inactive"}</Badge>,
              },
              { key: "assigned", header: "Assigned", cell: (admin) => fmtDate(admin.assigned_at) },
              {
                key: "actions",
                header: "",
                cell: (admin) =>
                  // An installation admin without a local grant is removed at the installation level.
                  !admin.direct ? null : (
                  <RowActions
                    canDelete={!readOnly}
                    deleteLabel="Remove"
                    deleteReason={readOnly ? "Membership is managed by your identity provider / IAM" : "Not permitted for your role"}
                    onDelete={() => setRemovingAdmin(admin)}
                  />
                ),
              },
            ]}
          />
        </>
      )}

      <div className="mt-6 mb-3">
        <h2 className="m-0 flex items-center gap-1">
          Usage this month
          <HelpTip label="Usage this month" text="Usage follows the UTC month and includes archived teams." />
        </h2>
      </div>
      <div className="mb-4 grid grid-cols-[repeat(auto-fit,minmax(160px,1fr))] gap-3">
        <Metric label="Requests" value={(usage.data?.requests ?? 0).toLocaleString()} />
        <Metric label="Total tokens" value={(usage.data?.total_tokens ?? 0).toLocaleString()} />
        <Metric label="Spend" value={fmtUsd(usage.data?.cost_nano_usd ?? 0)} />
      </div>
      <DataTable
        rows={usageRows}
        loading={usage.isPending}
        error={usage.error}
        onRetry={() => void usage.refetch()}
        rowKey={(row) => row.team_id}
        empty="No teams in this org."
        columns={[
          {
            key: "team",
            header: "Team",
            sort: (a, b) => a.team_name.localeCompare(b.team_name),
            cell: (row) => (
              <span className="flex flex-wrap items-center gap-2">
                <strong>{row.team_name}</strong>
                {row.archived && <Badge tone="muted">Archived</Badge>}
              </span>
            ),
          },
          { key: "requests", header: "Requests", align: "right", cell: (row) => row.requests.toLocaleString() },
          { key: "tokens", header: "Tokens", align: "right", cell: (row) => row.total_tokens.toLocaleString() },
          { key: "cost", header: "Spend", align: "right", cell: (row) => fmtUsd(row.cost_nano_usd) },
        ]}
      />

      {editing && <OrgEditor key={editing === "new" ? "new" : editing.id} org={editing} onClose={() => setEditing(null)} />}
      {creatingTeam && <NewTeamDialog org={data} onClose={() => setCreatingTeam(false)} />}
      <ConfirmDialog
        open={!!archivingTeam}
        title="Archive team"
        confirmLabel="Archive"
        message={`Archive “${archivingTeam?.name}”? This cannot be undone. Its keys are revoked, its service accounts deactivated, and the name becomes free again.`}
        onConfirm={() => removeTeam.mutateAsync(archivingTeam!.id)}
        onClose={() => setArchivingTeam(null)}
      />
      {assigning && <AssignAdminDialog org={data} onClose={() => setAssigning(false)} />}
      <ConfirmDialog
        open={!!removingAdmin}
        title="Remove org admin"
        confirmLabel="Remove"
        message={`Remove ${removingAdmin?.email} as an org admin of ${data.name}? Their installation role is unchanged.`}
        onConfirm={() => removeAdmin.mutateAsync(removingAdmin!)}
        onClose={() => setRemovingAdmin(null)}
      />
      <ConfirmDialog
        open={archivingOrg}
        title="Archive org"
        confirmLabel="Archive"
        message={`Archive “${data.name}”? This cannot be undone. An org can only be archived when it has no live team, and no team may be created in it afterwards.`}
        onConfirm={() => removeOrg.mutateAsync(data.id)}
        onClose={() => setArchivingOrg(false)}
      />
      {perms.canManageOrg && !data.deleted_at && (
        <div className="mt-6 flex flex-wrap items-center gap-2">
          <Button
            variant="outline"
            className="text-destructive hover:bg-destructive/10 hover:text-destructive"
            disabled={orgTeams.length > 0}
            title={orgTeams.length > 0 ? "Archive every live team in this org first" : undefined}
            onClick={() => setArchivingOrg(true)}
          >
            Archive org
          </Button>
          {orgTeams.length > 0 && (
            <span className="text-sm text-muted-foreground">
              An org can only be archived when it has no live team.
            </span>
          )}
        </div>
      )}
    </>
  );
}
