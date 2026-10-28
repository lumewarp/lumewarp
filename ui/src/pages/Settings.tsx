import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api";
import { Badge, fmtDate, PageHeader, SecretOnce } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { Dialog } from "../components/Dialog";
import { FormDialog, required, TextField, useForm } from "../components/Form";
import { CreateButton } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { useMe, useMut } from "../hooks";
import { useTheme } from "../theme";

export function Settings() {
  const me = useMe();
  const { theme, cycle } = useTheme();
  return (
    <>
      <PageHeader title="Settings" description="Your session, access tokens and UI preferences." />
      <section className="card pad kv">
        <h2>Session</h2>
        <dl>
          <dt>Name</dt>
          <dd>{me.data?.display_name ?? "…"}</dd>
          <dt>Email</dt>
          <dd>{me.data?.email ?? "—"}</dd>
          <dt>Teams and roles</dt>
          <dd>
            {me.data?.is_superuser && <Badge tone="info">superuser</Badge>}
            {me.data?.teams.map((team) => (
              <Badge key={team.team_id} tone="info">
                {team.team_name} · {team.role}
              </Badge>
            ))}
            {me.data && !me.data.is_superuser && me.data.teams.length === 0 && <span className="muted">No team memberships.</span>}
          </dd>
        </dl>
      </section>
      <section className="card pad kv">
        <h2>Appearance</h2>
        <button className="btn" onClick={cycle}>
          Theme: {theme}
        </button>
      </section>
      <h2 className="section-gap">Personal access tokens</h2>
      <Tokens />
    </>
  );
}

function Tokens() {
  const me = useMe();
  const list = useQuery({ queryKey: ["tokens"], queryFn: () => api.tokens(), enabled: me.data?.kind !== "bootstrap" && !!me.data?.user_id });
  const [creating, setCreating] = useState(false);
  const [secret, setSecret] = useState<string | null>(null);
  const [revoking, setRevoking] = useState<string | null>(null);
  const form = useForm({ name: "" }, { name: required("Name") });
  const create = useMut((name: string) => api.createToken({ name }), (r) => setSecret(r.secret));
  const revoke = useMut((id: string) => api.revokeToken(id), "Token revoked");

  if (me.data && !me.data.user_id) return <p className="muted">The bootstrap token is not tied to a user, so it has no personal access tokens.</p>;
  return (
    <>
      <div className="page-actions toolbar">
        <CreateButton
          onClick={() => {
            form.reset();
            setCreating(true);
          }}
        >
          New token
        </CreateButton>
      </div>
      <DataTable
        rows={list.data}
        loading={list.isPending}
        error={list.error}
        onRetry={() => void list.refetch()}
        rowKey={(r) => r.id}
        empty="No tokens."
        columns={[
          { key: "name", header: "Name", cell: (r) => (<><strong>{r.name}</strong> <code className="muted">{r.display_prefix}…</code></>) },
          { key: "created", header: "Created", cell: (r) => fmtDate(r.created_at) },
          { key: "expires", header: "Expires", cell: (r) => fmtDate(r.expires_at) },
          { key: "used", header: "Last used", cell: (r) => fmtDate(r.last_used_at) },
          { key: "actions", header: "", cell: (r) => (r.revoked_at ? <Badge>Revoked</Badge> : <div className="row-actions"><button className="btn btn-sm btn-danger-ghost" onClick={() => setRevoking(r.id)}>Revoke</button></div>) },
        ]}
      />
      <FormDialog open={creating} title="New personal access token" submitLabel="Create token" onClose={() => setCreating(false)} onSubmit={() => (form.validate() ? form.run(() => create.mutateAsync(form.values.name)) : false)}>
        <TextField label="Name" value={form.values.name} onChange={(v) => form.set("name", v)} error={form.errors.name} autoFocus placeholder="laptop" />
      </FormDialog>
      <Dialog open={!!secret} title="Token created" onClose={() => setSecret(null)} footer={<button className="btn btn-primary" onClick={() => setSecret(null)}>I’ve saved it</button>}>
        {secret && <SecretOnce secret={secret} />}
      </Dialog>
      <ConfirmDialog open={!!revoking} title="Revoke token" confirmLabel="Revoke" message="Anything using this token stops working immediately." onConfirm={() => revoke.mutateAsync(revoking!)} onClose={() => setRevoking(null)} />
    </>
  );
}
