import { useParams } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, ApiError, type Key, type Member, type Schemas, type ServiceAccount, type Team } from "../api";
import { Badge, fmtDate, fmtUsd, KeyBadge, PageHeader, SecretOnce, SuspendedReason } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { Dialog } from "../components/Dialog";
import { FormDialog, required, SelectField, TextField, useForm } from "../components/Form";
import { blankPolicy, Budget, PolicyFields, policyBody, policyFrom, PolicySummary, POLICY_ALIAS, type PolicyValues } from "../components/Policy";
import { CreateButton, RowActions } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { useAction, useCan, useMe, useMut } from "../hooks";

const TABS = [
  { id: "members", label: "Members" },
  { id: "service-accounts", label: "Service accounts" },
  { id: "keys", label: "Keys" },
  { id: "usage", label: "Usage" },
  { id: "settings", label: "Settings" },
] as const;

type Tab = (typeof TABS)[number]["id"];

export function TeamDetail() {
  const { teamId } = useParams({ strict: false }) as { teamId: string };
  const [tab, setTab] = useState<Tab>("members");
  const [externallyManagedTeamId, setExternallyManagedTeamId] = useState<string | null>(null);
  const team = useQuery({ queryKey: ["team", teamId], queryFn: () => api.team(teamId), enabled: !!teamId });

  if (team.isPending) return <div className="state" aria-busy="true">Loading team…</div>;
  if (team.error || !team.data) {
    return <div className="state state-error" role="alert">{team.error?.message ?? "Team not found."}</div>;
  }

  return (
    <>
      <PageHeader
        title={team.data.name}
        description="Manage the people, service accounts, keys, and usage for this team."
        actions={<a className="btn" href="/teams">All teams</a>}
      />
      <nav className="tabs" role="tablist" aria-label="Team sections">
        {TABS.map((item) => (
          <button
            key={item.id}
            id={`team-tab-${item.id}`}
            className="tab-button"
            type="button"
            role="tab"
            aria-selected={tab === item.id}
            aria-controls="team-panel"
            onClick={() => setTab(item.id)}
          >
            {item.label}
          </button>
        ))}
      </nav>
      <section id="team-panel" role="tabpanel" aria-labelledby={`team-tab-${tab}`}>
        {tab === "members" && (
          <MembersPanel
            team={team.data}
            managedExternally={externallyManagedTeamId === teamId}
            onManagedExternally={() => setExternallyManagedTeamId(teamId)}
          />
        )}
        {tab === "service-accounts" && <ServiceAccountsPanel team={team.data} />}
        {tab === "keys" && <KeysPanel team={team.data} />}
        {tab === "usage" && <TeamUsage team={team.data} />}
        {tab === "settings" && <TeamSettings team={team.data} />}
      </section>
    </>
  );
}

type MemberValues = PolicyValues & { user_id: string; email: string; role: Schemas["TeamRole"] };
const blankMember: MemberValues = { user_id: "", email: "", role: "member", ...blankPolicy() };

function isManagedConflict(error: unknown) {
  return error instanceof ApiError && error.status === 409 && error.code === "managed_externally";
}

/** createMember lookup failures (404 user_not_found, 409 conflict) rendered on the email field. */
function memberLookupError(error: unknown) {
  if (!(error instanceof ApiError)) return undefined;
  if (error.status === 404 && error.code === "user_not_found") return "No user with this email address exists.";
  if (error.status === 409 && error.code === "conflict") {
    if (/not active/i.test(error.message)) return "This user is inactive and cannot be added.";
    if (/already a member/i.test(error.message)) return "This user is already a member of this team.";
    return error.message;
  }
  return undefined;
}

