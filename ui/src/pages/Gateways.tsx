import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, type Gateway, type GatewayToken } from "../api";
import { Badge, fmtDateTime, PageHeader } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { FormDialog, TextField, useForm } from "../components/Form";
import { CreateButton } from "../components/RowActions";
import { useShowSecret } from "../components/SecretProvider";
import { DataTable } from "../components/Table";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../components/ui/tabs";
import { useMut } from "../hooks";
import { runTokenSecretMutation } from "../lib/keySecret";

type Tab = "gateways" | "tokens";
type BadgeTone = "danger" | "info" | "warn";

export interface ConfigBadge {
  tone: BadgeTone;
  text: string;
}

/**
 * The quarantined version still withheld: only the newest version is ever sent, so a quarantine
 * superseded by a newer version no longer matters.
 */
export function quarantinedVersions(versions: { version: number; quarantined: boolean }[] | undefined): Set<number> {
  const newest = (versions ?? []).reduce<{ version: number; quarantined: boolean } | undefined>(
    (max, v) => (max === undefined || v.version > max.version ? v : max),
    undefined,
  );
  return new Set(newest?.quarantined ? [newest.version] : []);
}

/**
 * The one badge a row shows for its config, by priority: quarantined > NACK > pending > behind.
 * A gateway is quarantined when the version it serves or last rejected is the scope's quarantined one.
 */
export function configBadge(g: Gateway, latest: number | undefined, quarantined: Set<number>): ConfigBadge | null {
  const quarantinedVersion = [g.last_nack_version, g.acked_version].find(
    (v): v is number => v !== null && quarantined.has(v),
  );
  if (quarantinedVersion !== undefined) return { tone: "danger", text: `quarantined v${quarantinedVersion}` };
  if (g.last_nack_version !== null) return { tone: "danger", text: `NACK v${g.last_nack_version}` };
  if (g.pending_version !== null) return { tone: "info", text: `pending v${g.pending_version}` };
  if (g.acked_version !== null && latest !== undefined && g.acked_version < latest) {
    return { tone: "warn", text: `behind v${latest}` };
  }
  return null;
}

function sharingLabel(g: Gateway): string {
  if (!g.shared_limits_enabled) return "per gateway";
  return g.shared_limits_namespace || "no namespace reported";
}

/**
 * The problems the fleet has right now, one sentence each: connected gateways on different builds
 * or shared-limits settings, each connected gateway's NACK, and the scope's quarantined version.
 * Disconnected rows are history and never raise a warning.
 */
export function gatewayWarnings(rows: Gateway[], quarantined: Set<number>): string[] {
  const live = rows.filter((g) => g.connected).sort((a, b) => a.gateway_id.localeCompare(b.gateway_id));
  const warnings: string[] = [];
  const versions = [...new Set(live.map((g) => g.gateway_version).filter(Boolean))];
  if (versions.length > 1) {
    const described = live
      .filter((g) => g.gateway_version)
      .map((g) => `${g.gateway_id} (${g.gateway_version})`)
      .join(", ");
    warnings.push(`Connected gateways run different versions: ${described}.`);
  }
  const sharing = [...new Set(live.map(sharingLabel))];
  if (sharing.length > 1) {
    const described = live.map((g) => `${g.gateway_id} (${sharingLabel(g)})`).join(", ");
    warnings.push(`Connected gateways disagree on shared limits: ${described}.`);
  }
  for (const g of live) {
    if (g.last_nack_version === null) continue;
    const code = g.last_nack_code ? ` (${g.last_nack_code})` : "";
    const reason = g.last_nack_reason?.trim().replace(/\.+$/, "");
    warnings.push(`${g.gateway_id} NACKed config v${g.last_nack_version}${code}${reason ? `: ${reason}` : ""}.`);
  }
  for (const version of [...quarantined].sort((a, b) => a - b)) {
    warnings.push(`Config v${version} is quarantined and withheld from every gateway in this scope.`);
  }
  return warnings;
}

