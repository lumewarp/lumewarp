import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, ApiError, hasPolicy, type Key, type Member, type Schemas, type ServiceAccount, type Team, type TeamAny } from "../api";
import { Badge, fmtDate, fmtUsd, InheritedBadge, KeyBadge, PageHeader, SuspendedReason } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { FormDialog, NumberField, required, SelectField, TextField, useForm } from "../components/Form";
import { blankKeyLimits, KeyLimitFields, keyLimitsBody, keyLimitsFrom, type KeyLimitCap, type KeyLimitValues } from "../components/KeyLimits";
import { blankPolicy, Budget, PolicyFields, policyBody, policyFrom, PolicySummary, POLICY_ALIAS, type PolicyValues } from "../components/Policy";
import { CreateButton, RowActions } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { Card } from "../components/ui/card";
import { ComboboxField } from "../components/ui/combobox-field";
import { DescriptionDetails, DescriptionList, DescriptionTerm } from "../components/ui/description-list";
import { HelpTip } from "../components/ui/help-tip";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../components/ui/tabs";
import { useAction, useCan, useMe, useMut, useOrgLayer, useSecretMut } from "../hooks";
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
  // An inherited admin may still be added as a direct member, so only memberships count.
  const existingUserIds = new Set((list.data ?? []).filter((member) => member.direct).map((member) => member.user_id));
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
          {
            key: "role",
            header: "Role",
            cell: (member) => (
              <span className="inline-flex flex-wrap gap-1">
                <Badge tone="info">{member.role}</Badge>
                <InheritedBadge from={member.inherited_from} />
              </span>
            ),
          },
          { key: "policy", header: "Policy", cell: (member) => <PolicySummary p={member} /> },
          {
            key: "actions",
            header: "",
            cell: (member) => (
              // An inherited admin has no membership here; the role changes where it is granted.
              member.direct ? (
                <RowActions
                  canEdit={isAdmin && !memberWritesDisabled}
                  // Removing an admin is org-level; a team admin removes plain members.
                  canDelete={isAdmin && !memberWritesDisabled && (member.role === "member" || canAssignAdmin)}
                  onEdit={() => open(member)}
                  onDelete={() => setDeleting(member)}
                />
              ) : null
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
                hint="The email must belong to an existing user."
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
          <div className="flex items-center gap-1 text-sm font-medium">
            Member policy
            <HelpTip label="Member policy" text="This exception overrides member defaults but remains capped by the team's effective limits." />
          </div>
          <PolicyFields values={form.values} errors={form.errors} set={form.set} team={team} />
        </fieldset>
      </FormDialog>
      <ConfirmDialog
        open={!!deleting}
        title="Remove member"
        confirmLabel="Remove"
        message={
          deleting?.inherited_from
            ? `Remove ${deleting.email}'s membership of ${team.name}? They stay an admin by inheritance and keep their keys.`
            : `Remove ${deleting?.email} from ${team.name}? Their active keys will be suspended.`
        }
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
  const [toggling, setToggling] = useState<ServiceAccount | null>(null);
  const form = useForm<ServiceValues>(blankService, { name: required("Name") });
  const save = useMut(
    ({ account, values }: { account: ServiceAccount | null; values: ServiceValues }) =>
      account
        ? api.updateServiceAccount(account.id, { name: values.name, description: values.description, ...policyBody(values) })
        : api.createServiceAccount(team.id, { name: values.name, description: values.description, ...policyBody(values) }),
    "Service account saved",
  );
  const remove = useMut((account: ServiceAccount) => api.deleteServiceAccount(account.id), "Service account deleted");
  const setActive = useMut(
    ({ account, active }: { account: ServiceAccount; active: boolean }) =>
      api.updateServiceAccount(account.id, { is_active: active }),
    (account) => (account.is_active ? "Service account reactivated" : "Service account deactivated"),
  );

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
          {
            key: "status",
            header: "Status",
            cell: (account) => (
              <Badge tone={account.is_active ? "ok" : "danger"}>{account.is_active ? "Active" : "Deactivated"}</Badge>
            ),
          },
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
                extra={
                  <Button variant="outline" size="sm" disabled={!isAdmin} onClick={() => setToggling(account)}>
                    {account.is_active ? "Deactivate" : "Reactivate"}
                  </Button>
                }
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
        <div className="flex items-center gap-1 text-sm font-medium">
          Account policy
          <HelpTip label="Account policy" text="These limits cap the account's keys, which stop working while the account is deactivated." />
        </div>
        <PolicyFields values={form.values} errors={form.errors} set={form.set} team={team} />
      </FormDialog>
      <ConfirmDialog
        open={!!deleting}
        title="Delete service account"
        message={`Delete “${deleting?.name}”? Accounts with keys cannot be deleted.`}
        onConfirm={() => remove.mutateAsync(deleting!)}
        onClose={() => setDeleting(null)}
      />
      <ConfirmDialog
        open={!!toggling}
        title={toggling?.is_active ? "Deactivate service account" : "Reactivate service account"}
        confirmLabel={toggling?.is_active ? "Deactivate" : "Reactivate"}
        message={
          toggling?.is_active
            ? `Deactivate “${toggling?.name}”? Its keys stop being served immediately. The account and its keys are kept; reactivating makes the non-revoked keys work again.`
            : `Reactivate “${toggling?.name}”? Its non-revoked keys are served by the gateway again.`
        }
        onConfirm={() => setActive.mutateAsync({ account: toggling!, active: !toggling!.is_active })}
        onClose={() => setToggling(null)}
      />
    </>
  );
}

