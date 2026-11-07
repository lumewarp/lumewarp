import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api";
import { Badge, fmtDateTime, PageHeader } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { FormDialog, required, TextField, useForm } from "../components/Form";
import { Button } from "../components/ui/button";
import { CreateButton } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { useMut } from "../hooks";
import { runTokenSecretMutation } from "../lib/keySecret";
import { useShowSecret } from "../components/SecretProvider";

type Gateway = Awaited<ReturnType<typeof api.gateways>>[number];
type GwToken = Gateway["tokens"][number];

export function Gateways() {
  const list = useQuery({ queryKey: ["gateways"], queryFn: () => api.gateways(), refetchInterval: 10_000 });
  const versions = useQuery({ queryKey: ["config-versions"], queryFn: () => api.configVersions() });
  const latest = versions.data?.reduce((m, v) => Math.max(m, v.version), 0);
  const showSecret = useShowSecret();
  const [creating, setCreating] = useState(false);
  const [revoking, setRevoking] = useState<{ gateway: string; token: GwToken } | null>(null);
  const form = useForm({ gateway_id: "", name: "" }, { gateway_id: required("Gateway id") });
  const create = useMut((v: { gateway_id: string; name: string }) =>
    runTokenSecretMutation(() => api.createGatewayToken(v), (value) => showSecret({ value, title: "Gateway token created" })),
  );
  const revoke = useMut(({ g, t }: { g: string; t: string }) => api.revokeGatewayToken(g, t), "Token revoked");

  return (
    <>
      <PageHeader
        title="Gateways"
        actions={
          <CreateButton
            onClick={() => {
              form.reset();
              setCreating(true);
            }}
          >
            New gateway token
          </CreateButton>
        }
      />
      <DataTable
        rows={list.data}
        loading={list.isPending}
        error={list.error}
        onRetry={() => void list.refetch()}
        rowKey={(r) => r.gateway_id}
        empty="No gateways have registered yet. Create a token to connect one."
        columns={[
          { key: "id", header: "Gateway", sort: (a, b) => a.gateway_id.localeCompare(b.gateway_id), cell: (r) => (<><strong>{r.gateway_id}</strong> <span className="text-muted-foreground">{r.gateway_version}</span></>) },
          { key: "state", header: "State", cell: (r) => (r.connected ? <Badge tone="ok">Connected</Badge> : <Badge>Disconnected</Badge>) },
          {
            key: "version",
            header: "Config",
            cell: (r) => {
              const behind = r.acked_version !== null && latest !== undefined && r.acked_version < latest;
              return (
                <>
                  {r.acked_version === null ? "—" : `v${r.acked_version}`} {behind && <Badge tone="warn">behind v{latest}</Badge>}
                  {r.pending_version !== null && <Badge tone="info">pending v{r.pending_version}</Badge>}
                  {r.last_nack_version !== null && <Badge tone="danger" >NACK v{r.last_nack_version}{r.last_nack_code ? `: ${r.last_nack_code}` : ""}</Badge>}
                  {r.quarantined_version !== null && <Badge tone="danger">quarantined v{r.quarantined_version}</Badge>}
                </>
              );
            },
          },
          { key: "seen", header: "Last seen", cell: (r) => (r.last_seen_at ? fmtDateTime(r.last_seen_at) : "—") },
          {
            key: "tokens",
            header: "Tokens",
            cell: (r) => (
              <ul className="m-0 mb-3 flex list-none flex-col gap-1 p-0">
                {r.tokens.map((t) => (
                  <li key={t.id}>
                    <code>{t.display_prefix}…</code> {t.name && <span className="text-muted-foreground">{t.name}</span>} {t.revoked_at ? <Badge>Revoked</Badge> : <Button variant="ghost" size="sm" className="text-destructive hover:bg-destructive/10 hover:text-destructive" onClick={() => setRevoking({ gateway: r.gateway_id, token: t })}>Revoke</Button>}
                  </li>
                ))}
              </ul>
            ),
          },
        ]}
      />
      <FormDialog open={creating} title="New gateway token" submitLabel="Create token" onClose={() => setCreating(false)} onSubmit={() => (form.validate() ? form.run(() => create.mutateAsync(form.values)) : false)}>
        <TextField label="Gateway id" value={form.values.gateway_id} onChange={(v) => form.set("gateway_id", v)} error={form.errors.gateway_id} autoFocus hint="The identifier the gateway reports." />
        <TextField label="Label" value={form.values.name} onChange={(v) => form.set("name", v)} />
      </FormDialog>
      <ConfirmDialog open={!!revoking} title="Revoke gateway token" confirmLabel="Revoke" message="The gateway using this token can no longer connect. It keeps serving its last good config." onConfirm={() => revoke.mutateAsync({ g: revoking!.gateway, t: revoking!.token.id })} onClose={() => setRevoking(null)} />
    </>
  );
}
