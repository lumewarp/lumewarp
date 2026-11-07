import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { useState } from "react";

import { api, type Upstream } from "../api";
import { Badge, PageHeader } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { CreateButton, RowActions } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { useCan, useMut } from "../hooks";

export const KINDS = [
  { value: "vllm", label: "vLLM" },
  { value: "openai", label: "OpenAI" },
  { value: "openai_compatible", label: "OpenAI-compatible" },
];

type HealthState = Upstream["health"];

const HEALTH: Record<HealthState, { tone: "ok" | "danger" | "muted"; label: string; title: string }> = {
  healthy: { tone: "ok", label: "Healthy", title: "Every connected gateway that checks it reports it healthy." },
  unhealthy: { tone: "danger", label: "Unhealthy", title: "At least one connected gateway reports it unhealthy." },
  unknown: { tone: "muted", label: "Unknown", title: "No connected gateway has reported it yet." },
  not_probed: { tone: "muted", label: "Not probed", title: "No periodic check is configured." },
};

/** An upstream's probe or a model's chat check, as the connected gateways report it. */
export function HealthBadge({ health }: { health: HealthState }) {
  const h = HEALTH[health];
  return (
    <span title={h.title}>
      <Badge tone={h.tone}>{h.label}</Badge>
    </span>
  );
}

export function Upstreams() {
  const perms = useCan();
  const navigate = useNavigate();
  // Health comes from gateway reports; refresh it while the page is open.
  const list = useQuery({ queryKey: ["upstreams"], queryFn: () => api.upstreams(), refetchInterval: 15000 });
  const [deleting, setDeleting] = useState<Upstream | null>(null);
  const remove = useMut((id: string) => api.deleteUpstream(id), "Upstream deleted");
  const edit = (u: Upstream) => void navigate({ to: "/upstreams/$upstreamId", params: { upstreamId: u.id } });

  return (
    <>
      <PageHeader title="Upstreams" actions={<CreateButton can={perms.isInstallationAdmin} onClick={() => void navigate({ to: "/upstreams/new" })}>New upstream</CreateButton>} />
      <DataTable
        rows={list.data}
        loading={list.isPending}
        error={list.error}
        onRetry={() => void list.refetch()}
        rowKey={(r) => r.id}
        empty="No upstreams yet."
        columns={[
          {
            key: "name",
            header: "Name",
            sort: (a, b) => a.name.localeCompare(b.name),
            cell: (r) => (
              <Link to="/upstreams/$upstreamId" params={{ upstreamId: r.id }} className="font-semibold hover:underline">
                {r.name}
              </Link>
            ),
          },
          { key: "kind", header: "Kind", cell: (r) => <Badge tone="info">{KINDS.find((k) => k.value === r.kind)?.label ?? r.kind}</Badge> },
          { key: "url", header: "Base URL", cell: (r) => <code>{r.base_url}</code> },
          { key: "health", header: "Health", cell: (r) => <HealthBadge health={r.health} /> },
          { key: "ref", header: "API key", hideBelow: "lg", cell: (r) => (r.api_key_set ? <Badge tone="info">stored</Badge> : r.api_key_ref ? <code>${r.api_key_ref}</code> : <span className="text-muted-foreground">none</span>) },
          { key: "actions", header: "", cell: (r) => <RowActions canEdit={perms.isInstallationAdmin} canDelete={perms.isInstallationAdmin} onEdit={() => edit(r)} onDelete={() => setDeleting(r)} /> },
        ]}
      />
      <ConfirmDialog open={!!deleting} title="Delete upstream" message={`Delete “${deleting?.name}”? It must not be used by any model.`} onConfirm={() => remove.mutateAsync(deleting!.id)} onClose={() => setDeleting(null)} />
    </>
  );
}