function GatewayId({ g }: { g: Gateway }) {
  return (
    <span className="whitespace-nowrap" data-testid={`gateway-${g.gateway_id}`}>
      <code>{g.gateway_id}</code>
    </span>
  );
}

function RateLimits({ g }: { g: Gateway }) {
  if (!g.shared_limits_enabled) return <Badge tone="muted">Per gateway</Badge>;
  return <Badge tone="ok">{g.shared_limits_namespace ? `Shared (${g.shared_limits_namespace})` : "Shared"}</Badge>;
}

function TokenName({ g }: { g: Gateway }) {
  return g.token.name ? (
    <span className="whitespace-nowrap">{g.token.name}</span>
  ) : (
    <code className="whitespace-nowrap text-muted-foreground">{g.token.display_prefix}…</code>
  );
}

function Config({ g, latest, quarantined }: { g: Gateway; latest: number | undefined; quarantined: Set<number> }) {
  const badge = configBadge(g, latest, quarantined);
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5" data-testid={`config-${g.gateway_id}`}>
      {g.acked_version === null ? <span className="text-muted-foreground">—</span> : `v${g.acked_version}`}
      {badge && <Badge tone={badge.tone}>{badge.text}</Badge>}
    </span>
  );
}

function GatewayTokenDialog({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: () => void }) {
  const showSecret = useShowSecret();
  const form = useForm({ name: "" });
  const create = useMut(() =>
    runTokenSecretMutation(
      () => api.createGatewayToken({ name: form.values.name }),
      (value) => showSecret({ value, title: "Gateway token created" }),
    ),
  );
  return (
    <FormDialog
      open={open}
      title="New gateway token"
      submitLabel="Create token"
      onClose={onClose}
      onSubmit={() =>
        form.run(async () => {
          await create.mutateAsync();
          onCreated();
        })
      }
    >
      <TextField label="Label" value={form.values.name} onChange={(value) => form.set("name", value)} autoFocus />
    </FormDialog>
  );
}

