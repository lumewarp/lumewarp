import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api";
import { Badge, fmtDateTime, PageHeader } from "../components/Bits";
import { DataTable } from "../components/Table";
import { Dialog } from "../components/Dialog";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { ApiError } from "../api";
import { useMut } from "../hooks";

/**
 * The control plane refuses a rollback when connected gateways lack a router's strategy, unless forced
 * (`compiler/strategy.py::check_router_strategy`): a 422 on field `strategy` whose message is
 * `strategy: [router '<name>': ]connected gateways lack '<name>' and would reject the whole config: <ids>; send force to save anyway`.
 * Anything else, including a `params` error that happens to contain "send force", is not forceable.
 * (A structured error code from the API would be sturdier than matching its text.)
 */
const MISSING_GATEWAYS = /^strategy: (?:router '.*': )?connected gateways lack .+ and would reject the whole config: .*; send force to save anyway$/s;
export const isMissingStrategy = (e: unknown): e is ApiError => e instanceof ApiError && e.status === 422 && e.field === "strategy" && MISSING_GATEWAYS.test(e.message);

/**
 * Confirm a rollback. A 422 about strategies gateways lack offers a second, explicit confirmation that
 * sends `force`; any other failure is shown here with its message.
 */
function RollbackDialog({ version, onClose }: { version: number | null; onClose: () => void }) {
  const [problem, setProblem] = useState<ApiError | Error | null>(null);
  const [force, setForce] = useState(false);
  const run = useMut(({ version, force }: { version: number; force: boolean }) => api.rollback(version, force), "Rolled back: a new version was published");
  const close = () => {
    setProblem(null);
    setForce(false);
    onClose();
  };
  const go = async (useForce: boolean) => {
    try {
      await run.mutateAsync({ version: version!, force: useForce });
      close();
    } catch (e) {
      setForce(isMissingStrategy(e));
      setProblem(e instanceof Error ? e : new Error(String(e)));
    }
  };
  return (
    <Dialog
      open={version !== null}
      onClose={close}
      title="Roll back routing"
      footer={
        <>
          <Button type="button" variant="outline" onClick={close} autoFocus>
            Cancel
          </Button>
          <Button type="button" variant={force ? "destructive" : "default"} disabled={run.isPending || (!!problem && !force)} onClick={() => void go(force)}>
            {run.isPending ? "Working…" : force ? "Roll back anyway" : "Roll back"}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3 text-sm">
        <p className="text-muted-foreground">Restore the routing (models, aliases and routers) from v{version} as a new version? Keys, budgets, policies and access grants are not rolled back.</p>
        {problem && (
          <Alert variant={force ? "warn" : "destructive"} data-testid="rollback-problem">
            <AlertDescription>
              <p>{problem.message}</p>
              {force && <p>Gateways that lack it will reject the whole config and stop receiving updates, including key revocations. Roll back anyway only if you are fixing those gateways.</p>}
            </AlertDescription>
          </Alert>
        )}
      </div>
    </Dialog>
  );
}

export function Config() {
  const list = useQuery({ queryKey: ["config-versions"], queryFn: () => api.configVersions() });
  const [target, setTarget] = useState<number | null>(null);
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
          { key: "digest", header: "Digest", cell: (r) => <code className="text-muted-foreground">{r.digest.slice(0, 12)}</code> },
          {
            key: "actions",
            header: "",
            cell: (r) => (
              <div className="flex justify-end gap-1.5 whitespace-nowrap">
                <Button variant="outline" size="sm" disabled={r.version === current || !!r.compacted_at} title={r.compacted_at ? "Compacted; payload no longer retained" : undefined} onClick={() => setTarget(r.version)}>
                  Roll back
                </Button>
              </div>
            ),
          },
        ]}
      />
      <RollbackDialog version={target} onClose={() => setTarget(null)} />
    </>
  );
}