function MembersPanel({
  team,
  managedExternally,
  onManagedExternally,
}: {
  team: Team;
  managedExternally: boolean;
  onManagedExternally: () => void;
}) {
  const perms = useCan();
  const me = useMe();
  const isAdmin = perms.teamAdmin(team.id);
  const grantManagementDisabled =
    !!me.data && "manages_grants" in me.data && me.data.manages_grants === false;
  const memberWritesDisabled = managedExternally || grantManagementDisabled;
  const list = useQuery({ queryKey: ["members", team.id], queryFn: () => api.members(team.id) });
  const userDirectory = useQuery({
    queryKey: ["user-directory"],
    queryFn: () => api.users(),
    enabled: perms.isSuper,
  });
  const [editing, setEditing] = useState<Member | "new" | null>(null);
  const [deleting, setDeleting] = useState<Member | null>(null);
  const form = useForm<MemberValues>(blankMember, { email: required("Email") });
  const existingUserIds = new Set((list.data ?? []).map((member) => member.user_id));
  const matchingUsers = (userDirectory.data ?? []).filter(
    (user) =>
      user.is_active &&
      !existingUserIds.has(user.id) &&
      user.email.toLowerCase().includes(form.values.email.trim().toLowerCase()),
  );

  const save = useMut(async ({ member, values }: { member: Member | null; values: MemberValues }) => {
    try {
      if (member) {
        return await api.updateMember(team.id, member.user_id, { role: values.role, ...policyBody(values) });
      }
      return await api.createMember(team.id, {
        ...(values.user_id ? { user_id: values.user_id.trim() } : { email: values.email.trim() }),
        role: values.role,
        ...policyBody(values),
      });
    } catch (error) {
      if (isManagedConflict(error)) onManagedExternally();
      throw error;
    }
  }, "Member saved");
  const remove = useMut(async (member: Member) => {
    try {
      return await api.deleteMember(team.id, member.user_id);
    } catch (error) {
      if (isManagedConflict(error)) onManagedExternally();
      throw error;
    }
  }, "Member removed");

  const open = (member: Member | "new") => {
    form.reset(
      member === "new"
        ? blankMember
        : { user_id: member.user_id, email: member.email, role: member.role, ...policyFrom(member) },
    );
    setEditing(member);
  };
  const submit = async () => {
    if (memberWritesDisabled || !form.validate()) return false;
    try {
      return await form.run(() =>
        save.mutateAsync({
          member: editing === "new" ? null : (editing as Member),
          values: form.values,
        }),
        POLICY_ALIAS,
      );
    } catch (error) {
      // Lookup failures belong to the email field: keep the dialog open and say why inline.
      const message = editing === "new" ? memberLookupError(error) : undefined;
      if (!message) throw error;
      form.setError("email", message);
      return false;
    }
  };

  return (
    <>
      <div className="panel-head">
        <h2>Members</h2>
        <CreateButton can={isAdmin && !memberWritesDisabled} onClick={() => open("new")}>Add member</CreateButton>
      </div>
      {memberWritesDisabled && (
        <p className="form-warn" role="status">
          Membership is managed by your identity provider / IAM.
        </p>
      )}
      <DataTable
        rows={list.data}
        loading={list.isPending}
        error={list.error}
        onRetry={() => void list.refetch()}
        rowKey={(member) => member.user_id}
        empty="No members yet."
        columns={[
          { key: "email", header: "Email", sort: (a, b) => a.email.localeCompare(b.email), cell: (member) => <><strong>{member.email}</strong>{!member.user_active && <> <Badge tone="danger">Inactive</Badge></>}</> },
          { key: "name", header: "Name", cell: (member) => member.display_name || "—" },
          { key: "role", header: "Role", cell: (member) => <Badge tone="info">{member.role}</Badge> },
          { key: "policy", header: "Policy", cell: (member) => <PolicySummary p={member} /> },
          {
            key: "actions",
            header: "",
            cell: (member) => (
              <RowActions
                canEdit={isAdmin && !memberWritesDisabled}
                canDelete={isAdmin && !memberWritesDisabled}
                onEdit={() => open(member)}
                onDelete={() => setDeleting(member)}
              />
            ),
          },
        ]}
      />
      <FormDialog
        open={!!editing}
        title={editing === "new" ? "Add member" : "Edit member"}
        submitLabel={editing === "new" ? "Add member" : "Save"}
        onClose={() => setEditing(null)}
        onSubmit={submit}
        submitDisabled={memberWritesDisabled}
      >
        {memberWritesDisabled && <p className="form-warn">Membership is managed by your identity provider / IAM.</p>}
        <fieldset className="form-fieldset" disabled={memberWritesDisabled}>
          {editing === "new" ? (
            <>
              <TextField
                label="Email"
                value={form.values.email}
                onChange={(value) => {
                  form.set("email", value);
                  form.set("user_id", "");
                }}
                error={form.errors.email}
                hint={
                  perms.isSuper
                    ? "Enter an existing user's email address, or pick a matching user below."
                    : "Enter the email address of an existing user."
                }
                autoFocus
              />
              {perms.isSuper && (
                <SelectField
                  label="User"
                  value={form.values.user_id}
                  onChange={(value) => {
                    form.set("user_id", value);
                    const user = matchingUsers.find((candidate) => candidate.id === value);
                    if (user) form.set("email", user.email);
                  }}
                  hint="Only active users who are not already on this team are shown."
                  options={[
                    {
                      value: "",
                      label: userDirectory.isPending
                        ? "Loading users…"
                        : matchingUsers.length
                          ? "Select a user (optional)"
                          : "No matching users",
                    },
                    ...matchingUsers.map((user) => ({
                      value: user.id,
                      label: user.email,
                    })),
                  ]}
                />
              )}
            </>
          ) : editing ? (
            <TextField label="Email" value={(editing as Member).email} onChange={() => {}} />
          ) : null}
          <SelectField
            label="Role"
            value={form.values.role}
            onChange={(value) => form.set("role", value as Schemas["TeamRole"])}
            options={[
              { value: "admin", label: "Admin" },
              { value: "member", label: "Member" },
            ]}
          />
          <PolicyFields values={form.values} errors={form.errors} set={form.set} />
        </fieldset>
      </FormDialog>
      <ConfirmDialog
        open={!!deleting}
        title="Remove member"
        confirmLabel="Remove"
        message={`Remove ${deleting?.email} from ${team.name}? Their active keys will be suspended.`}
        onConfirm={() => remove.mutateAsync(deleting!)}
        onClose={() => setDeleting(null)}
      />
    </>
  );
}