type KeyValues = KeyLimitValues & { name: string; service_account_id: string; expires: string };
const blankKey: KeyValues = { name: "", service_account_id: "", expires: "", ...blankKeyLimits() };

const minLimit = (own: number | null, parent: number | null | undefined) =>
  own === null ? (parent ?? null) : parent === null || parent === undefined ? own : Math.min(own, parent);

/** A stored policy or member default, as the three limit fields. */
const policyLimits = (p: { rpm_limit: number | null; tpm_limit: number | null; max_parallel: number | null }): KeyLimitValues => ({
  rpm_limit: p.rpm_limit,
  tpm_limit: p.tpm_limit,
  max_parallel: p.max_parallel,
});

const clampLimits = (own: KeyLimitValues, parent: KeyLimitValues): KeyLimitValues => ({
  rpm_limit: minLimit(own.rpm_limit, parent.rpm_limit),
  tpm_limit: minLimit(own.tpm_limit, parent.tpm_limit),
  max_parallel: minLimit(own.max_parallel, parent.max_parallel),
});

function KeysPanel({ team }: { team: TeamAny }) {
  const me = useMe();
  const perms = useCan();
  const toast = useToast();
  const showSecret = useShowSecret();
  const isAdmin = perms.teamAdmin(team.id);
  const full = hasPolicy(team) ? team : null;
  const teamLimits = full ? policyLimits(full) : undefined;
  const myTeam = me.data?.teams.find((row) => row.team_id === team.id);
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
    ...(isAdmin
      ? (accounts.data ?? []).filter((account) => account.is_active).map((account) => ({ value: account.id, label: account.name }))
      : []),
  ];
  const form = useForm<KeyValues>(blankKey, { name: required("Name") });
  const create = useSecretMut(
    (values: KeyValues) =>
      api.createKey(team.id, {
        name: values.name,
        expires_at: expiryFromDay(values.expires),
        service_account_id: values.service_account_id || null,
        ...keyLimitsBody(values),
      }),
    (value) => showSecret({ value, title: "API key created" }),
  );
  const update = useMut(
    ({ key, values }: { key: Key; values: KeyValues }) => {
      const body: Schemas["KeyUpdate"] = { name: values.name, ...keyLimitsBody(values) };
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
        : {
            name: key.name,
            service_account_id: key.service_account_id ?? "",
            expires: localDayOf(key.expires_at),
            ...keyLimitsFrom(key),
          },
    );
    setEditing(key);
  };
  // The owner's effective limits, where the API provides them: the caller's own
  // membership has an exact value from /users/me, and a member or service account's
  // stored policy is clamped by the team's policy (the org may lower it further).
  const userCap = (userId: string | null): KeyLimitCap => {
    if (!userId) return undefined;
    if (userId === me.data?.user_id && myTeam) return myTeam.limits;
    if (!full || !teamLimits) return undefined;
    const member = members.data?.find((row) => row.user_id === userId);
    const defaults: KeyLimitValues = {
      rpm_limit: full.member_rpm_default ?? null,
      tpm_limit: full.member_tpm_default ?? null,
      max_parallel: full.member_max_parallel_default ?? null,
    };
    const own = member ? {
      rpm_limit: member.rpm_limit ?? defaults.rpm_limit,
      tpm_limit: member.tpm_limit ?? defaults.tpm_limit,
      max_parallel: member.max_parallel ?? defaults.max_parallel,
    } : defaults;
    return clampLimits(own, teamLimits);
  };
  const accountCap = (accountId: string | null): KeyLimitCap => {
    const account = accounts.data?.find((row) => row.id === accountId);
    if (!account) return undefined;
    const own = policyLimits(account);
    return teamLimits ? clampLimits(own, teamLimits) : own;
  };
  const capFor = (key: Key | "new"): KeyLimitCap => {
    if (key === "new") return form.values.service_account_id ? accountCap(form.values.service_account_id) : userCap(me.data?.user_id ?? null);
    return key.owner_kind === "service_account" ? accountCap(key.service_account_id) : userCap(key.user_id);
  };
  const exactCap = (key: Key | "new") =>
    key === "new" ? !form.values.service_account_id : key.owner_kind === "user" && key.user_id === me.data?.user_id;
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
        <TextField label="Expires" type="date" value={form.values.expires} max={expiryMax} onChange={(value) => form.set("expires", value)} note={expiryHint} />
        <KeyLimitFields
          values={form.values}
          errors={form.errors}
          set={form.set}
          cap={editing ? capFor(editing) : undefined}
          capNote={editing && exactCap(editing) ? undefined : "the owner's limits, clamped by the team; the org may lower them further"}
        />
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
  member_rpm_default: number | null;
  member_tpm_default: number | null;
  member_max_parallel_default: number | null;
};