function TokensTab({ rows, loading, error, onRetry }: { rows: GatewayToken[] | undefined; loading: boolean; error: Error | null; onRetry: () => void }) {
  const [revoking, setRevoking] = useState<GatewayToken | null>(null);
  const revoke = useMut((token: GatewayToken) => api.revokeGatewayToken(token.id), "Gateway token revoked");
  const connected = revoking?.connected_gateways ?? 0;
  const message = `Disconnects ${connected} connected gateway${connected === 1 ? "" : "s"}; ${connected === 1 ? "it keeps" : "they keep"} serving ${connected === 1 ? "its" : "their"} last good config.`;
  return (
    <>
      <DataTable
        rows={rows}
        loading={loading}
        error={error}
        onRetry={onRetry}
        rowKey={(t) => t.id}
        empty="No gateway tokens yet."
        columns={[
          {
            key: "label",
            header: "Label",
            sort: (a, b) => a.name.localeCompare(b.name),
            cell: (t) => (t.name ? <strong className="[overflow-wrap:anywhere]">{t.name}</strong> : <span className="text-muted-foreground">—</span>),
          },
          { key: "prefix", header: "Prefix", cell: (t) => <code>{t.display_prefix}…</code> },
          { key: "created", header: "Created", cell: (t) => fmtDateTime(t.created_at) },
          { key: "used", header: "Last used", cell: (t) => (t.last_used_at ? fmtDateTime(t.last_used_at) : "—") },
          {
            key: "gateways",
            header: "Gateways",
            cell: (t) => <span data-testid={`token-gateways-${t.id}`}>{t.connected_gateways}</span>,
          },
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
                  type="button"
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
        message={message}
        onConfirm={() => revoke.mutateAsync(revoking!)}
        onClose={() => setRevoking(null)}
      />
    </>
  );
}

export function Gateways() {
  const gateways = useQuery({ queryKey: ["gateways"], queryFn: () => api.gateways(), refetchInterval: 10_000 });
  const versions = useQuery({ queryKey: ["config-versions"], queryFn: () => api.configVersions() });
  const tokens = useQuery({ queryKey: ["gateway-tokens"], queryFn: () => api.gatewayTokens(), refetchInterval: 10_000 });
  const [tab, setTab] = useState<Tab>("gateways");
  const [creating, setCreating] = useState(false);
  const [opened, setOpened] = useState(0);

  const latest = versions.data?.reduce((max, v) => Math.max(max, v.version), 0);
  const quarantined = quarantinedVersions(versions.data);
  const rows = gateways.data
    ? [...gateways.data].sort((a, b) => Number(b.connected) - Number(a.connected) || a.gateway_id.localeCompare(b.gateway_id))
    : undefined;
  const warnings = gatewayWarnings(gateways.data ?? [], quarantined);

  return (
    <>
      <PageHeader
        title="Gateways"
        actions={
          <CreateButton
            onClick={() => {
              setOpened((n) => n + 1);
              setCreating(true);
            }}
          >
            New gateway token
          </CreateButton>
        }
      />
      <Tabs value={tab} onValueChange={(value) => setTab(value as Tab)}>
        <TabsList aria-label="Gateway sections" className="mb-4 max-w-full justify-start overflow-x-auto">
          <TabsTrigger value="gateways" className="flex-none">
            Gateways
          </TabsTrigger>
          <TabsTrigger value="tokens" className="flex-none">
            Tokens
          </TabsTrigger>
        </TabsList>
        <TabsContent value="gateways">
          {warnings.length > 0 && (
            <Alert variant="warn" className="mb-4" data-testid="gateway-warnings">
              <AlertDescription>
                <ul className="m-0 flex list-disc flex-col gap-1 pl-4">
                  {warnings.map((warning) => (
                    <li key={warning}>{warning}</li>
                  ))}
                </ul>
              </AlertDescription>
            </Alert>
          )}
          <DataTable
            rows={rows}
            loading={gateways.isPending}
            error={gateways.error}
            onRetry={() => {
              void gateways.refetch();
              void versions.refetch();
            }}
            rowKey={(r) => r.gateway_id}
            mutedRow={(r) => !r.connected}
            empty="No gateways have connected yet. Create a token to connect one."
            columns={[
              { key: "id", header: "Gateway", sort: (a, b) => a.gateway_id.localeCompare(b.gateway_id), cell: (r) => <GatewayId g={r} /> },
              {
                key: "state",
                header: "State",
                cell: (r) => <Badge tone={r.connected ? "ok" : "muted"}>{r.connected ? "Connected" : "Disconnected"}</Badge>,
              },
              { key: "version", header: "Version", cell: (r) => (r.gateway_version ? <code className="whitespace-nowrap">{r.gateway_version}</code> : <span className="text-muted-foreground">—</span>) },
              { key: "config", header: "Config", cell: (r) => <Config g={r} latest={latest} quarantined={quarantined} /> },
              { key: "limits", header: "Rate limits", hideBelow: "md", cell: (r) => <RateLimits g={r} /> },
              { key: "token", header: "Token", hideBelow: "md", cell: (r) => <TokenName g={r} /> },
              { key: "seen", header: "Last seen", hideBelow: "lg", cell: (r) => <span className="whitespace-nowrap">{r.last_seen_at ? fmtDateTime(r.last_seen_at) : "—"}</span> },
              { key: "peer", header: "Peer", hideBelow: "lg", cell: (r) => (r.peer ? <code className="[overflow-wrap:anywhere]">{r.peer}</code> : "—") },
            ]}
          />
        </TabsContent>
        <TabsContent value="tokens">
          <TokensTab rows={tokens.data} loading={tokens.isPending} error={tokens.error} onRetry={() => void tokens.refetch()} />
        </TabsContent>
      </Tabs>
      <GatewayTokenDialog
        key={opened}
        open={creating}
        onClose={() => setCreating(false)}
        onCreated={() => setTab("tokens")}
      />
    </>
  );
}
