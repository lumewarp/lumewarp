import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Membership, type User } from "../api";
import { Badge, PageHeader } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { Field, FormDialog, required, SelectField, TextField, useForm } from "../components/Form";
import { CreateButton, RowActions } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { useCan, useMut, useNames, useTree } from "../hooks";

const ORG_ROLES = ["owner", "admin"];
const TEAM_ROLES = ["admin", "member", "viewer"];

export function Access() {
  return (
    <>
      <PageHeader title="Users & access" description="People and the roles they hold on organizations and teams." />
      <Users />
      <h2 className="section-gap">Memberships</h2>
      <Memberships />
    </>
  );
}

interface UserValues {
  email: string;
  display_name: string;
  is_superuser: boolean;
  is_active: boolean;
}
const blankUser: UserValues = { email: "", display_name: "", is_superuser: false, is_active: true };

function Check({ label, checked, onChange }: { label: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <Field label={label}>
      {(p) => <input {...p} type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />}
    </Field>
  );
}

function Users() {
  const perms = useCan();
  const list = useQuery({ queryKey: ["users"], queryFn: () => api.users() });
  const [editing, setEditing] = useState<User | "new" | null>(null);
  const [deleting, setDeleting] = useState<User | null>(null);
  const form = useForm<UserValues>(blankUser, { email: (v) => (/^\S+@\S+$/.test(v) ? undefined : "Enter a valid email") });
  const create = useMut((v: UserValues) => api.createUser(v), "User created");
  const update = useMut(({ id, v }: { id: string; v: UserValues }) => api.updateUser(id, v), "User saved");
  const remove = useMut((id: string) => api.deleteUser(id), "User deleted");
  const open = (u: User | "new") => {
    form.reset(u === "new" ? blankUser : { email: u.email, display_name: u.display_name, is_superuser: u.is_superuser, is_active: u.is_active });
    setEditing(u);
  };
  const submit = async () => {
    if (!form.validate()) return false;
    return form.run(() => (editing === "new" ? create.mutateAsync(form.values) : update.mutateAsync({ id: (editing as User).id, v: form.values })));
  };
  return (
    <>
      <div className="page-actions toolbar">
        <CreateButton can={perms.isSuper} onClick={() => open("new")}>New user</CreateButton>
      </div>
      <DataTable
        rows={list.data}
        loading={list.isPending}
        error={list.error}
        onRetry={() => void list.refetch()}
        rowKey={(r) => r.id}
        empty="No users yet."
        columns={[
          { key: "email", header: "Email", sort: (a, b) => a.email.localeCompare(b.email), cell: (r) => <strong>{r.email}</strong> },
          { key: "name", header: "Name", cell: (r) => r.display_name },
          { key: "status", header: "Status", cell: (r) => (<>{r.is_active ? <Badge tone="ok">Active</Badge> : <Badge>Disabled</Badge>}{r.is_superuser && <Badge tone="info">Superuser</Badge>}</>) },
          { key: "actions", header: "", cell: (r) => <RowActions canEdit={perms.isSuper} canDelete={perms.isSuper} onEdit={() => open(r)} onDelete={() => setDeleting(r)} /> },
        ]}
      />
      <FormDialog open={!!editing} title={editing === "new" ? "New user" : "Edit user"} submitLabel={editing === "new" ? "Create" : "Save"} onClose={() => setEditing(null)} onSubmit={submit}>
        <TextField label="Email" value={form.values.email} onChange={(v) => form.set("email", v)} error={form.errors.email} autoFocus />
        <TextField label="Display name" value={form.values.display_name} onChange={(v) => form.set("display_name", v)} />
        <Check label="Active" checked={form.values.is_active} onChange={(v) => form.set("is_active", v)} />
        <Check label="Superuser" checked={form.values.is_superuser} onChange={(v) => form.set("is_superuser", v)} />
      </FormDialog>
      <ConfirmDialog open={!!deleting} title="Delete user" message={`Delete ${deleting?.email}? Their tokens and memberships go with them.`} onConfirm={() => remove.mutateAsync(deleting!.id)} onClose={() => setDeleting(null)} />
    </>
  );
}

interface MemberValues {
  user_id: string;
  org_id: string;
  team_id: string;
  role: string;
}

