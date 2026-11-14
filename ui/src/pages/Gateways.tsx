import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";

import { api } from "../api";
import { Badge, fmtDateTime, PageHeader } from "../components/Bits";
import { FormDialog, required, TextField, useForm } from "../components/Form";
import { CreateButton } from "../components/RowActions";
import { useShowSecret } from "../components/SecretProvider";
import { DataTable } from "../components/Table";
import { Badge as UiBadge } from "../components/ui/badge";
import { useMut } from "../hooks";
import { runTokenSecretMutation } from "../lib/keySecret";

export type Gateway = Awaited<ReturnType<typeof api.gateways>>[number];
export type GwToken = Gateway["tokens"][number];
export type Instance = Gateway["instances"][number];

interface LimitsState {
  kind: "shared" | "per-instance" | "mixed" | "none";
  namespace: string;
}

/** How the connected instances of one gateway id share rate limits, and whether they disagree. */
export function limitsState(instances: Instance[]): LimitsState {
  const live = instances.filter((i) => i.connected);
  if (!live.length) return { kind: "none", namespace: "" };
  const on = live.filter((i) => i.shared_limits_enabled);
  if (!on.length) return { kind: "per-instance", namespace: "" };
  const spaces = new Set(on.map((i) => i.shared_limits_namespace));
  if (on.length === live.length && spaces.size === 1) return { kind: "shared", namespace: [...spaces][0] };
  return { kind: "mixed", namespace: [...spaces].map((s) => s || "no namespace reported").join(", ") };
}

/** The distinct build versions of the connected instances, sorted; empty while none is connected. */
export function liveVersions(instances: Instance[]): string[] {
  return [...new Set(instances.filter((i) => i.connected).map((i) => i.gateway_version).filter(Boolean))].sort();
}

function versionsDiffer(instances: Instance[]): boolean {
  return liveVersions(instances).length > 1;
}

/** The value most instances share, when it is a strict majority; null on a tie. */
function majority(values: string[]): string | null {
  const counts = new Map<string, number>();
  for (const value of values) counts.set(value, (counts.get(value) ?? 0) + 1);
  let winner: string | null = null;
  let count = 0;
  for (const [value, n] of counts) {
    if (n > count) {
      winner = value;
      count = n;
    }
  }
  return winner !== null && count * 2 > values.length ? winner : null;
}

const sharingKey = (instance: Instance) =>
  instance.shared_limits_enabled ? `shared:${instance.shared_limits_namespace}` : "per-instance";

/**
 * The connected instances whose build version or shared-limits setting differs from the majority. A tie has no
 * odd one out: during a rolling upgrade two versions may be equally common. Disconnected rows never count.
 */
export function oddInstances(instances: Instance[]): Set<string> {
  const live = instances.filter((i) => i.connected);
  // An instance that reported no build version is not counted, the same as in liveVersions.
  const version = majority(live.map((i) => i.gateway_version).filter(Boolean));
  const sharing = majority(live.map(sharingKey));
  const odd = new Set<string>();
  for (const instance of live) {
    if ((version !== null && !!instance.gateway_version && instance.gateway_version !== version) || (sharing !== null && sharingKey(instance) !== sharing)) {
      odd.add(instance.instance_id);
    }
  }
  return odd;
}

export interface ConfigStatus {
  tone: "danger" | "info" | "warn";
  text: string;
  title?: string;
}

/** The one badge the list shows for a gateway's config, by priority: quarantined > NACK > pending > behind. */
export function configStatus(g: Gateway, latest: number | undefined): ConfigStatus | null {
  if (g.quarantined_version !== null) return { tone: "danger", text: `quarantined v${g.quarantined_version}` };
  if (g.last_nack_version !== null) {
    const detail = [g.last_nack_code, g.last_nack_reason].filter(Boolean).join(": ");
    return { tone: "danger", text: `NACK v${g.last_nack_version}`, ...(detail ? { title: detail } : {}) };
  }
  if (g.pending_version !== null) return { tone: "info", text: `pending v${g.pending_version}` };
  if (g.acked_version !== null && latest !== undefined && g.acked_version < latest) {
    return { tone: "warn", text: `behind v${latest}` };
  }
  return null;
}

