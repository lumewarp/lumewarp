import { CrownIcon, ShieldMinusIcon, ShieldPlusIcon, Trash2Icon, UserCheckIcon, UserXIcon } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type InstallationAdmin, type Schemas, type User } from "../api";
import { Badge, PageHeader } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { FormDialog, required, TextField, useForm } from "../components/Form";
import { Alert, AlertDescription } from "../components/ui/alert";
import { CreateButton, IconAction } from "../components/RowActions";
import { DataTable, useCursorPaging } from "../components/Table";
import { useMe, useMut } from "../hooks";

type Values = Schemas["UserCreate"];
type ConfirmationKind = "activate" | "deactivate" | "delete" | "make-admin" | "remove-admin" | "transfer";

const PAGE_SIZE = 50;
const blank: Values = { email: "", display_name: "", is_active: true };

export function Users() {
  const me = useMe();
  const { cursor, page, paging } = useCursorPaging("");
  const [creating, setCreating] = useState(false);
  const [confirming, setConfirming] = useState<{ user: User; kind: ConfirmationKind } | null>(null);
  const users = useQuery({
    queryKey: ["users", cursor],
    queryFn: () => api.usersPage(cursor),
    enabled: !!me.data?.is_admin,
  });
  // Installation admins are few: one list, joined to the user rows by id.
  const admins = useQuery({
    queryKey: ["installation-admins"],
    queryFn: () => api.installationAdmins(),
    enabled: !!me.data?.is_admin,
  });
  const roleOf = new Map<string, InstallationAdmin>((admins.data ?? []).map((row) => [row.user_id, row]));
  const canTransfer = me.data?.installation_role === "proxy_admin" || me.data?.kind === "break_glass";
  const form = useForm<Values>(blank, { email: required("Email") });
  const create = useMut((values: Values) => api.createUser(values), "User created");
  const update = useMut(
    ({ user, changes }: { user: User; changes: Schemas["UserUpdate"] }) =>
      api.updateUser(user.id, changes),
    "User updated",
  );
  const remove = useMut((user: User) => api.deleteUser(user.id), "User deleted");
  const makeAdmin = useMut((user: User) => api.assignInstallationAdmin(user.id), "Installation admin added");
  const removeAdmin = useMut((user: User) => api.removeInstallationAdmin(user.id), "Installation admin removed");
  const transfer = useMut((user: User) => api.transferProxyAdmin(user.id), "Proxy admin transferred");

  if (me.isPending) return <div className="p-8 text-center text-muted-foreground" aria-busy="true">Loading user access…</div>;
  if (me.error) return <Alert variant="destructive"><AlertDescription>{me.error.message}</AlertDescription></Alert>;
  if (!me.data?.is_admin) {
    return <Alert variant="destructive"><AlertDescription>Installation admin access is required to manage users.</AlertDescription></Alert>;
  }

  const submit = async () => {
    if (!form.validate()) return false;
    return form.run(() => create.mutateAsync(form.values));
  };
  const next = users.data?.next_cursor ?? undefined;
  const confirmation = confirming;
  const isMe = (user: User) => user.id === me.data?.user_id;
  const texts: Record<ConfirmationKind, (user: User) => { title: string; label: string; message: string }> = {
    deactivate: (user) => ({ title: "Deactivate user", label: "Deactivate", message: `Deactivate ${user.email}? This suspends the user’s API keys.` }),
    activate: (user) => ({ title: "Reactivate user", label: "Reactivate", message: `Reactivate ${user.email}?` }),
    delete: (user) => ({ title: "Delete user", label: "Delete", message: `Delete ${user.email}? If the user owns keys, the API will reject deletion and show why.` }),
    "make-admin": (user) => ({ title: "Make installation admin", label: "Make admin", message: `Make ${user.email} an installation admin? They can then manage the whole installation.` }),
    "remove-admin": (user) => ({
      title: "Remove installation admin",
      label: "Remove admin",
      message: `Remove ${user.email} as an installation admin?${isMe(user) ? " You will lose installation access." : ""}`,
    }),
    transfer: (user) => ({
      title: "Transfer proxy_admin",
      label: "Transfer",
      message: `${user.email} becomes the proxy_admin and the current proxy_admin becomes an admin. The new proxy_admin's personal access tokens are revoked.`,
    }),
  };
  const confirmText = confirmation ? texts[confirmation.kind](confirmation.user) : null;

  return (
    <>
      <PageHeader
        title="Users"
        hint="Installation admins manage the whole installation; the proxy_admin is the highest role and moves only through a transfer."
        actions={<CreateButton can onClick={() => { form.reset(); setCreating(true); }}>New user</CreateButton>}
      />

      <DataTable
        rows={users.data?.items}
        loading={users.isPending}
        error={users.error ?? admins.error}
        onRetry={() => {
          void users.refetch();
          void admins.refetch();
        }}
        rowKey={(user) => user.id}
        paging={paging(
          next,
          users.isFetching,
          users.data?.items.length
            ? `${page * PAGE_SIZE + 1}–${page * PAGE_SIZE + users.data.items.length}${next ? "" : ` of ${page * PAGE_SIZE + users.data.items.length}`}`
            : "No users",
        )}
        empty="No users yet."
        columns={[
          { key: "email", header: "Email", sort: (a, b) => a.email.localeCompare(b.email), cell: (user) => <strong>{user.email}</strong> },
          { key: "name", header: "Name", cell: (user) => user.display_name || "—" },
          {
            key: "role",
            header: "Role",
            cell: (user) => {
              const role = roleOf.get(user.id)?.role;
              return role ? <Badge tone={role === "proxy_admin" ? "ok" : "muted"}>{role}</Badge> : "—";
            },
          },
          { key: "status", header: "Status", cell: (user) => <Badge tone={user.is_active ? "ok" : "danger"}>{user.is_active ? "Active" : "Inactive"}</Badge> },
          {
            key: "actions",
            header: "",
            cell: (user) => {
              const isSelf = isMe(user);
              const role = roleOf.get(user.id)?.role;
              return (
                <div className="flex justify-end gap-1 whitespace-nowrap">
                  {role === "admin" && canTransfer && (
                    <IconAction
                      label="Transfer proxy_admin"
                      icon={CrownIcon}
                      disabled={!user.is_active}
                      reason="An inactive user cannot become the proxy_admin"
                      onClick={() => setConfirming({ user, kind: "transfer" })}
                    />
                  )}
                  {role === "admin" && (
                    <IconAction label="Remove admin" icon={ShieldMinusIcon} onClick={() => setConfirming({ user, kind: "remove-admin" })} />
                  )}
                  {!role && (
                    <IconAction
                      label="Make admin"
                      icon={ShieldPlusIcon}
                      disabled={!user.is_active || !user.has_signed_in}
                      reason={user.is_active ? "The user must sign in once before receiving a role" : "An inactive user cannot become an admin"}
                      onClick={() => setConfirming({ user, kind: "make-admin" })}
                    />
                  )}
                  <IconAction
                    label={user.is_active ? "Deactivate" : "Reactivate"}
                    icon={user.is_active ? UserXIcon : UserCheckIcon}
                    disabled={isSelf && user.is_active}
                    reason="You can't deactivate your own account"
                    onClick={() => setConfirming({ user, kind: user.is_active ? "deactivate" : "activate" })}
                  />
                  <IconAction
                    label="Delete"
                    icon={Trash2Icon}
                    className="text-destructive hover:bg-destructive/10 hover:text-destructive"
                    disabled={isSelf}
                    reason="You can't delete your own account"
                    onClick={() => setConfirming({ user, kind: "delete" })}
                  />
                </div>
              );
            },
          },
        ]}
      />
      <FormDialog
        open={creating}
        title="New user"
        submitLabel="Create user"
        onClose={() => setCreating(false)}
        onSubmit={submit}
      >
        <TextField label="Email" value={form.values.email} onChange={(value) => form.set("email", value)} error={form.errors.email} autoFocus />
        <TextField label="Display name" value={form.values.display_name} onChange={(value) => form.set("display_name", value)} />
      </FormDialog>
      <ConfirmDialog
        open={!!confirmation}
        title={confirmText?.title ?? ""}
        confirmLabel={confirmText?.label ?? ""}
        message={confirmText?.message ?? ""}
        onConfirm={() => {
          if (!confirmation) return Promise.resolve();
          const { user, kind } = confirmation;
          if (kind === "delete") return remove.mutateAsync(user);
          if (kind === "make-admin") return makeAdmin.mutateAsync(user);
          if (kind === "remove-admin") return removeAdmin.mutateAsync(user);
          if (kind === "transfer") return transfer.mutateAsync(user);
          return update.mutateAsync({ user, changes: { is_active: kind === "activate" } });
        }}
        onClose={() => setConfirming(null)}
      />
    </>
  );
}