function Memberships() {
  const perms = useCan();
  const tree = useTree();
  const users = useQuery({ queryKey: ["users"], queryFn: () => api.users() });
  const list = useQuery({ queryKey: ["memberships"], queryFn: () => api.memberships() });
  const userName = useNames((users.data ?? []).map((u) => ({ id: u.id, name: u.email })));
  const orgName = useNames(tree.data?.orgs);
  const teamName = useNames(tree.data?.teams);
  const [editing, setEditing] = useState<Membership | "new" | null>(null);
  const [deleting, setDeleting] = useState<Membership | null>(null);
  const form = useForm<MemberValues>({ user_id: "", org_id: "", team_id: "", role: "admin" }, { user_id: required("User"), org_id: required("Organization") });
  const create = useMut((v: MemberValues) => api.createMembership({ user_id: v.user_id, org_id: v.org_id, team_id: v.team_id || null, role: v.role }), "Membership created");
  const update = useMut(({ id, role }: { id: string; role: string }) => api.updateMembership(id, role), "Role changed");
  const remove = useMut((id: string) => api.deleteMembership(id), "Membership removed");

  const orgs = (tree.data?.orgs ?? []).filter((o) => perms.orgAdmin(o.id));
  const teamsOfOrg = (tree.data?.teams ?? []).filter((t) => t.org_id === form.values.org_id);
  const roles = form.values.team_id ? TEAM_ROLES : ORG_ROLES;

  const open = (m: Membership | "new") => {
    form.reset(m === "new" ? { user_id: users.data?.[0]?.id ?? "", org_id: orgs[0]?.id ?? "", team_id: "", role: "admin" } : { user_id: m.user_id, org_id: m.org_id, team_id: m.team_id ?? "", role: m.role });
    setEditing(m);
  };
  const submit = async () => {
    if (!form.validate()) return false;
    return form.run(() => (editing === "new" ? create.mutateAsync(form.values) : update.mutateAsync({ id: (editing as Membership).id, role: form.values.role })));
  };

  return (
    <>
      <div className="page-actions toolbar">
        <CreateButton can={orgs.length > 0} onClick={() => open("new")}>Add membership</CreateButton>
      </div>
      <DataTable
        rows={list.data}
        loading={list.isPending}
        error={list.error}
        onRetry={() => void list.refetch()}
        rowKey={(r) => r.id}
        empty="No memberships yet."
        columns={[
          { key: "user", header: "User", sort: (a, b) => userName(a.user_id).localeCompare(userName(b.user_id)), cell: (r) => userName(r.user_id) },
          { key: "org", header: "Organization", cell: (r) => orgName(r.org_id) },
          { key: "team", header: "Team", cell: (r) => (r.team_id ? teamName(r.team_id) : <span className="muted">whole org</span>) },
          { key: "role", header: "Role", cell: (r) => <Badge tone="info">{r.role}</Badge> },
          { key: "actions", header: "", cell: (r) => <RowActions canEdit={perms.orgAdmin(r.org_id)} canDelete={perms.orgAdmin(r.org_id)} onEdit={() => open(r)} onDelete={() => setDeleting(r)} /> },
        ]}
      />
      <FormDialog open={!!editing} title={editing === "new" ? "Add membership" : "Change role"} submitLabel={editing === "new" ? "Add" : "Save"} onClose={() => setEditing(null)} onSubmit={submit}>
        {editing === "new" && (
          <>
            <SelectField label="User" value={form.values.user_id} onChange={(v) => form.set("user_id", v)} error={form.errors.user_id} options={(users.data ?? []).map((u) => ({ value: u.id, label: u.email }))} />
            <SelectField label="Organization" value={form.values.org_id} onChange={(v) => { form.set("org_id", v); form.set("team_id", ""); form.set("role", "admin"); }} error={form.errors.org_id} options={orgs.map((o) => ({ value: o.id, label: o.name }))} />
            <SelectField label="Team" value={form.values.team_id} onChange={(v) => { form.set("team_id", v); form.set("role", v ? "member" : "admin"); }} options={[{ value: "", label: "Whole organization" }, ...teamsOfOrg.map((t) => ({ value: t.id, label: t.name }))]} />
          </>
        )}
        <SelectField label="Role" value={form.values.role} onChange={(v) => form.set("role", v)} error={form.errors.role} options={(editing !== "new" && editing?.team_id ? TEAM_ROLES : editing === "new" ? roles : ORG_ROLES).map((r) => ({ value: r, label: r }))} />
      </FormDialog>
      <ConfirmDialog open={!!deleting} title="Remove membership" confirmLabel="Remove" message="Remove this membership? The user loses the access it grants." onConfirm={() => remove.mutateAsync(deleting!.id)} onClose={() => setDeleting(null)} />
    </>
  );
}