type ServiceValues = PolicyValues & { name: string; description: string };
const blankService: ServiceValues = { name: "", description: "", ...blankPolicy() };

function ServiceAccountsPanel({ team }: { team: Team }) {
  const perms = useCan();
  const isAdmin = perms.teamAdmin(team.id);
  const list = useQuery({ queryKey: ["service-accounts", team.id], queryFn: () => api.serviceAccounts(team.id) });
  const [editing, setEditing] = useState<ServiceAccount | "new" | null>(null);
  const [deleting, setDeleting] = useState<ServiceAccount | null>(null);
  const form = useForm<ServiceValues>(blankService, { name: required("Name") });
  const save = useMut(
    ({ account, values }: { account: ServiceAccount | null; values: ServiceValues }) =>
      account
        ? api.updateServiceAccount(account.id, { name: values.name, description: values.description, ...policyBody(values) })
        : api.createServiceAccount(team.id, { name: values.name, description: values.description, ...policyBody(values) }),
    "Service account saved",
  );
  const remove = useMut((account: ServiceAccount) => api.deleteServiceAccount(account.id), "Service account deleted");

  const open = (account: ServiceAccount | "new") => {
    form.reset(
      account === "new"
        ? blankService
        : { name: account.name, description: account.description, ...policyFrom(account) },
    );
    setEditing(account);
  };
  const submit = async () => {
    if (!form.validate()) return false;
    return form.run(() =>
      save.mutateAsync({
        account: editing === "new" ? null : (editing as ServiceAccount),
        values: form.values,
      }),
      POLICY_ALIAS,
    );
  };

  return (
    <>
      <div className="panel-head">
        <h2>Service accounts</h2>
        <CreateButton can={isAdmin} onClick={() => open("new")}>New service account</CreateButton>
      </div>
      <DataTable
        rows={list.data}
        loading={list.isPending}
        error={list.error}
        onRetry={() => void list.refetch()}
        rowKey={(account) => account.id}
        empty="No service accounts yet."
        columns={[
          { key: "name", header: "Name", sort: (a, b) => a.name.localeCompare(b.name), cell: (account) => <strong>{account.name}</strong> },
          { key: "description", header: "Description", cell: (account) => account.description || "—" },
          { key: "policy", header: "Policy", cell: (account) => <PolicySummary p={account} /> },
          { key: "budget", header: "Budget", cell: (account) => <Budget p={account} /> },
          {
            key: "actions",
            header: "",
            cell: (account) => (
              <RowActions
                canEdit={isAdmin}
                canDelete={isAdmin}
                onEdit={() => open(account)}
                onDelete={() => setDeleting(account)}
              />
            ),
          },
        ]}
      />
      <FormDialog
        open={!!editing}
        title={editing === "new" ? "New service account" : "Edit service account"}
        submitLabel={editing === "new" ? "Create" : "Save"}
        onClose={() => setEditing(null)}
        onSubmit={submit}
      >
        <TextField label="Name" value={form.values.name} onChange={(value) => form.set("name", value)} error={form.errors.name} autoFocus />
        <TextField label="Description" value={form.values.description} onChange={(value) => form.set("description", value)} />
        <PolicyFields values={form.values} errors={form.errors} set={form.set} />
      </FormDialog>
      <ConfirmDialog
        open={!!deleting}
        title="Delete service account"
        message={`Delete “${deleting?.name}”? Accounts with keys cannot be deleted.`}
        onConfirm={() => remove.mutateAsync(deleting!)}
        onClose={() => setDeleting(null)}
      />
    </>
  );
}

