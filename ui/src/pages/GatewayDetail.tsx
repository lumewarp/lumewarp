import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { useState } from "react";

import { api } from "../api";
import { Badge, fmtDateTime, PageHeader } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { DataTable } from "../components/Table";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Badge as UiBadge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card } from "../components/ui/card";
import { DescriptionDetails, DescriptionList, DescriptionTerm } from "../components/ui/description-list";
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyTitle } from "../components/ui/empty";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../components/ui/tabs";
import { useMut } from "../hooks";
import { cn } from "../lib/utils";
import {
  gatewayWarnings,
  GatewayTokenDialog,
  oddInstances,
  rateLimitsSummary,
  rateLimitsText,
  type Gateway,
  type GwToken,
  type Instance,
} from "./Gateways";

/*
 * One gateway id: what it reports, its processes and the tokens that authenticate them. The list keeps only
 * monitoring columns; the warnings, NACK reason and token actions live here. The gateway comes from the same
 * ["gateways"] query as the list, so opening this page reuses its cache and shares its polling.
 */

function BackToGateways() {
  return (
    <Button variant="outline" asChild>
      <Link to="/gateways">All gateways</Link>
    </Button>
  );
}

export function GatewayDetail() {
  const { gatewayId } = useParams({ strict: false }) as { gatewayId: string };
  const gateways = useQuery({ queryKey: ["gateways"], queryFn: () => api.gateways(), refetchInterval: 10_000 });
  const versions = useQuery({ queryKey: ["config-versions"], queryFn: () => api.configVersions() });
  const latest = versions.data?.reduce((m, v) => Math.max(m, v.version), 0);
  const [creating, setCreating] = useState(false);
  const [opened, setOpened] = useState(0);
  const [tab, setTab] = useState<"instances" | "tokens">("instances");

  if (gateways.isPending) {
    return <div className="p-8 text-center text-muted-foreground" aria-busy="true">Loading gateway…</div>;
  }
  // A failed background poll keeps the last data on screen; only a first load that failed replaces the page.
  if (gateways.error && !gateways.data) {
    return (
      <>
        <PageHeader title={gatewayId} actions={<BackToGateways />} />
        <Alert variant="destructive" data-testid="state-error">
          <AlertDescription className="flex flex-col items-start gap-3">
            <p>{gateways.error.message}</p>
            <Button variant="outline" size="sm" onClick={() => void gateways.refetch()}>
              Retry
            </Button>
          </AlertDescription>
        </Alert>
      </>
    );
  }

  const gateway = gateways.data?.find((row) => row.gateway_id === gatewayId);
  if (!gateway) {
    return (
      <>
        <PageHeader title="Gateway not found" actions={<BackToGateways />} />
        <Empty className="border">
          <EmptyHeader>
            <EmptyTitle>No gateway with this id</EmptyTitle>
            <EmptyDescription>“{gatewayId}” is not known to the control plane.</EmptyDescription>
          </EmptyHeader>
          <EmptyContent>
            <Button variant="outline" asChild>
              <Link to="/gateways">All gateways</Link>
            </Button>
          </EmptyContent>
        </Empty>
      </>
    );
  }

  const warnings = gatewayWarnings(gateway);

  return (
    <>
      <PageHeader
        title={gateway.gateway_id}
        actions={
          <>
            <BackToGateways />
            <Button
              onClick={() => {
                setOpened((n) => n + 1);
                setCreating(true);
              }}
            >
              New token
            </Button>
          </>
        }
      />
      {warnings.length > 0 && (
        <Alert variant="warn" className="mb-5" data-testid="gateway-warnings">
          <AlertDescription>
            <ul className="m-0 flex list-disc flex-col gap-1 pl-4">
              {warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          </AlertDescription>
        </Alert>
      )}
      <Card className="mb-5 gap-3 p-4 shadow-none">
        <DescriptionList>
          <DescriptionTerm>State</DescriptionTerm>
          <DescriptionDetails>
            <Badge tone={gateway.connected ? "ok" : "muted"}>{gateway.connected ? "Connected" : "Disconnected"}</Badge>
          </DescriptionDetails>
          <DescriptionTerm>Instances</DescriptionTerm>
          <DescriptionDetails>
            {gateway.instances.length ? `${gateway.instances.filter((i) => i.connected).length}/${gateway.instances.length} connected` : "No instances yet"}
          </DescriptionDetails>
          <DescriptionTerm>Gateway version</DescriptionTerm>
          <DescriptionDetails>{gateway.gateway_version || "—"}</DescriptionDetails>
          <DescriptionTerm>Applied config</DescriptionTerm>
          <DescriptionDetails>
            <span className="inline-flex flex-wrap items-center gap-1.5">
              {gateway.acked_version === null ? <span className="text-muted-foreground">—</span> : `v${gateway.acked_version}`}
              {gateway.acked_version !== null && latest !== undefined && gateway.acked_version < latest && <Badge tone="warn">behind v{latest}</Badge>}
              {gateway.pending_version !== null && <Badge tone="info">pending v{gateway.pending_version}</Badge>}
            </span>
          </DescriptionDetails>
          <DescriptionTerm>Rate limits</DescriptionTerm>
          <DescriptionDetails>
            <RateLimitsDetail gateway={gateway} />
          </DescriptionDetails>
          <DescriptionTerm>Capabilities</DescriptionTerm>
          <DescriptionDetails>
            {gateway.capabilities.length ? <span className="[overflow-wrap:anywhere]">{gateway.capabilities.join(", ")}</span> : <span className="text-muted-foreground">—</span>}
          </DescriptionDetails>
          {gateway.connected ? (
            <>
              <DescriptionTerm>Connected since</DescriptionTerm>
              <DescriptionDetails>{gateway.connected_at ? fmtDateTime(gateway.connected_at) : "—"}</DescriptionDetails>
            </>
          ) : (
            <>
              <DescriptionTerm>Disconnected at</DescriptionTerm>
              <DescriptionDetails>{gateway.disconnected_at ? fmtDateTime(gateway.disconnected_at) : "—"}</DescriptionDetails>
            </>
          )}
          <DescriptionTerm>Last seen</DescriptionTerm>
          <DescriptionDetails>{gateway.last_seen_at ? fmtDateTime(gateway.last_seen_at) : "—"}</DescriptionDetails>
        </DescriptionList>
      </Card>
      <Tabs value={tab} onValueChange={(value) => setTab(value as "instances" | "tokens")}>
        <TabsList aria-label="Gateway sections" className="mb-4 max-w-full justify-start overflow-x-auto">
          <TabsTrigger value="instances" className="flex-none">
            Instances
          </TabsTrigger>
          <TabsTrigger value="tokens" className="flex-none">
            Tokens
          </TabsTrigger>
        </TabsList>
        <TabsContent value="instances">
          <InstancesPanel gateway={gateway} />
        </TabsContent>
        <TabsContent value="tokens">
          <TokensPanel gateway={gateway} />
        </TabsContent>
      </Tabs>
      <GatewayTokenDialog key={opened} open={creating} gatewayId={gateway.gateway_id} onClose={() => setCreating(false)} onCreated={() => setTab("tokens")} />
    </>
  );
}

function RateLimitsDetail({ gateway }: { gateway: Gateway }) {
  const summary = rateLimitsSummary(gateway);
  if (!summary) return <span className="text-muted-foreground">—</span>;
  const text = rateLimitsText(gateway);
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <Badge tone={summary.tone}>{summary.label}</Badge>
      {text && <span className="text-muted-foreground">{text}</span>}
    </span>
  );
}

