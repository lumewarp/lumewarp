import { useMutation, useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { ActivityIcon } from "lucide-react";
import { useMemo, useState } from "react";

import { api, type Model } from "../api";
import { fmtTokens, fmtUsd, PageHeader } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { CreateButton, IconAction, RowActions } from "../components/RowActions";
import { SearchBox } from "../components/Search";
import { useToast } from "../components/Toast";
import { DataTable } from "../components/Table";
import { useCan, useMut, useNames, useUpstreams } from "../hooks";
import { matches } from "../lib/catalog";
import { HealthBadge } from "./Upstreams";

export function Models() {
  const perms = useCan();
  const navigate = useNavigate();
  // Health comes from gateway reports; refresh it while the page is open.
  const list = useQuery({ queryKey: ["models"], queryFn: () => api.models(), refetchInterval: 15000 });
  const upstreams = useUpstreams();
  const upstreamName = useNames(upstreams.data);
  const [search, setSearch] = useState("");
  const [deleting, setDeleting] = useState<Model | null>(null);
  const remove = useMut((id: string) => api.deleteModel(id), "Model deleted");
  const mapped = (m: Model) => `${upstreamName(m.upstream_id)}/${m.upstream_model}`;

  const rows = useMemo(
    () => (list.data ?? []).filter((m) => matches(search, m.name, m.upstream_model)),
    [list.data, search],
  );

  return (
    <>
      <PageHeader title="Models" actions={<CreateButton can={perms.isInstallationAdmin} onClick={() => void navigate({ to: "/models/new" })}>New model</CreateButton>} />
      <SearchBox value={search} onChange={setSearch} label="Search models" placeholder="Name or upstream model" />
      <DataTable
        rows={rows}
        loading={list.isPending || upstreams.isPending}
        error={list.error ?? upstreams.error}
        onRetry={() => {
          void list.refetch();
          void upstreams.refetch();
        }}
        rowKey={(r) => r.id}
        empty={search ? "No models match your search." : "No models yet."}
        columns={[
          {
            key: "name",
            header: "Name",
            sort: (a, b) => a.name.localeCompare(b.name),
            cell: (r) => (
              <Link to="/models/$modelId" params={{ modelId: r.id }} className="font-semibold hover:underline">
                {r.name}
              </Link>
            ),
          },
          { key: "maps", header: "Mapped name", sort: (a, b) => mapped(a).localeCompare(mapped(b)), cell: (r) => <code className="text-muted-foreground">{mapped(r)}</code> },
          { key: "health", header: "Health", cell: (r) => <HealthBadge health={r.health} /> },
          { key: "length", header: "Input / Output length", align: "right", hideBelow: "lg", cell: (r) => <span title={`${r.max_input_tokens ?? "—"} / ${r.max_output_tokens ?? "—"} tokens`}>{`${fmtTokens(r.max_input_tokens)} / ${fmtTokens(r.max_output_tokens)}`}</span> },
          { key: "cost", header: "Input / Output cost (1M tok)", align: "right", hideBelow: "lg", cell: (r) => (r.price_input_nano_usd_per_mtok === null ? <span className="text-muted-foreground">unpriced</span> : `${fmtUsd(r.price_input_nano_usd_per_mtok)} / ${fmtUsd(r.price_output_nano_usd_per_mtok)}`) },
          { key: "actions", header: "", cell: (r) => <RowActions extra={perms.isInstallationAdmin && <TestModelAction model={r} />} canEdit={perms.isInstallationAdmin} canDelete={perms.isInstallationAdmin} onEdit={() => void navigate({ to: "/models/$modelId", params: { modelId: r.id } })} onDelete={() => setDeleting(r)} /> },
        ]}
      />
      <ConfirmDialog open={!!deleting} title="Delete model" message={`Delete “${deleting?.name}”? It must not be used by any router, fallback or alias.`} onConfirm={() => remove.mutateAsync(deleting!.id)} onClose={() => setDeleting(null)} />
    </>
  );
}

/** Sends a short chat completion to the saved model from the control plane and reports in a toast. */
function TestModelAction({ model }: { model: Model }) {
  const toast = useToast();
  const run = useMutation({
    mutationFn: () => api.testModel({ upstream_id: model.upstream_id, upstream_model: model.upstream_model, default_body: model.default_body ?? null }),
    onSuccess: (r) => {
      const { probe, reply } = r;
      if (probe.ok) toast("success", `${model.name}: OK · HTTP ${probe.status} · ${probe.latency_ms} ms${reply ? ` · ${reply}` : ""}`);
      else toast("error", `${model.name}: ${probe.status !== null ? `HTTP ${probe.status}` : (probe.error ?? "Failed")} · ${probe.latency_ms} ms`);
    },
    onError: (e: Error) => toast("error", `${model.name}: ${e.message}`),
  });
  return <IconAction label="Test" icon={ActivityIcon} busy={run.isPending} onClick={() => run.mutate()} />;
}