type KeyValues = { name: string; service_account_id: string; expires: string };
const blankKey: KeyValues = { name: "", service_account_id: "", expires: "" };

function expiresAt(value: string) {
  return value ? new Date(`${value}T23:59:59`).toISOString() : null;
}

function KeysPanel({ team }: { team: Team }) {
  const me = useMe();
  const perms = useCan();
  const isAdmin = perms.teamAdmin(team.id);
  const members = useQuery({ queryKey: ["members", team.id], queryFn: () => api.members(team.id) });
  const accounts = useQuery({ queryKey: ["service-accounts", team.id], queryFn: () => api.serviceAccounts(team.id) });
  const keys = useQuery({ queryKey: ["team-keys", team.id], queryFn: () => api.teamKeys(team.id) });
  const [editing, setEditing] = useState<Key | "new" | null>(null);
  const [revoking, setRevoking] = useState<Key | null>(null);
  const [secret, setSecret] = useState<string | null>(null);
  const [secretTitle, setSecretTitle] = useState("API key created");
  const ownerOptions = [
    ...(perms.canCreateOwnKey(team.id) && !!me.data?.user_id
      ? [{ value: "", label: me.data.email ?? "My account" }]
      : []),
    ...(isAdmin ? (accounts.data ?? []).map((account) => ({ value: account.id, label: account.name })) : []),
  ];
  const form = useForm<KeyValues>(blankKey, { name: required("Name") });
  const create = useMut(
    (values: KeyValues) =>
      api.createKey(team.id, {
        name: values.name,
        expires_at: expiresAt(values.expires),
        service_account_id: values.service_account_id || null,
      }),
    (result) => {
      setSecretTitle("API key created");
      setSecret(result.secret);
    },
  );
  const update = useMut(
    ({ key, values }: { key: Key; values: KeyValues }) => {
      const body: Schemas["KeyUpdate"] = { name: values.name };
      if (values.expires !== (key.expires_at?.slice(0, 10) ?? "")) {
        body.expires_at = expiresAt(values.expires);
      }
      return api.updateKey(key.id, body);
    },
    "Key saved",
  );
  const setLifecycle = useAction(
    ({ key, lifecycle }: { key: Key; lifecycle: "active" | "suspended" }) =>
      api.updateKey(key.id, { lifecycle }),
    "Key updated",
  );
  const revoke = useMut((key: Key) => api.revokeKey(key.id), "Key revoked");
  const rotate = useAction(
    (key: Key) => api.rotateKey(key.id),
    (result) => {
      setSecretTitle("API key rotated");
      setSecret(result.secret);
    },
  );

  const open = (key: Key | "new") => {
    const defaultOwner = ownerOptions[0]?.value ?? "";
    form.reset(
      key === "new"
        ? { ...blankKey, service_account_id: defaultOwner }
        : { name: key.name, service_account_id: key.service_account_id ?? "", expires: key.expires_at?.slice(0, 10) ?? "" },
    );
    setEditing(key);
  };
  const submit = async () => {
    if (!form.validate()) return false;
    return form.run(() =>
      editing === "new"
        ? create.mutateAsync(form.values)
        : update.mutateAsync({ key: editing as Key, values: form.values }),
    );
  };
  const ownerName = (key: Key) => {
    if (key.owner_kind === "service_account") {
      return accounts.data?.find((account) => account.id === key.service_account_id)?.name ?? key.service_account_id?.slice(0, 8) ?? "—";
    }
    return members.data?.find((member) => member.user_id === key.user_id)?.email ??
      (key.user_id === me.data?.user_id ? me.data?.email : undefined) ??
      key.user_id?.slice(0, 8) ??
      "—";
  };
  const canResume = (key: Key) => {
    if (!perms.canResumeKey(key)) return false;
    if (
      key.owner_kind === "user" &&
      (key.suspended_reason === "owner_removed" || key.suspended_reason === "owner_deactivated")
    ) {
      return members.data?.some((member) => member.user_id === key.user_id) ?? false;
    }
    return true;
  };

  return (
    <>
      <div className="panel-head">
        <h2>Keys</h2>
        <CreateButton can={ownerOptions.length > 0} onClick={() => open("new")}>New key</CreateButton>
      </div>
      <DataTable
        rows={keys.data}
        loading={keys.isPending || members.isPending || accounts.isPending}
        error={keys.error ?? members.error ?? accounts.error}
        onRetry={() => void keys.refetch()}
        rowKey={(key) => key.id}
        empty="No keys in this team."
        columns={[
          { key: "name", header: "Name", sort: (a, b) => a.name.localeCompare(b.name), cell: (key) => <><strong>{key.name}</strong> <code className="muted">{key.display_prefix}…</code></> },
          { key: "owner", header: "Owner", cell: (key) => <>{key.owner_kind === "service_account" ? "Service account: " : ""}{ownerName(key)}</> },
          { key: "lifecycle", header: "Lifecycle", cell: (key) => <><KeyBadge k={key} /><SuspendedReason reason={key.suspended_reason} /></> },
          { key: "expires", header: "Expires", cell: (key) => fmtDate(key.expires_at) },
          { key: "created", header: "Created", cell: (key) => fmtDate(key.created_at) },
          {
            key: "actions",
            header: "",
            cell: (key) => {
              const allowed = perms.canManageKey(key);
              const ownerInactive = key.owner_kind === "user" && key.owner_active === false;
              return (
                <RowActions
                  canEdit={allowed && key.lifecycle !== "revoked"}
                  onEdit={() => open(key)}
                  extra={key.lifecycle === "revoked" ? null : (
                    <>
                      <button className="btn btn-sm" disabled={!allowed} onClick={() => void rotate(key)}>Rotate</button>
                      <button
                        className="btn btn-sm"
                        disabled={
                          key.lifecycle === "suspended"
                            ? ownerInactive || !canResume(key)
                            : !allowed
                        }
                        title={key.lifecycle === "suspended" && ownerInactive ? "Cannot resume because the owner is inactive" : undefined}
                        onClick={() => void setLifecycle({ key, lifecycle: key.lifecycle === "suspended" ? "active" : "suspended" })}
                      >
                        {key.lifecycle === "suspended" ? "Resume" : "Suspend"}
                      </button>
                      <button className="btn btn-sm btn-danger-ghost" disabled={!allowed} onClick={() => setRevoking(key)}>Revoke</button>
                    </>
                  )}
                />
              );
            },
          },
        ]}
      />
      <FormDialog
        open={!!editing}
        title={editing === "new" ? "New API key" : "Edit API key"}
        submitLabel={editing === "new" ? "Create key" : "Save"}
        onClose={() => setEditing(null)}
        onSubmit={submit}
      >
        {editing === "new" && ownerOptions.length > 1 && (
          <SelectField
            label="Owner"
            value={form.values.service_account_id}
            onChange={(value) => form.set("service_account_id", value)}
            options={ownerOptions}
          />
        )}
        <TextField label="Name" value={form.values.name} onChange={(value) => form.set("name", value)} error={form.errors.name} autoFocus />
        <TextField label="Expires" type="date" value={form.values.expires} onChange={(value) => form.set("expires", value)} hint="Empty means the key never expires." />
      </FormDialog>
      <Dialog
        open={!!secret}
        title={secretTitle}
        onClose={() => setSecret(null)}
        footer={<button className="btn btn-primary" onClick={() => setSecret(null)}>I’ve saved it</button>}
      >
        {secret && <SecretOnce secret={secret} />}
      </Dialog>
      <ConfirmDialog
        open={!!revoking}
        title="Revoke key"
        confirmLabel="Revoke"
        message={`Revoke “${revoking?.name}”? This cannot be undone.`}
        onConfirm={() => revoke.mutateAsync(revoking!)}
        onClose={() => setRevoking(null)}
      />
    </>
  );
}

