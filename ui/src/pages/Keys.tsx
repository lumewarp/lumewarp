import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { api, type Key, type Schemas } from "../api";
import { fmtDate, KeyBadge, PageHeader, SecretOnce, SuspendedReason } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { Dialog } from "../components/Dialog";
import { FormDialog, required, SelectField, TextField, useForm } from "../components/Form";
import { CreateButton, RowActions } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { useAction, useCan, useCatalog, useMe, useMut } from "../hooks";
import { Badge as UiBadge } from "../components/ui/badge";
import { Skeleton } from "../components/ui/skeleton";

type Values = { name: string; team_id: string; expires: string };
const blank: Values = { name: "", team_id: "", expires: "" };

function expiresAt(value: string) {
  return value ? new Date(`${value}T23:59:59`).toISOString() : null;
}

/**
 * The names a new key can call: what the team allows, narrowed by the owner's own member policy.
 * Read-only; built from what the API returns for any team member (the team, its members, the catalog).
 */
function CallableNames({ teamId, userId }: { teamId: string; userId: string | null | undefined }) {
  const team = useQuery({ queryKey: ["team", teamId], queryFn: () => api.team(teamId), enabled: !!teamId });
  const members = useQuery({ queryKey: ["members", teamId], queryFn: () => api.members(teamId), enabled: !!teamId });
  const catalog = useCatalog();
  const names = useMemo(() => {
    if (!team.data || !members.data || !catalog.data) return null;
    const own = members.data.find((m) => m.user_id === userId);
    let allowed: Set<string> | null = team.data.model_access === "list" ? new Set(team.data.allowed_models) : null;
    if (own?.model_access === "list") {
      const mine = new Set(own.allowed_models);
      allowed = allowed ? new Set([...allowed].filter((n) => mine.has(n))) : mine;
    }
    return catalog.data.filter((e) => allowed === null || allowed.has(e.name));
  }, [team.data, members.data, catalog.data, userId]);
  const error = team.error ?? members.error ?? catalog.error;
  return (
    <div className="flex flex-col gap-2" data-testid="callable-names">
      <span className="text-sm font-semibold">This key can call</span>
      {error ? (
        <p className="field-error" role="alert">
          {error.message}
        </p>
      ) : !names ? (
        <Skeleton className="h-4 w-full" />
      ) : names.length === 0 ? (
        <p className="text-sm text-muted-foreground">No names. The team or your own access allows none.</p>
      ) : (
        (["model", "alias", "router"] as const).map((kind) => {
          const items = names.filter((e) => e.kind === kind);
          return (
            items.length > 0 && (
              <div key={kind} role="group" aria-label={`${kind}s`} className="flex flex-wrap items-center gap-1.5">
                <span className="w-16 text-xs text-muted-foreground capitalize">{kind}s</span>
                {items.map((e) => (
                  <UiBadge key={e.id} variant="muted">
                    {e.name}
                  </UiBadge>
                ))}
              </div>
            )
          );
        })
      )}
      <p className="field-hint">Read-only. Access is set on the team and on your membership.</p>
    </div>
  );
}

export function Keys() {
  const me = useMe();
  const perms = useCan();
  const list = useQuery({ queryKey: ["own-keys"], queryFn: () => api.ownKeys() });
  const [editing, setEditing] = useState<Key | "new" | null>(null);
  const [revoking, setRevoking] = useState<Key | null>(null);
  const [secret, setSecret] = useState<string | null>(null);
  const [secretTitle, setSecretTitle] = useState("API key created");
  const teams = (me.data?.teams ?? []).map((team) => ({ value: team.team_id, label: team.team_name }));
  const form = useForm<Values>(blank, { name: required("Name"), team_id: required("Team") });
  const create = useMut(
    (values: Values) => api.createKey(values.team_id, { name: values.name, expires_at: expiresAt(values.expires) }),
    (result) => {
      setSecretTitle("API key created");
      setSecret(result.secret);
    },
  );
  const update = useMut(
    ({ key, values }: { key: Key; values: Values }) => {
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
    form.reset(
      key === "new"
        ? { ...blank, team_id: teams[0]?.value ?? "" }
        : { name: key.name, team_id: key.team_id, expires: key.expires_at?.slice(0, 10) ?? "" },
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

  return (
    <>
      <PageHeader
        title="My keys"
        description="Keys you own across your teams. Secrets are shown once when created or rotated."
        actions={<CreateButton can={!!me.data?.user_id && teams.length > 0} onClick={() => open("new")}>New key</CreateButton>}
      />
      {!me.data?.user_id && <p className="muted">The break-glass session does not own API keys.</p>}
      <DataTable
        rows={list.data}
        loading={list.isPending}
        error={list.error}
        onRetry={() => void list.refetch()}
        rowKey={(key) => key.id}
        empty="You do not own any API keys yet."
        columns={[
          { key: "name", header: "Name", sort: (a, b) => a.name.localeCompare(b.name), cell: (key) => <><strong>{key.name}</strong> <code className="muted">{key.display_prefix}…</code></> },
          { key: "owner", header: "Owner", cell: () => me.data?.email ?? "—" },
          { key: "team", header: "Team", cell: (key) => me.data?.teams.find((team) => team.team_id === key.team_id)?.team_name ?? key.team_id.slice(0, 8) },
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
                            ? ownerInactive || !perms.canResumeKey(key)
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
        {editing === "new" && (
          <SelectField
            label="Team"
            value={form.values.team_id}
            onChange={(value) => form.set("team_id", value)}
            error={form.errors.team_id}
            options={teams}
          />
        )}
        <TextField label="Name" value={form.values.name} onChange={(value) => form.set("name", value)} error={form.errors.name} autoFocus />
        <TextField label="Expires" type="date" value={form.values.expires} onChange={(value) => form.set("expires", value)} hint="Empty means the key never expires." />
        {editing === "new" && form.values.team_id && <CallableNames teamId={form.values.team_id} userId={me.data?.user_id} />}
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
