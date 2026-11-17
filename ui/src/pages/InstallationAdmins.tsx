import { useQuery } from "@tanstack/react-query";
import { CrownIcon } from "lucide-react";
import { useState } from "react";

import { api, type InstallationAdmin } from "../api";
import { Badge, fmtDate, PageHeader } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { FormDialog, useForm } from "../components/Form";
import { CreateButton, IconAction, RowActions } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { Alert, AlertDescription } from "../components/ui/alert";
import { ComboboxField } from "../components/ui/combobox-field";
import { useMe, useMut } from "../hooks";

function AddAdminDialog({ admins, onClose }: { admins: InstallationAdmin[]; onClose: () => void }) {
  const users = useQuery({ queryKey: ["user-directory"], queryFn: () => api.users() });
  const form = useForm<{ user_id: string }>({ user_id: "" }, { user_id: (value) => (value ? undefined : "Choose a user") });
  const assign = useMut((id: string) => api.assignInstallationAdmin(id), "Installation admin added");
  const current = new Set(admins.map((admin) => admin.user_id));
  const options = (users.data ?? [])
    .filter((user) => user.is_active && !current.has(user.id))
    .map((user) => ({ value: user.id, label: user.email }));
  return (
    <FormDialog
      open
      title="Add an installation admin"
      submitLabel="Add admin"
      submitDisabled={options.length === 0}
      onClose={onClose}
      onSubmit={async () => {
        if (!form.validate()) return false;
        return form.run(() => assign.mutateAsync(form.values.user_id));
      }}
    >
      {options.length ? (
        <ComboboxField
          label="User"
          value={form.values.user_id}
          onChange={(value) => form.set("user_id", value)}
          error={form.errors.user_id}
          options={[{ value: "", label: "Select a user" }, ...options]}
          hint="The user must be active and have signed in once."
        />
      ) : (
        <p className="m-0 text-sm text-muted-foreground">No eligible users: every active user is already an installation admin.</p>
      )}
    </FormDialog>
  );
}

function TransferDialog({ target, onClose }: { target: InstallationAdmin; onClose: () => void }) {
  const transfer = useMut((id: string) => api.transferProxyAdmin(id), "Proxy admin transferred");
  return (
    <FormDialog
      open
      title="Transfer proxy_admin"
      submitLabel="Transfer"
      onClose={onClose}
      onSubmit={async () => {
        await transfer.mutateAsync(target.user_id);
      }}
    >
      <Alert variant="warn" role="status">
        <AlertDescription>
          {target.email} becomes the proxy_admin and the current proxy_admin becomes an admin. The new proxy_admin's personal access tokens are revoked.
        </AlertDescription>
      </Alert>
    </FormDialog>
  );
}

export function InstallationAdmins() {
  const me = useMe();
  const admins = useQuery({ queryKey: ["installation-admins"], queryFn: () => api.installationAdmins() });
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<InstallationAdmin | null>(null);
  const [transferring, setTransferring] = useState<InstallationAdmin | null>(null);
  const remove = useMut((id: string) => api.removeInstallationAdmin(id), "Installation admin removed");
  const canTransfer = me.data?.installation_role === "proxy_admin" || me.data?.kind === "break_glass";
  const myId = me.data?.user_id;
  return (
    <>
      <PageHeader
        title="Installation admins"
        hint="Installation admins manage the whole installation; the proxy_admin is the highest role and moves only through a transfer."
        actions={<CreateButton onClick={() => setAdding(true)}>Add admin</CreateButton>}
      />
      <DataTable
        rows={admins.data}
        loading={admins.isPending}
        error={admins.error}
        onRetry={() => void admins.refetch()}
        rowKey={(admin) => admin.user_id}
        empty="No installation admins."
        columns={[
          { key: "email", header: "Email", sort: (a, b) => a.email.localeCompare(b.email), cell: (admin) => <strong>{admin.email}</strong> },
          { key: "name", header: "Name", cell: (admin) => admin.display_name || "—" },
          {
            key: "role",
            header: "Role",
            cell: (admin) => <Badge tone={admin.role === "proxy_admin" ? "ok" : "muted"}>{admin.role}</Badge>,
          },
          {
            key: "status",
            header: "Status",
            cell: (admin) => <Badge tone={admin.is_active ? "ok" : "danger"}>{admin.is_active ? "Active" : "Inactive"}</Badge>,
          },
          { key: "by", header: "Assigned by", cell: (admin) => admins.data?.find((row) => row.user_id === admin.assigned_by)?.email ?? admin.assigned_by ?? "—" },
          { key: "assigned", header: "Assigned", cell: (admin) => fmtDate(admin.assigned_at) },
          {
            key: "actions",
            header: "",
            cell: (admin) =>
              admin.role === "proxy_admin" ? null : (
                <RowActions
                  deleteLabel="Remove"
                  onDelete={() => setRemoving(admin)}
                  extra={canTransfer && <IconAction label="Transfer proxy_admin" icon={CrownIcon} onClick={() => setTransferring(admin)} />}
                />
              ),
          },
        ]}
      />
      {adding && <AddAdminDialog admins={admins.data ?? []} onClose={() => setAdding(false)} />}
      {transferring && <TransferDialog target={transferring} onClose={() => setTransferring(null)} />}
      <ConfirmDialog
        open={!!removing}
        title="Remove installation admin"
        message={`Remove ${removing?.email ?? ""} as an installation admin?${removing?.user_id === myId ? " You will lose installation access." : ""}`}
        confirmLabel="Remove"
        onConfirm={() => remove.mutateAsync(removing!.user_id)}
        onClose={() => setRemoving(null)}
      />
    </>
  );
}