function TeamUsage({ team }: { team: Team }) {
  const [groupBy, setGroupBy] = useState("owner");
  const members = useQuery({ queryKey: ["members", team.id], queryFn: () => api.members(team.id) });
  const accounts = useQuery({ queryKey: ["service-accounts", team.id], queryFn: () => api.serviceAccounts(team.id) });
  const usage = useQuery({
    queryKey: ["usage", team.id, groupBy],
    queryFn: () => api.usage({ team_id: team.id, group_by: groupBy }),
  });
  const ownerName = (kind: string | null | undefined, id: string | null | undefined) => {
    if (!id) return "—";
    if (kind === "service_account") return accounts.data?.find((account) => account.id === id)?.name ?? id.slice(0, 8);
    return members.data?.find((member) => member.user_id === id)?.email ?? id.slice(0, 8);
  };

  return (
    <>
      <div className="panel-head">
        <h2>Usage</h2>
        <div className="filters">
          <SelectField
            label="Group by"
            value={groupBy}
            onChange={setGroupBy}
            options={[
              { value: "owner", label: "Owner" },
              { value: "key", label: "Key" },
              { value: "day,requested", label: "Day × requested" },
            ]}
          />
        </div>
      </div>
      <DataTable
        rows={usage.data}
        loading={usage.isPending || members.isPending || accounts.isPending}
        error={usage.error ?? members.error ?? accounts.error}
        onRetry={() => void usage.refetch()}
        rowKey={(row) => [row.date, row.requested, row.owner_kind, row.owner_id, row.key_id].join("|")}
        empty="No usage for this team."
        columns={[
          { key: "date", header: "Date", cell: (row) => row.date ?? "—" },
          { key: "requested", header: "Requested", cell: (row) => row.requested ?? "—" },
          { key: "owner", header: "Owner", cell: (row) => ownerName(row.owner_kind, row.owner_id) },
          { key: "key", header: "Key", cell: (row) => row.key_id?.slice(0, 8) ?? "—" },
          { key: "requests", header: "Requests", align: "right", cell: (row) => row.requests.toLocaleString() },
          { key: "tokens", header: "Tokens", align: "right", cell: (row) => row.total_tokens.toLocaleString() },
          { key: "cost", header: "Cost", align: "right", cell: (row) => fmtUsd(row.cost_nano_usd) },
        ]}
      />
    </>
  );
}

