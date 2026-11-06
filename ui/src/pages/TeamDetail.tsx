import { Link, useParams } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, ApiError, hasPolicy, type Key, type Member, type Schemas, type ServiceAccount, type Team, type TeamAny } from "../api";
import { Badge, fmtDate, fmtUsd, KeyBadge, PageHeader, SuspendedReason } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { FormDialog, NumberField, required, SelectField, TextField, useForm } from "../components/Form";
import { blankPolicy, Budget, PolicyFields, policyBody, policyFrom, PolicySummary, POLICY_ALIAS, type PolicyValues } from "../components/Policy";
import { CreateButton, RowActions } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { Card } from "../components/ui/card";
import { ComboboxField } from "../components/ui/combobox-field";
import { DescriptionDetails, DescriptionList, DescriptionTerm } from "../components/ui/description-list";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../components/ui/tabs";
import { useAction, useCan, useMe, useMut, useSecretMut } from "../hooks";
import { useShowSecret } from "../components/SecretProvider";
import { useToast } from "../components/Toast";
import { expiryFromDay, fmtLifetime, lastExpiryDay, localDayOf } from "./keyText";

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
  const perms = useCan();
  const canListTeams = perms.isInstallationAdmin || perms.isTeamAdmin;
  const [tab, setTab] = useState<Tab | null>(null);
  const [externallyManagedTeamId, setExternallyManagedTeamId] = useState<string | null>(null);
  const team = useQuery({ queryKey: ["team", teamId], queryFn: () => api.team(teamId), enabled: !!teamId });

  if (team.isPending) return <div className="p-8 text-center text-muted-foreground" aria-busy="true">Loading team…</div>;
  if (team.error || !team.data) {
    return (
      <Alert variant="destructive" data-testid="state-error">
        <AlertDescription>{team.error?.message ?? "Team not found."}</AlertDescription>
      </Alert>
    );
  }

  const data = team.data;
  // The server sends the team policy only to a team admin (or higher); members and
  // service-account lists are the same view, so a plain member's tabs never request them.
  const full = hasPolicy(data) ? data : null;
  const tabs = TABS.filter((item) =>
    item.id === "members" || item.id === "service-accounts" ? full !== null : true,
  );
  const selected = tab !== null && tabs.some((item) => item.id === tab) ? tab : tabs[0].id;

  return (
    <>
      <PageHeader
        title={data.name}
        description="Manage the people, service accounts, keys, and usage for this team."
        actions={
          <Button variant="outline" asChild>
            {canListTeams ? <Link to="/teams">All teams</Link> : <Link to="/my-keys">My keys</Link>}
          </Button>
        }
      />
      <Tabs value={selected} onValueChange={(value) => setTab(value as Tab)} activationMode="manual">
        <TabsList aria-label="Team sections" className="mb-4 max-w-full justify-start overflow-x-auto">
          {tabs.map((item) => (
            <TabsTrigger key={item.id} value={item.id} className="flex-none">
              {item.label}
            </TabsTrigger>
          ))}
        </TabsList>
        <TabsContent value="members">
          {full && (
            <MembersPanel
              team={full}
              managedExternally={externallyManagedTeamId === teamId}
              onManagedExternally={() => setExternallyManagedTeamId(teamId)}
            />
          )}
        </TabsContent>
        <TabsContent value="service-accounts">{full && <ServiceAccountsPanel team={full} />}</TabsContent>
        <TabsContent value="keys">
          <KeysPanel team={data} />
        </TabsContent>
        <TabsContent value="usage">
          <TeamUsage team={data} />
        </TabsContent>
        <TabsContent value="settings">
          <TeamSettings team={data} />
        </TabsContent>
      </Tabs>
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
    enabled: perms.isInstallationAdmin,
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

  // Only an installation admin may hand out or change a team admin role; a team admin
  // edits limits and leaves the existing role alone.
  const canAssignAdmin = perms.canAssignTeamAdmin(team.id);
  const save = useMut(async ({ member, values }: { member: Member | null; values: MemberValues }) => {
    try {
      if (member) {
        return await api.updateMember(team.id, member.user_id, {
          ...(canAssignAdmin ? { role: values.role } : {}),
          ...policyBody(values),
        });
      }
      return await api.createMember(team.id, {
        ...(values.user_id ? { user_id: values.user_id.trim() } : { email: values.email.trim() }),
        role: canAssignAdmin ? values.role : "member",
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
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <h2 className="m-0">Members</h2>
        <CreateButton can={isAdmin && !memberWritesDisabled} onClick={() => open("new")}>Add member</CreateButton>
      </div>
      {memberWritesDisabled && (
        <Alert variant="warn" role="status">
          <AlertDescription>Membership is managed by your identity provider / IAM.</AlertDescription>
        </Alert>
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
                // Removing an admin is installation-level; a team admin removes plain members.
                canDelete={isAdmin && !memberWritesDisabled && (member.role === "member" || canAssignAdmin)}
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
        {memberWritesDisabled && (
          <Alert variant="warn" role="status">
            <AlertDescription>Membership is managed by your identity provider / IAM.</AlertDescription>
          </Alert>
        )}
        <fieldset className="m-0 flex min-w-0 flex-col gap-3.5 border-0 p-0" disabled={memberWritesDisabled}>
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
                  perms.isInstallationAdmin
                    ? "Enter an existing user's email address, or pick a matching user below."
                    : "Enter the email address of an existing user."
                }
                autoFocus
              />
              {perms.isInstallationAdmin && (
                <ComboboxField
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
          {canAssignAdmin ? (
            <SelectField
              label="Role"
              value={form.values.role}
              onChange={(value) => form.set("role", value as Schemas["TeamRole"])}
              options={[
                { value: "admin", label: "Admin" },
                { value: "member", label: "Member" },
              ]}
            />
          ) : editing === "new" ? (
            <p className="m-0 text-sm text-muted-foreground">
              New members join as members; only an org admin can assign a team admin.
            </p>
          ) : null}
          <PolicyFields values={form.values} errors={form.errors} set={form.set} team={team} />
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
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <h2 className="m-0">Service accounts</h2>
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
        <PolicyFields values={form.values} errors={form.errors} set={form.set} team={team} />
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

function KeysPanel({ team }: { team: TeamAny }) {
  const me = useMe();
  const perms = useCan();
  const toast = useToast();
  const showSecret = useShowSecret();
  const isAdmin = perms.teamAdmin(team.id);
  // The member and service-account lists are the team-admin view; a plain member's own
  // keys need neither (the server forces the list to their own keys).
  const members = useQuery({ queryKey: ["members", team.id], queryFn: () => api.members(team.id), enabled: isAdmin });
  const accounts = useQuery({ queryKey: ["service-accounts", team.id], queryFn: () => api.serviceAccounts(team.id), enabled: isAdmin });
  const keys = useQuery({ queryKey: ["team-keys", team.id], queryFn: () => api.teamKeys(team.id) });
  const [editing, setEditing] = useState<Key | "new" | null>(null);
  const [revoking, setRevoking] = useState<Key | null>(null);
  const ownerOptions = [
    ...(perms.canCreateOwnKey(team.id) && !!me.data?.user_id
      ? [{ value: "", label: me.data.email ?? "My account" }]
      : []),
    ...(isAdmin ? (accounts.data ?? []).map((account) => ({ value: account.id, label: account.name })) : []),
  ];
  const form = useForm<KeyValues>(blankKey, { name: required("Name") });
  const create = useSecretMut(
    (values: KeyValues) =>
      api.createKey(team.id, {
        name: values.name,
        expires_at: expiryFromDay(values.expires),
        service_account_id: values.service_account_id || null,
      }),
    (value) => showSecret({ value, title: "API key created" }),
  );
  const update = useMut(
    ({ key, values }: { key: Key; values: KeyValues }) => {
      const body: Schemas["KeyUpdate"] = { name: values.name };
      if (values.expires !== localDayOf(key.expires_at)) {
        body.expires_at = expiryFromDay(values.expires);
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
  const rotate = useSecretMut(
    (key: Key) => api.rotateKey(key.id),
    (value) => showSecret({ value, title: "API key rotated" }),
  );
  const rotateKey = (key: Key) => void rotate.mutateAsync(key).catch((e: Error) => toast("error", e.message));

  const open = (key: Key | "new") => {
    const defaultOwner = ownerOptions[0]?.value ?? "";
    form.reset(
      key === "new"
        ? { ...blankKey, service_account_id: defaultOwner }
        : { name: key.name, service_account_id: key.service_account_id ?? "", expires: localDayOf(key.expires_at) },
    );
    setEditing(key);
  };
  const memberKey = editing === "new" ? !form.values.service_account_id : editing?.owner_kind === "user";
  const lifetime = memberKey ? (team.max_member_key_lifetime_seconds ?? null) : null;
  const expiryMax = lifetime === null ? undefined : lastExpiryDay(lifetime);
  const expiryHint =
    lifetime === null
      ? "Empty means the key never expires."
      : editing === "new"
        ? `Optional. This team limits member keys to ${fmtLifetime(lifetime)}; empty uses that limit. The latest day is ${expiryMax}.`
        : `Required. This team limits member keys to ${fmtLifetime(lifetime)}; the latest day is ${expiryMax}.`;
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
    if (key.user_id === me.data?.user_id) return true;
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
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <h2 className="m-0">Keys</h2>
        <CreateButton can={ownerOptions.length > 0} onClick={() => open("new")}>New key</CreateButton>
      </div>
      <DataTable
        rows={keys.data}
        loading={keys.isPending || (isAdmin && (members.isPending || accounts.isPending))}
        error={keys.error ?? (isAdmin ? (members.error ?? accounts.error) : null)}
        onRetry={() => void keys.refetch()}
        rowKey={(key) => key.id}
        empty="No keys in this team."
        columns={[
          { key: "name", header: "Name", sort: (a, b) => a.name.localeCompare(b.name), cell: (key) => <><strong>{key.name}</strong> <code className="text-muted-foreground">{key.display_prefix}…</code></> },
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
                      <Button variant="outline" size="sm" disabled={!allowed} onClick={() => rotateKey(key)}>Rotate</Button>
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={
                          key.lifecycle === "suspended"
                            ? ownerInactive || !canResume(key)
                            : !allowed
                        }
                        title={key.lifecycle === "suspended" && ownerInactive ? "Cannot resume because the owner is inactive" : undefined}
                        onClick={() => void setLifecycle({ key, lifecycle: key.lifecycle === "suspended" ? "active" : "suspended" })}
                      >
                        {key.lifecycle === "suspended" ? "Resume" : "Suspend"}
                      </Button>
                      <Button variant="ghost" size="sm" className="text-destructive hover:bg-destructive/10 hover:text-destructive" disabled={!allowed} onClick={() => setRevoking(key)}>Revoke</Button>
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
          <ComboboxField
            label="Owner"
            value={form.values.service_account_id}
            onChange={(value) => form.set("service_account_id", value)}
            options={ownerOptions}
          />
        )}
        <TextField label="Name" value={form.values.name} onChange={(value) => form.set("name", value)} error={form.errors.name} autoFocus />
        <TextField label="Expires" type="date" value={form.values.expires} max={expiryMax} onChange={(value) => form.set("expires", value)} hint={expiryHint} />
      </FormDialog>
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

function TeamUsage({ team }: { team: TeamAny }) {
  const perms = useCan();
  const me = useMe();
  const isAdmin = perms.teamAdmin(team.id);
  const [groupBy, setGroupBy] = useState("owner");
  const members = useQuery({ queryKey: ["members", team.id], queryFn: () => api.members(team.id), enabled: isAdmin });
  const accounts = useQuery({ queryKey: ["service-accounts", team.id], queryFn: () => api.serviceAccounts(team.id), enabled: isAdmin });
  const usage = useQuery({
    queryKey: ["usage", team.id, groupBy],
    queryFn: () => api.usage({ team_id: team.id, group_by: groupBy }),
  });
  const ownerName = (kind: string | null | undefined, id: string | null | undefined) => {
    if (!id) return "—";
    if (kind === "service_account") return accounts.data?.find((account) => account.id === id)?.name ?? id.slice(0, 8);
    return members.data?.find((member) => member.user_id === id)?.email ?? (id === me.data?.user_id ? me.data?.email : undefined) ?? id.slice(0, 8);
  };

  return (
    <>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <h2 className="m-0">Usage</h2>
        <div className="grid grid-cols-[repeat(auto-fit,minmax(180px,1fr))] gap-3">
          <SelectField
            label="Group by"
            value={groupBy}
            onChange={setGroupBy}
            options={[
              { value: "owner", label: "Owner" },
              { value: "key", label: "Key" },
              { value: "day,requested", label: "Day × requested name" },
            ]}
          />
        </div>
      </div>
      <DataTable
        rows={usage.data}
        loading={usage.isPending || (isAdmin && (members.isPending || accounts.isPending))}
        error={usage.error ?? (isAdmin ? (members.error ?? accounts.error) : null)}
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

type TeamSettingsValues = PolicyValues & {
  name: string;
  max_keys_per_member: number | null;
  max_member_key_lifetime_seconds: number | null;
};

const teamSettingsFrom = (team: TeamAny, withPolicy: boolean): TeamSettingsValues => ({
  name: team.name,
  ...(withPolicy && hasPolicy(team) ? policyFrom(team, true) : blankPolicy(true)),
  max_keys_per_member: team.max_keys_per_member ?? null,
  max_member_key_lifetime_seconds: team.max_member_key_lifetime_seconds ?? null,
});

function TeamSettings({ team }: { team: TeamAny }) {
  const perms = useCan();
  const canEdit = perms.canUpdateTeamSettings(team.id);
  const canSetQuota = perms.canSetTeamQuota(team.id);
  const [editing, setEditing] = useState(false);
  const form = useForm<TeamSettingsValues>(teamSettingsFrom(team, canSetQuota), {
    name: required("Name"),
  });
  const update = useMut(
    (values: TeamSettingsValues) => {
      const body: Schemas["TeamUpdate"] = {
        name: values.name,
        max_keys_per_member: values.max_keys_per_member,
        max_member_key_lifetime_seconds: values.max_member_key_lifetime_seconds,
      };
      // A team admin edits the name and the member key limits; the team quota
      // (models, rate limits, budget) is sent only by an installation admin, so a stale
      // read can never look like a quota change.
      if (canSetQuota) Object.assign(body, policyBody(values));
      return api.updateTeam(team.id, body);
    },
    "Team settings saved",
  );

  const edit = () => {
    form.reset(teamSettingsFrom(team, canSetQuota));
    setEditing(true);
  };
  const submit = async () => {
    if (!form.validate()) return false;
    return form.run(() => update.mutateAsync(form.values), POLICY_ALIAS);
  };

  return (
    <>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <h2 className="m-0">Settings</h2>
        {canEdit && <Button variant="outline" onClick={edit}>Edit team</Button>}
      </div>
      {!canEdit && (
        <p className="text-muted-foreground">Team settings are read-only for members.</p>
      )}
      {canEdit && !canSetQuota && (
        <p className="mb-3 text-muted-foreground">
          Model access, rate limits and the budget are set by an installation admin. You can change the name and the
          member key limits.
        </p>
      )}
      <Card className="mb-4 gap-3 p-4 shadow-none">
        <DescriptionList>
          <DescriptionTerm>Name</DescriptionTerm>
          <DescriptionDetails>{team.name}</DescriptionDetails>
          {hasPolicy(team) && (
            <>
              <DescriptionTerm>Policy</DescriptionTerm>
              <DescriptionDetails><PolicySummary p={team} /></DescriptionDetails>
              <DescriptionTerm>Budget</DescriptionTerm>
              <DescriptionDetails><Budget p={team} /></DescriptionDetails>
            </>
          )}
          <DescriptionTerm>Keys per member</DescriptionTerm>
          <DescriptionDetails>{team.max_keys_per_member ?? "No limit"}</DescriptionDetails>
          <DescriptionTerm>Longest member key lifetime</DescriptionTerm>
          <DescriptionDetails>{fmtLifetime(team.max_member_key_lifetime_seconds)}</DescriptionDetails>
          <DescriptionTerm>Created</DescriptionTerm>
          <DescriptionDetails>{fmtDate(team.created_at)}</DescriptionDetails>
        </DescriptionList>
      </Card>
      <FormDialog open={editing} title="Edit team settings" onClose={() => setEditing(false)} onSubmit={submit}>
        <TextField label="Name" value={form.values.name} onChange={(value) => form.set("name", value)} error={form.errors.name} autoFocus />
        {canSetQuota && <PolicyFields values={form.values} errors={form.errors} set={form.set} isTeam />}
        <NumberField
          label="Max keys per member"
          min={1}
          value={form.values.max_keys_per_member}
          onChange={(value) => form.set("max_keys_per_member", value)}
          error={form.errors.max_keys_per_member}
          hint="Blank means no limit. Revoking a key frees a slot; the cap is checked when a key is created."
        />
        <NumberField
          label="Longest member key lifetime (seconds)"
          min={1}
          value={form.values.max_member_key_lifetime_seconds}
          onChange={(value) => form.set("max_member_key_lifetime_seconds", value)}
          error={form.errors.max_member_key_lifetime_seconds}
          hint={
            form.values.max_member_key_lifetime_seconds
              ? `${fmtLifetime(form.values.max_member_key_lifetime_seconds)}. Blank means no limit.`
              : "Blank means no limit. Member keys must expire within this many seconds."
          }
        />
      </FormDialog>
    </>
  );
}
