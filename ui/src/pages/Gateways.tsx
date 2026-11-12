import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight } from "lucide-react";
import { useState, type ReactNode } from "react";
import { api } from "../api";
import { Badge, fmtDateTime, PageHeader } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { FormDialog, required, TextField, useForm } from "../components/Form";
import { Button } from "../components/ui/button";
import { CreateButton } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { useMut } from "../hooks";
import { cn } from "../lib/utils";
import { runTokenSecretMutation } from "../lib/keySecret";
import { useShowSecret } from "../components/SecretProvider";

type Gateway = Awaited<ReturnType<typeof api.gateways>>[number];
type GwToken = Gateway["tokens"][number];
type Instance = Gateway["instances"][number];

/** How the connected instances of one gateway id share rate limits, and whether they disagree. */
export function limitsState(instances: Instance[]): { kind: "shared" | "per-instance" | "mixed" | "none"; namespace: string } {
  const live = instances.filter((i) => i.connected);
  if (!live.length) return { kind: "none", namespace: "" };
  const on = live.filter((i) => i.shared_limits_enabled);
  if (!on.length) return { kind: "per-instance", namespace: "" };
  const spaces = new Set(on.map((i) => i.shared_limits_namespace));
  if (on.length === live.length && spaces.size === 1) return { kind: "shared", namespace: [...spaces][0] };
  return { kind: "mixed", namespace: [...spaces].join(", ") };
}

function versionsDiffer(instances: Instance[]) {
  return new Set(instances.filter((i) => i.connected).map((i) => i.gateway_version)).size > 1;
}

function RateLimits({ g }: { g: Gateway }) {
  const st = limitsState(g.instances);
  if (st.kind === "none") return <span className="text-muted-foreground">—</span>;
  if (st.kind === "shared") return <Badge tone="ok">Shared{st.namespace ? ` (${st.namespace})` : ""}</Badge>;
  if (st.kind === "per-instance") return <Badge>Per instance</Badge>;
  return <Badge tone="warn">Instances disagree on shared limits{st.namespace ? ` (${st.namespace})` : ""}</Badge>;
}

function Labelled({ label, children, className }: { label: string; children: ReactNode; className?: string }) {
  return (
    <span className={className}>
      <span className="sr-only">{label}: </span>
      {children}
    </span>
  );
}

function InstanceTable({ g }: { g: Gateway }) {
  return (
    <ul id={`instances-${g.gateway_id}`} aria-label={`Instances of ${g.gateway_id}`} className="m-0 mt-2 flex list-none flex-col gap-1.5 p-0 text-sm" data-testid={`instances-${g.gateway_id}`}>
      {g.instances.map((i) => (
        <li key={i.instance_id} className={cn("flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border px-2.5 py-1.5", !i.connected && "text-muted-foreground")} data-testid={`instance-${i.instance_id}`}>
          <Labelled label="Instance"><code>{i.instance_id}</code></Labelled>
          <Labelled label="State">{i.connected ? <Badge tone="ok">Connected</Badge> : <Badge>Disconnected</Badge>}</Labelled>
          <Labelled label="Version">{i.gateway_version}</Labelled>
          <Labelled label="Applied config" className="flex flex-wrap items-center gap-1">
            {i.acked_version === null ? "—" : `v${i.acked_version}`}
            {i.pending_version !== null && <Badge tone="info">pending v{i.pending_version}</Badge>}
            {i.last_nack_version !== null && <Badge tone="danger">NACK v{i.last_nack_version}</Badge>}
          </Labelled>
          <Labelled label="Last seen" className="text-xs text-muted-foreground">{i.last_seen_at ? fmtDateTime(i.last_seen_at) : "—"}</Labelled>
          {i.peer && <Labelled label="Peer" className="text-xs text-muted-foreground">{i.peer}</Labelled>}
        </li>
      ))}
    </ul>
  );
}

function GatewayCell({ g }: { g: Gateway }) {
  const [open, setOpen] = useState(false);
  const live = g.instances.filter((i) => i.connected).length;
  const Icon = open ? ChevronDown : ChevronRight;
  const none = g.instances.length === 0;
  return (
    <div>
      {none ? (
        <strong className="px-2.5">{g.gateway_id}</strong>
      ) : (
        <Button type="button" variant="ghost" size="sm" aria-expanded={open} aria-controls={`instances-${g.gateway_id}`} aria-label={`${open ? "Hide" : "Show"} instances of ${g.gateway_id}`} onClick={() => setOpen(!open)}>
          <Icon aria-hidden="true" />
          <strong>{g.gateway_id}</strong>
        </Button>
      )}{" "}
      <span className="text-muted-foreground">{g.gateway_version}</span>{" "}
      <span className="text-xs text-muted-foreground" data-testid={`instance-count-${g.gateway_id}`}>{none ? "No instances yet" : `${live}/${g.instances.length} instances connected`}</span>
      {versionsDiffer(g.instances) && <Badge tone="warn">Versions differ</Badge>}
      {open && !none && <InstanceTable g={g} />}
    </div>
  );
}

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
          { key: "id", header: "Gateway", sort: (a, b) => a.gateway_id.localeCompare(b.gateway_id), cell: (r) => <GatewayCell g={r} /> },
          { key: "limits", header: "Rate limits", hideBelow: "md", cell: (r) => <RateLimits g={r} /> },
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