const teamSettingsFrom = (team: TeamAny, withPolicy: boolean): TeamSettingsValues => {
  const full = hasPolicy(team) ? team : null;
  return {
    name: team.name,
    ...(withPolicy && full ? policyFrom(full, true) : blankPolicy(true)),
    max_keys_per_member: team.max_keys_per_member ?? null,
    max_member_key_lifetime_seconds: team.max_member_key_lifetime_seconds ?? null,
    member_rpm_default: full?.member_rpm_default ?? null,
    member_tpm_default: full?.member_tpm_default ?? null,
    member_max_parallel_default: full?.member_max_parallel_default ?? null,
  };
};

const memberDefaultsText = (team: Team) => {
  const parts = [
    team.member_rpm_default != null ? `${team.member_rpm_default.toLocaleString()} requests/min` : null,
    team.member_tpm_default != null ? `${team.member_tpm_default.toLocaleString()} tokens/min` : null,
    team.member_max_parallel_default != null ? `${team.member_max_parallel_default.toLocaleString()} in parallel` : null,
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : "No default limits";
};

function TeamSettings({ team }: { team: TeamAny }) {
  const perms = useCan();
  const navigate = useNavigate();
  const { orgs, show } = useOrgLayer();
  const canEdit = perms.canUpdateTeamSettings(team.id);
  const canSetQuota = perms.canSetTeamQuota(team.id);
  const canDelete = perms.canDeleteTeam(team.id);
  const [editing, setEditing] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const orgName = orgs.data?.find((org) => org.id === team.org_id)?.name;
  const form = useForm<TeamSettingsValues>(teamSettingsFrom(team, canSetQuota), {
    name: required("Name"),
  });
  const update = useMut(
    (values: TeamSettingsValues) => {
      const body: Schemas["TeamUpdate"] = {
        name: values.name,
        max_keys_per_member: values.max_keys_per_member,
        max_member_key_lifetime_seconds: values.max_member_key_lifetime_seconds,
        member_rpm_default: values.member_rpm_default,
        member_tpm_default: values.member_tpm_default,
        member_max_parallel_default: values.member_max_parallel_default,
      };
      // A team admin edits the name, the member key limits and the member defaults; the
      // team quota (models, rate limits, budget) is sent only by an org admin, so a stale
      // read can never look like a quota change.
      if (canSetQuota) Object.assign(body, policyBody(values));
      return api.updateTeam(team.id, body);
    },
    "Team settings saved",
  );
  const archive = useMut((id: string) => api.deleteTeam(id), () => {
    void navigate({ to: "/teams" });
  });

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
        <div className="flex flex-wrap gap-2">
          {canEdit && <Button variant="outline" onClick={edit}>Edit team</Button>}
          {canDelete && (
            <Button
              variant="outline"
              className="text-destructive hover:bg-destructive/10 hover:text-destructive"
              onClick={() => setArchiving(true)}
            >
              Archive team
            </Button>
          )}
        </div>
      </div>
      {!canEdit && (
        <p className="text-muted-foreground">Team settings are read-only for members.</p>
      )}
      {canEdit && !canSetQuota && (
        <p className="mb-3 text-muted-foreground">
          Model access, the team's own rate limits and its budget are set by an org admin. You can change the name,
          the member key limits and the member defaults.
        </p>
      )}
      <Card className="mb-4 gap-3 p-4 shadow-none">
        <DescriptionList>
          <DescriptionTerm>Name</DescriptionTerm>
          <DescriptionDetails>{team.name}</DescriptionDetails>
          {show && orgName && (
            <>
              <DescriptionTerm>Org</DescriptionTerm>
              <DescriptionDetails>
                <Link to="/orgs/$orgId" params={{ orgId: team.org_id }}>{orgName}</Link>
              </DescriptionDetails>
            </>
          )}
          {hasPolicy(team) && (
            <>
              <DescriptionTerm>Policy</DescriptionTerm>
              <DescriptionDetails><PolicySummary p={team} /></DescriptionDetails>
              <DescriptionTerm>Budget</DescriptionTerm>
              <DescriptionDetails><Budget p={team} /></DescriptionDetails>
            </>
          )}
          {hasPolicy(team) && (
            <>
              <DescriptionTerm>Member default limits</DescriptionTerm>
              <DescriptionDetails>{memberDefaultsText(team)}</DescriptionDetails>
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
        {canSetQuota && (
          <PolicyFields values={form.values} errors={form.errors} set={form.set} isTeam parent={orgs.data?.find((org) => org.id === team.org_id)} />
        )}
        <NumberField
          label="Member default requests per minute"
          min={1}
          value={form.values.member_rpm_default}
          onChange={(value) => form.set("member_rpm_default", value)}
          error={form.errors.member_rpm_default}
          hint="Member exceptions override this default, which remains capped by the team and is shared across gateways that use the same Redis namespace, otherwise counted per gateway instance."
        />
        <NumberField
          label="Member default tokens per minute"
          min={1}
          value={form.values.member_tpm_default}
          onChange={(value) => form.set("member_tpm_default", value)}
          error={form.errors.member_tpm_default}
          hint="A changed value may not exceed the team's effective value and is shared across gateways that use the same Redis namespace, otherwise counted per gateway instance."
        />
        <NumberField
          label="Member default max parallel requests"
          min={1}
          value={form.values.member_max_parallel_default}
          onChange={(value) => form.set("member_max_parallel_default", value)}
          error={form.errors.member_max_parallel_default}
          hint="Blank removes the member default; the team's limit still applies, shared across gateways that use the same Redis namespace or else per gateway instance."
        />
        <NumberField
          label="Max keys per member"
          min={1}
          value={form.values.max_keys_per_member}
          onChange={(value) => form.set("max_keys_per_member", value)}
          error={form.errors.max_keys_per_member}
          hint="Blank means no limit; revoking a key frees a slot and the cap is checked only on creation."
        />
        <NumberField
          label="Longest member key lifetime (seconds)"
          min={1}
          value={form.values.max_member_key_lifetime_seconds}
          onChange={(value) => form.set("max_member_key_lifetime_seconds", value)}
          error={form.errors.max_member_key_lifetime_seconds}
          hint="Blank means no limit; member keys must expire within this many seconds."
          note={form.values.max_member_key_lifetime_seconds ? fmtLifetime(form.values.max_member_key_lifetime_seconds) : undefined}
        />
      </FormDialog>
      <ConfirmDialog
        open={archiving}
        title="Archive team"
        confirmLabel="Archive"
        message={`Archive “${team.name}”? This cannot be undone. Its keys are revoked, its service accounts deactivated, and the name becomes free again.`}
        onConfirm={() => archive.mutateAsync(team.id)}
        onClose={() => setArchiving(false)}
      />
    </>
  );
}
