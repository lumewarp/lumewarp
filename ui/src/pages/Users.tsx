import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Schemas, type User } from "../api";
import { Badge, PageHeader } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { FormDialog, required, TextField, useForm } from "../components/Form";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { Card } from "../components/ui/card";
import { CreateButton } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { useMe, useMut } from "../hooks";

type Values = Schemas["UserCreate"];
type ConfirmationKind = "activate" | "deactivate" | "delete";

const PAGE_SIZE = 50;
const blank: Values = { email: "", display_name: "", is_active: true };

export function Users() {
  const me = useMe();
  const [cursor, setCursor] = useState<string | undefined>();
  const [previousCursors, setPreviousCursors] = useState<(string | undefined)[]>([]);
  const [creating, setCreating] = useState(false);
  const [confirming, setConfirming] = useState<{ user: User; kind: ConfirmationKind } | null>(null);
  const users = useQuery({
    queryKey: ["users", cursor],
    queryFn: () => api.usersPage(cursor),
    enabled: !!me.data?.is_admin,
  });
  const form = useForm<Values>(blank, { email: required("Email") });
  const create = useMut((values: Values) => api.createUser(values), "User created");
  const update = useMut(
    ({ user, changes }: { user: User; changes: Schemas["UserUpdate"] }) =>
      api.updateUser(user.id, changes),
    "User updated",
  );
  const remove = useMut((user: User) => api.deleteUser(user.id), "User deleted");

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
  const confirmMessage = confirmation
    ? confirmation.kind === "deactivate"
      ? `Deactivate ${confirmation.user.email}? This suspends the user’s API keys.`
      : confirmation.kind === "activate"
        ? `Reactivate ${confirmation.user.email}?`
        : `Delete ${confirmation.user.email}? If the user owns keys, the API will reject deletion and show why.`
    : "";
  const confirmLabel =
    confirmation?.kind === "delete"
      ? "Delete"
      : confirmation?.kind === "deactivate"
        ? "Deactivate"
        : "Reactivate";

  return (
    <>
      <PageHeader
        title="Users"
        description="Manage users. Installation admin roles are managed in Postgres."
        actions={<CreateButton can onClick={() => { form.reset(); setCreating(true); }}>New user</CreateButton>}
      />
      <DataTable
        rows={users.data?.items}
        loading={users.isPending}
        error={users.error}
        onRetry={() => void users.refetch()}
        rowKey={(user) => user.id}
        pageSize={PAGE_SIZE}
        empty="No users yet."
        columns={[
          { key: "email", header: "Email", sort: (a, b) => a.email.localeCompare(b.email), cell: (user) => <strong>{user.email}</strong> },
          { key: "name", header: "Name", cell: (user) => user.display_name || "—" },
          { key: "status", header: "Status", cell: (user) => <Badge tone={user.is_active ? "ok" : "danger"}>{user.is_active ? "Active" : "Inactive"}</Badge> },
          {
            key: "actions",
            header: "",
            cell: (user) => {
              const isSelf = user.id === me.data?.user_id;
              return (
                <div className="flex justify-end gap-1.5 whitespace-nowrap">
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={isSelf && user.is_active}
                    onClick={() => setConfirming({ user, kind: user.is_active ? "deactivate" : "activate" })}
                  >
                    {user.is_active ? "Deactivate" : "Reactivate"}
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="text-destructive hover:bg-destructive/10 hover:text-destructive"
                    disabled={isSelf}
                    onClick={() => setConfirming({ user, kind: "delete" })}
                  >
                    Delete
                  </Button>
                </div>
              );
            },
          },
        ]}
      />
      <Card className="flex-row flex-wrap items-center justify-end gap-2 px-3.5 py-2 text-sm text-muted-foreground shadow-none" aria-label="User pages">
        <span>Showing {users.data?.items.length ?? 0} users · up to {PAGE_SIZE} per page</span>
        <Button
          variant="outline"
          size="sm"
          disabled={previousCursors.length === 0 || users.isFetching}
          onClick={() => {
            const previous = previousCursors.at(-1);
            setPreviousCursors((stack) => stack.slice(0, -1));
            setCursor(previous);
          }}
        >
          Previous
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={!next || users.isFetching}
          onClick={() => {
            if (!next) return;
            setPreviousCursors((stack) => [...stack, cursor]);
            setCursor(next);
          }}
        >
          Next
        </Button>
      </Card>
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
        title={
          confirmation?.kind === "delete"
            ? "Delete user"
            : confirmation?.kind === "deactivate"
              ? "Deactivate user"
              : "Reactivate user"
        }
        confirmLabel={confirmLabel}
        message={confirmMessage}
        onConfirm={() => {
          if (!confirmation) return Promise.resolve();
          if (confirmation.kind === "delete") return remove.mutateAsync(confirmation.user);
          const changes: Schemas["UserUpdate"] =
            confirmation.kind === "activate"
              ? { is_active: true }
              : { is_active: false };
          return update.mutateAsync({ user: confirmation.user, changes });
        }}
        onClose={() => setConfirming(null)}
      />
    </>
  );
}
