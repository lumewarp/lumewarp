import { useQuery } from "@tanstack/react-query";
import { Trash2Icon } from "lucide-react";
import { useState } from "react";

import { api, type Gateway } from "../api";
import { Badge, fmtDateTime, PageHeader } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { IconAction } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { Alert, AlertDescription } from "../components/ui/alert";
import { useMut } from "../hooks";
import { useZone } from "../zone";

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
 * A gateway is quarantined when the version it serves or last rejected is the zone's quarantined one.
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
 * or shared-limits settings, each connected gateway's NACK, and the zone's quarantined version.
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
    warnings.push(`Config v${version} is quarantined and withheld from every gateway in this zone.`);
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

function Config({ g, latest, quarantined }: { g: Gateway; latest: number | undefined; quarantined: Set<number> }) {
  const badge = configBadge(g, latest, quarantined);
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5" data-testid={`config-${g.gateway_id}`}>
      {g.acked_version === null ? <span className="text-muted-foreground">—</span> : `v${g.acked_version}`}
      {badge && <Badge tone={badge.tone}>{badge.text}</Badge>}
    </span>
  );
}

export function Gateways() {
  const { id: zone } = useZone();
  const gateways = useQuery({ queryKey: ["gateways", zone], queryFn: () => api.gateways(zone), refetchInterval: 10_000 });
  const versions = useQuery({ queryKey: ["config-versions", zone], queryFn: () => api.configVersions(zone) });
  const [deleting, setDeleting] = useState<Gateway | null>(null);
  const remove = useMut((g: Gateway) => api.deleteGateway(zone, g.gateway_id), "Gateway deleted");

  const latest = versions.data?.reduce((max, v) => Math.max(max, v.version), 0);
  const quarantined = quarantinedVersions(versions.data);
  const rows = gateways.data
    ? [...gateways.data].sort((a, b) => Number(b.connected) - Number(a.connected) || a.gateway_id.localeCompare(b.gateway_id))
    : undefined;
  const warnings = gatewayWarnings(gateways.data ?? [], quarantined);

  return (
    <>
      <PageHeader title="Gateways" hint="A gateway authenticates with the zone's token and gets a new ID each time it connects; a disconnected row is deleted after 24 hours." />
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
        empty="No gateways have connected yet."
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
          { key: "seen", header: "Last seen", hideBelow: "lg", cell: (r) => <span className="whitespace-nowrap">{r.last_seen_at ? fmtDateTime(r.last_seen_at) : "—"}</span> },
          { key: "peer", header: "Peer", hideBelow: "lg", cell: (r) => (r.peer ? <code className="[overflow-wrap:anywhere]">{r.peer}</code> : "—") },
          {
            key: "actions",
            header: "",
            align: "right",
            cell: (r) =>
              r.connected ? null : (
                <div className="flex justify-end">
                  <IconAction
                    label={`Delete ${r.gateway_id}`}
                    icon={Trash2Icon}
                    className="text-destructive hover:bg-destructive/10 hover:text-destructive"
                    onClick={() => setDeleting(r)}
                  />
                </div>
              ),
          },
        ]}
      />
      <ConfirmDialog
        open={!!deleting}
        title="Delete gateway"
        message={`Delete the disconnected gateway “${deleting?.gateway_id}”? Its row and plugin report are removed; usage already recorded stays.`}
        onConfirm={() => remove.mutateAsync(deleting!)}
        onClose={() => setDeleting(null)}
      />
    </>
  );
}