function InstancesPanel({ gateway }: { gateway: Gateway }) {
  const odd = oddInstances(gateway.instances);
  // Connected first: the rows a rollout is about come before the retention window's disconnected ones.
  const rows = [...gateway.instances].sort(
    (a, b) => Number(b.connected) - Number(a.connected) || a.instance_id.localeCompare(b.instance_id),
  );
  return (
    <DataTable
      rows={rows}
      rowKey={(i) => i.instance_id}
      empty="No instances yet."
      columns={[
        {
          key: "instance",
          header: "Instance",
          cell: (i) => (
            <span
              className={cn("inline-flex flex-wrap items-center gap-1.5", !i.connected && "text-muted-foreground")}
              data-testid={`instance-${i.instance_id}`}
            >
              <code>{i.instance_id}</code>
              {odd.has(i.instance_id) && (
                <UiBadge variant="warn" data-testid={`odd-instance-${i.instance_id}`}>
                  Out of step
                </UiBadge>
              )}
            </span>
          ),
        },
        {
          key: "state",
          header: "State",
          cell: (i) => <Badge tone={i.connected ? "ok" : "muted"}>{i.connected ? "Connected" : "Disconnected"}</Badge>,
        },
        {
          key: "version",
          header: "Version",
          cell: (i) => <span className={cn(!i.connected && "text-muted-foreground")}>{i.gateway_version || "—"}</span>,
        },
        { key: "config", header: "Applied config", cell: (i) => <AppliedConfig instance={i} /> },
        {
          key: "connected",
          header: "Connected since",
          cell: (i) => <span className={cn(!i.connected && "text-muted-foreground")}>{i.connected_at ? fmtDateTime(i.connected_at) : "—"}</span>,
        },
        {
          key: "seen",
          header: "Last seen",
          cell: (i) => (
            <span className={cn(!i.connected && "text-muted-foreground")} data-testid={`last-seen-${i.instance_id}`}>
              {i.last_seen_at ? fmtDateTime(i.last_seen_at) : "—"}
            </span>
          ),
        },
        {
          key: "peer",
          header: "Peer",
          hideBelow: "md",
          cell: (i) => (
            <span className={cn(!i.connected && "text-muted-foreground")} data-testid={`peer-${i.instance_id}`}>
              {i.peer ?? "—"}
            </span>
          ),
        },
      ]}
    />
  );
}