/** Whether the list's Health column should flag this gateway. The detail page says what is wrong. */
export function needsAttention(g: Gateway): boolean {
  return (
    versionsDiffer(g.instances) ||
    limitsState(g.instances).kind === "mixed" ||
    g.last_nack_version !== null ||
    g.quarantined_version !== null
  );
}

/** The problems the detail page spells out, one sentence each; empty when the fleet agrees and is healthy. */
export function gatewayWarnings(g: Gateway): string[] {
  const warnings: string[] = [];
  const versions = liveVersions(g.instances);
  if (versions.length > 1) warnings.push(`Versions differ: ${versions.join(", ")}.`);
  const limits = limitsState(g.instances);
  if (limits.kind === "mixed") {
    warnings.push(`Instances disagree on shared limits${limits.namespace ? ` (${limits.namespace})` : ""}.`);
  }
  const odd = [...oddInstances(g.instances)].sort();
  if (odd.length) {
    warnings.push(`Out of step with most connected instances (version or shared limits): ${odd.join(", ")}.`);
  }
  if (g.last_nack_version !== null) {
    const code = g.last_nack_code ? ` (${g.last_nack_code})` : "";
    const reason = g.last_nack_reason?.trim().replace(/\.+$/, "");
    warnings.push(`Config v${g.last_nack_version} was NACKed${code}${reason ? `: ${reason}` : ""}.`);
  }
  if (g.quarantined_version !== null) {
    warnings.push(`Config v${g.quarantined_version} is quarantined for this gateway id.`);
  }
  return warnings;
}

/** The short label of the list's Rate limits column; the full explanation is on the detail page. */
export function rateLimitsSummary(g: Gateway): { tone: "ok" | "muted" | "warn"; label: string } | null {
  const state = limitsState(g.instances);
  if (state.kind === "none") return null;
  if (state.kind === "shared") return { tone: "ok", label: state.namespace ? `Shared (${state.namespace})` : "Shared" };
  if (state.kind === "per-instance") return { tone: "muted", label: "Per instance" };
  return { tone: "warn", label: "Mixed" };
}

/** The sentence behind a Shared or Mixed badge on the detail page; null when the badge says it all. */
export function rateLimitsText(g: Gateway): string | null {
  const state = limitsState(g.instances);
  if (state.kind === "shared") {
    return state.namespace ? `Connected instances share their counters in namespace ${state.namespace}.` : "Connected instances share their counters; no namespace is reported.";
  }
  if (state.kind === "mixed") {
    const live = g.instances.filter((i) => i.connected);
    const sharing = live.filter((i) => i.shared_limits_enabled);
    if (sharing.length < live.length) return "Some connected instances share their counters and others count their own.";
    const namespaces = [...new Set(sharing.map((i) => i.shared_limits_namespace || "no namespace reported"))];
    return `Connected instances share their counters under different namespaces (${namespaces.join(", ")}).`;
  }
  return null;
}

function GatewayIdentity({ g }: { g: Gateway }) {
  return (
    <div className="flex min-w-0 flex-col">
      <Link to="/gateways/$gatewayId" params={{ gatewayId: g.gateway_id }} data-testid={`gateway-link-${g.gateway_id}`} className="w-fit">
        <strong className="[overflow-wrap:anywhere]">{g.gateway_id}</strong>
      </Link>
      {g.gateway_version && <span className="text-xs text-muted-foreground [overflow-wrap:anywhere]">{g.gateway_version}</span>}
    </div>
  );
}

function InstancesCount({ g }: { g: Gateway }) {
  if (!g.instances.length) {
    return (
      <span className="text-muted-foreground" data-testid={`instance-count-${g.gateway_id}`}>
        No instances yet
      </span>
    );
  }
  const live = g.instances.filter((i) => i.connected).length;
  return (
    <UiBadge variant={live === g.instances.length ? "ok" : "warn"} data-testid={`instance-count-${g.gateway_id}`}>
      {live}/{g.instances.length} instances connected
    </UiBadge>
  );
}