function TeamSettings({ team }: { team: Team }) {
  const perms = useCan();
  const [editing, setEditing] = useState(false);
  const form = useForm<PolicyValues & { name: string }>(
    { name: team.name, ...policyFrom(team) },
    { name: required("Name") },
  );
  const update = useMut(
    (values: PolicyValues & { name: string }) => api.updateTeam(team.id, { name: values.name, ...policyBody(values) }),
    "Team settings saved",
  );

  const edit = () => {
    form.reset({ name: team.name, ...policyFrom(team) });
    setEditing(true);
  };
  const submit = async () => {
    if (!form.validate()) return false;
    return form.run(() => update.mutateAsync(form.values), POLICY_ALIAS);
  };

  return (
    <>
      <div className="panel-head">
        <h2>Settings</h2>
        {perms.teamAdmin(team.id) && <button className="btn" onClick={edit}>Edit team</button>}
      </div>
      {!perms.teamAdmin(team.id) && (
        <p className="muted">Team settings are read-only for members.</p>
      )}
      <section className="card pad kv">
        <dl>
          <dt>Name</dt><dd>{team.name}</dd>
          <dt>Policy</dt><dd><PolicySummary p={team} /></dd>
          <dt>Budget</dt><dd><Budget p={team} /></dd>
          <dt>Created</dt><dd>{fmtDate(team.created_at)}</dd>
        </dl>
      </section>
      <FormDialog open={editing} title="Edit team settings" onClose={() => setEditing(false)} onSubmit={submit}>
        <TextField label="Name" value={form.values.name} onChange={(value) => form.set("name", value)} error={form.errors.name} autoFocus />
        <PolicyFields values={form.values} errors={form.errors} set={form.set} isTeam />
      </FormDialog>
    </>
  );
}