function AppliedConfig({ instance }: { instance: Instance }) {
  return (
    <span className={cn("inline-flex flex-wrap items-center gap-1.5", !instance.connected && "text-muted-foreground")}>
      {instance.acked_version === null ? "—" : `v${instance.acked_version}`}
      {instance.pending_version !== null && <Badge tone="info">pending v{instance.pending_version}</Badge>}
      {instance.last_nack_version !== null && <Badge tone="danger">NACK v{instance.last_nack_version}</Badge>}
    </span>
  );
}

function TokensPanel({ gateway }: { gateway: Gateway }) {
  const [revoking, setRevoking] = useState<GwToken | null>(null);
  const revoke = useMut((token: GwToken) => api.revokeGatewayToken(gateway.gateway_id, token.id), "Token revoked");
  return (
    <>
      <DataTable
        rows={gateway.tokens}
        rowKey={(t) => t.id}
        empty="No tokens yet."
        columns={[
          {
            key: "label",
            header: "Label",
            sort: (a, b) => a.name.localeCompare(b.name),
            cell: (t) => (t.name ? <strong>{t.name}</strong> : <span className="text-muted-foreground">—</span>),
          },
          { key: "prefix", header: "Prefix", cell: (t) => <code>{t.display_prefix}…</code> },
          { key: "created", header: "Created", cell: (t) => fmtDateTime(t.created_at) },
          { key: "used", header: "Last used", cell: (t) => (t.last_used_at ? fmtDateTime(t.last_used_at) : "—") },
          {
            key: "status",
            header: "Status",
            cell: (t) =>
              t.revoked_at ? (
                <span className="inline-flex flex-wrap items-center gap-1.5">
                  <Badge>Revoked</Badge>
                  <span className="text-xs text-muted-foreground">{fmtDateTime(t.revoked_at)}</span>
                </span>
              ) : (
                <Badge tone="ok">Active</Badge>
              ),
          },
          {
            key: "actions",
            header: "",
            align: "right",
            cell: (t) =>
              t.revoked_at ? null : (
                <Button
                  variant="ghost"
                  size="sm"
                  className="text-destructive hover:bg-destructive/10 hover:text-destructive"
                  aria-label={`Revoke ${t.name || `${t.display_prefix}…`}`}
                  onClick={() => setRevoking(t)}
                >
                  Revoke
                </Button>
              ),
          },
        ]}
      />
      <ConfirmDialog
        open={!!revoking}
        title="Revoke gateway token"
        confirmLabel="Revoke"
        message="The gateway using this token can no longer connect. It keeps serving its last good config."
        onConfirm={() => revoke.mutateAsync(revoking!)}
        onClose={() => setRevoking(null)}
      />
    </>
  );
}