function RateLimits({ g }: { g: Gateway }) {
  const summary = rateLimitsSummary(g);
  if (!summary) return <span className="text-muted-foreground">—</span>;
  return <Badge tone={summary.tone}>{summary.label}</Badge>;
}

function ConfigStatus({ g, latest }: { g: Gateway; latest: number | undefined }) {
  const status = configStatus(g, latest);
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      {g.acked_version === null ? <span className="text-muted-foreground">—</span> : `v${g.acked_version}`}
      {status && (
        <UiBadge variant={status.tone} title={status.title}>
          {status.text}
        </UiBadge>
      )}
    </span>
  );
}

/**
 * The create-token dialog, shared by the list and a gateway's page: fixed id, or typed on the list. Callers give
 * it a new `key` on every opening, so each one starts from an empty form without a leftover error.
 */
export function GatewayTokenDialog({ open, gatewayId, onClose, onCreated }: { open: boolean; gatewayId?: string; onClose: () => void; onCreated?: () => void }) {
  const showSecret = useShowSecret();
  const form = useForm({ gateway_id: gatewayId ?? "", name: "" }, { gateway_id: required("Gateway id") });
  const create = useMut((values: { gateway_id: string; name: string }) =>
    runTokenSecretMutation(() => api.createGatewayToken(values), (value) => showSecret({ value, title: "Gateway token created" })),
  );

  const submit = async () => {
    if (!form.validate()) return false;
    if (gatewayId) {
      // A server error must stay visible: there is no editable field for it to attach to.
      await create.mutateAsync({ gateway_id: gatewayId, name: form.values.name });
      onCreated?.();
      return;
    }
    return form.run(async () => {
      await create.mutateAsync(form.values);
      onCreated?.();
    });
  };

  return (
    <FormDialog open={open} title="New gateway token" submitLabel="Create token" onClose={onClose} onSubmit={submit}>
      {gatewayId ? (
        <div className="flex flex-col gap-1">
          <span className="text-sm font-semibold">Gateway id</span>
          <code className="text-muted-foreground [overflow-wrap:anywhere]">{gatewayId}</code>
        </div>
      ) : (
        <TextField
          label="Gateway id"
          value={form.values.gateway_id}
          onChange={(value) => form.set("gateway_id", value)}
          error={form.errors.gateway_id}
          autoFocus
        />
      )}
      <TextField
        label="Label"
        value={form.values.name}
        onChange={(value) => form.set("name", value)}
        autoFocus={!!gatewayId}
      />
    </FormDialog>
  );
}

export function Gateways() {
  const list = useQuery({ queryKey: ["gateways"], queryFn: () => api.gateways(), refetchInterval: 10_000 });
  const versions = useQuery({ queryKey: ["config-versions"], queryFn: () => api.configVersions() });
  const latest = versions.data?.reduce((m, v) => Math.max(m, v.version), 0);
  const [creating, setCreating] = useState(false);
  const [opened, setOpened] = useState(0);

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
      <DataTable
        rows={list.data}
        loading={list.isPending}
        error={list.error}
        onRetry={() => void list.refetch()}
        rowKey={(r) => r.gateway_id}
        empty="No gateways have registered yet. Create a token to connect one."
        columns={[
          { key: "id", header: "Gateway", sort: (a, b) => a.gateway_id.localeCompare(b.gateway_id), cell: (r) => <GatewayIdentity g={r} /> },
          { key: "instances", header: "Instances", cell: (r) => <InstancesCount g={r} /> },
          { key: "config", header: "Config", hideBelow: "sm", cell: (r) => <ConfigStatus g={r} latest={latest} /> },
          { key: "limits", header: "Rate limits", hideBelow: "md", cell: (r) => <RateLimits g={r} /> },
          { key: "seen", header: "Last seen", hideBelow: "lg", cell: (r) => (r.last_seen_at ? fmtDateTime(r.last_seen_at) : "—") },
          {
            key: "health",
            header: "Health",
            hideBelow: "md",
            cell: (r) => (needsAttention(r) ? <Badge tone="warn">Needs attention</Badge> : null),
          },
        ]}
      />
      <GatewayTokenDialog key={opened} open={creating} onClose={() => setCreating(false)} />
    </>
  );
}
