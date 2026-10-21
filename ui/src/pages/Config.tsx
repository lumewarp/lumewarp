import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api";
import { Badge, fmtDateTime, PageHeader } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { DataTable } from "../components/Table";
import { useMut } from "../hooks";

export function Config() {
  const list = useQuery({ queryKey: ["config-versions"], queryFn: () => api.configVersions() });
  const [target, setTarget] = useState<number | null>(null);
  const rollback = useMut((v: number) => api.rollback(v), "Rolled back: a new version was published");
  const current = list.data?.reduce((m, v) => Math.max(m, v.version), 0);
  return (
    <>
      <PageHeader title="Config versions" description="Immutable snapshots pushed to gateways. Rolling back restores routing only; keys, budgets and policies stay current." />
      <DataTable
        rows={list.data}
        loading={list.isPending}
        error={list.error}
        onRetry={() => void list.refetch()}
        rowKey={(r) => String(r.version)}
        empty="No versions published yet."
        columns={[
          { key: "version", header: "Version", sort: (a, b) => a.version - b.version, cell: (r) => (<><strong>v{r.version}</strong> {r.version === current && <Badge tone="ok">current</Badge>}</>) },
          { key: "kind", header: "Kind", cell: (r) => (r.rollback_of_version === null ? r.kind : `${r.kind} of v${r.rollback_of_version}`) },
          { key: "created", header: "Created", sort: (a, b) => a.created_at.localeCompare(b.created_at), cell: (r) => fmtDateTime(r.created_at) },
          { key: "keys", header: "Keys", align: "right", cell: (r) => r.key_count.toLocaleString() },
          { key: "size", header: "Size", align: "right", cell: (r) => `${(r.size_bytes / 1024).toFixed(1)} KiB` },
          { key: "digest", header: "Digest", cell: (r) => <code className="muted">{r.digest.slice(0, 12)}</code> },
          {
            key: "actions",
            header: "",
            cell: (r) => (
              <div className="row-actions">
                <button className="btn btn-sm" disabled={r.version === current || !!r.compacted_at} title={r.compacted_at ? "Compacted; payload no longer retained" : undefined} onClick={() => setTarget(r.version)}>
                  Roll back
                </button>
              </div>
            ),
          },
        ]}
      />
      <ConfirmDialog open={target !== null} title="Roll back routing" confirmLabel="Roll back" message={`Restore the routing from v${target} as a new version? Keys, budgets and policies are not rolled back.`} onConfirm={() => rollback.mutateAsync(target!)} onClose={() => setTarget(null)} />
    </>
  );
}
