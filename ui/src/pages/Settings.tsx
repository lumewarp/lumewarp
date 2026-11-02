import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api";
import { Badge, fmtDate, PageHeader, SecretOnce } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { Dialog } from "../components/Dialog";
import { FormDialog, required, TextField, useForm } from "../components/Form";
import { Button } from "../components/ui/button";
import { Card } from "../components/ui/card";
import { DescriptionDetails, DescriptionList, DescriptionTerm } from "../components/ui/description-list";
import { CreateButton } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { useMe, useMut } from "../hooks";
import { useTheme } from "../theme";

const CARD = "mb-4 gap-3 p-4 shadow-none";

export function Settings() {
  const me = useMe();
  const { theme, cycle } = useTheme();
  return (
    <>
      <PageHeader title="Settings" description="Your session, access tokens and UI preferences." />
      <Card className={CARD}>
        <h2>Session</h2>
        <DescriptionList>
          <DescriptionTerm>Name</DescriptionTerm>
          <DescriptionDetails>{me.data?.display_name ?? "…"}</DescriptionDetails>
          <DescriptionTerm>Email</DescriptionTerm>
          <DescriptionDetails>{me.data?.email ?? "—"}</DescriptionDetails>
          <DescriptionTerm>Teams and roles</DescriptionTerm>
          <DescriptionDetails>
            {me.data?.is_admin && <Badge tone="info">admin group</Badge>}
            {me.data?.teams.map((team) => (
              <Badge key={team.team_id} tone="info">
                {team.team_name} · {team.role}
              </Badge>
            ))}
            {me.data && !me.data.is_admin && me.data.teams.length === 0 && <span className="text-muted-foreground">No team memberships.</span>}
          </DescriptionDetails>
        </DescriptionList>
      </Card>
      <Card className={CARD}>
        <h2>Appearance</h2>
        <div>
          <Button variant="outline" onClick={cycle}>
            Theme: {theme}
          </Button>
        </div>
      </Card>
      <h2 className="mt-6">Personal access tokens</h2>
      <Tokens />
    </>
  );
}

function Tokens() {
  const me = useMe();
  const list = useQuery({ queryKey: ["tokens"], queryFn: () => api.tokens(), enabled: me.data?.kind !== "break_glass" && !!me.data?.user_id });
  const [creating, setCreating] = useState(false);
  const [secret, setSecret] = useState<string | null>(null);
  const [revoking, setRevoking] = useState<string | null>(null);
  const form = useForm({ name: "" }, { name: required("Name") });
  const create = useMut((name: string) => api.createToken({ name }), (r) => setSecret(r.secret));
  const revoke = useMut((id: string) => api.revokeToken(id), "Token revoked");

  if (me.data && !me.data.user_id) return <p className="text-muted-foreground">The break-glass session is not tied to a user, so it has no personal access tokens.</p>;
  return (
    <>
      <div className="mb-3 flex flex-wrap items-center justify-end gap-2">
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
          { key: "name", header: "Name", cell: (r) => (<><strong>{r.name}</strong> <code className="text-muted-foreground">{r.display_prefix}…</code></>) },
          { key: "created", header: "Created", cell: (r) => fmtDate(r.created_at) },
          { key: "expires", header: "Expires", cell: (r) => fmtDate(r.expires_at) },
          { key: "used", header: "Last used", cell: (r) => fmtDate(r.last_used_at) },
          { key: "actions", header: "", cell: (r) => (r.revoked_at ? <Badge>Revoked</Badge> : <div className="flex justify-end gap-1.5 whitespace-nowrap"><Button variant="ghost" size="sm" className="text-destructive hover:bg-destructive/10 hover:text-destructive" onClick={() => setRevoking(r.id)}>Revoke</Button></div>) },
        ]}
      />
      <FormDialog open={creating} title="New personal access token" submitLabel="Create token" onClose={() => setCreating(false)} onSubmit={() => (form.validate() ? form.run(() => create.mutateAsync(form.values.name)) : false)}>
        <TextField label="Name" value={form.values.name} onChange={(v) => form.set("name", v)} error={form.errors.name} autoFocus placeholder="laptop" />
      </FormDialog>
      <Dialog open={!!secret} title="Token created" onClose={() => setSecret(null)} footer={<Button onClick={() => setSecret(null)}>I’ve saved it</Button>}>
        {secret && <SecretOnce secret={secret} />}
      </Dialog>
      <ConfirmDialog open={!!revoking} title="Revoke token" confirmLabel="Revoke" message="Anything using this token stops working immediately." onConfirm={() => revoke.mutateAsync(revoking!)} onClose={() => setRevoking(null)} />
    </>
  );
}
