import { Link, useNavigate } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import { api, type Plugin, type Router } from "../api";
import { Badge, PageHeader, TypeBadge } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { CreateButton, RowActions } from "../components/RowActions";
import { SearchBox } from "../components/Search";
import { DataTable } from "../components/Table";
import { useAliases, useById, useCan, useModels, useMut, useNames, useRouters, useStrategies } from "../hooks";
import { matches, routerUpstreamModels } from "../lib/catalog";
import { typeLabel } from "../lib/modelType";
import { settingTitles, strategyTitle } from "../lib/schema";

/** Share of each split as a percentage string ("90%", "33.3%"). */
export function splitShares(weights: number[]): string[] {
  const total = weights.reduce((a, w) => a + Math.max(0, w), 0);
  return weights.map((w) => {
    if (!total) return "—";
    const pct = (Math.max(0, w) / total) * 100;
    return `${Number.isInteger(pct) ? pct : pct.toFixed(1)}%`;
  });
}

function memberSummary(m: { model_id: string; weight: number; params: Record<string, unknown> }, name: (id: string) => string, strategy: Plugin | undefined) {
  const extras = Object.entries(m.params)
    .map(([k, v]) => `${k} ${typeof v === "object" ? JSON.stringify(v) : String(v)}`)
    .join(", ");
  return (
    <div key={m.model_id} className="flex flex-wrap items-baseline gap-x-2">
      <code>{name(m.model_id)}</code>
      {(strategy?.uses_weight ?? true) && m.weight !== 1 && <span className="text-muted-foreground">×{m.weight}</span>}
      {extras && <span className="text-xs text-muted-foreground">{extras}</span>}
    </div>
  );
}

function Health({ router, strategy }: { router: Router; strategy: Plugin | undefined }) {
  if (router.config_invalid) {
    return (
      <div className="flex flex-col items-start gap-1" data-testid="config-invalid">
        <Badge tone="danger">Config invalid</Badge>
        {router.config_problems.slice(0, 3).map((p) => (
          <span key={p} className="max-w-64 text-xs text-muted-foreground [overflow-wrap:anywhere]">
            {p}
          </span>
        ))}
      </div>
    );
  }
  if (strategy && strategy.missing_gateways.length) {
    return <Badge tone="warn">Missing on {strategy.missing_gateways.join(", ")}</Badge>;
  }
  return <Badge tone="ok">OK</Badge>;
}

export function Routers() {
  const perms = useCan();
  const list = useRouters();
  const models = useModels();
  const aliases = useAliases();
  const strategies = useStrategies();
  const modelName = useNames(models.data);
  const modelById = useById(models.data);
  const [search, setSearch] = useState("");
  const [deleting, setDeleting] = useState<Router | null>(null);
  const navigate = useNavigate();
  const strategyByName = useMemo(() => new Map((strategies.data ?? []).map((s) => [s.name, s])), [strategies.data]);
  const remove = useMut((id: string) => api.deleteRouter(id), "Router deleted");
  const edit = (r: Router) => void navigate({ to: "/routers/$routerId", params: { routerId: r.id } });

  const rows = useMemo(
    () =>
      (list.data ?? []).filter((r) => {
        const upstreamModels = routerUpstreamModels(r, modelById);
        return matches(search, r.name, r.model_type, typeLabel(r.model_type), ...upstreamModels);
      }),
    [list.data, search, modelById],
  );
  const aliasesOf = (r: Router) => (aliases.data ?? []).filter((a) => a.target === r.id).map((a) => a.name);
  const loadError = list.error ?? models.error ?? aliases.error ?? strategies.error;

  return (
    <>
      <PageHeader
        title="Routers"
        actions={
          <CreateButton can={perms.isInstallationAdmin} onClick={() => void navigate({ to: "/routers/new" })}>
            New router
          </CreateButton>
        }
      />
      <SearchBox value={search} onChange={setSearch} label="Search routers" placeholder="Name or upstream model" />
      <DataTable
        rows={rows}
        loading={list.isPending || models.isPending || aliases.isPending || strategies.isPending}
        error={loadError}
        onRetry={() => {
          void list.refetch();
          void models.refetch();
          void aliases.refetch();
          void strategies.refetch();
        }}
        rowKey={(r) => r.id}
        empty={search ? "No routers match your search." : "No routers yet."}
        columns={[
          {
            key: "name",
            header: "Name",
            sort: (a, b) => a.name.localeCompare(b.name),
            cell: (r) => (
              <Link to="/routers/$routerId" params={{ routerId: r.id }} className="font-semibold hover:underline">
                {r.name}
              </Link>
            ),
          },
          {
            key: "type",
            header: "Type",
            cell: (r) => <TypeBadge type={r.model_type} />,
          },
          {
            key: "strategy",
            header: "Strategy",
            cell: (r) => (
              <div className="flex flex-col">
                <span>
                  {(() => {
                    const s = strategyByName.get(r.strategy);
                    return s ? strategyTitle(s) : r.strategy;
                  })()}
                </span>
                <code className="text-muted-foreground">{r.strategy}</code>
              </div>
            ),
          },
          {
            key: "members",
            header: "Members",
            cell: (r) => {
              const strategy = strategyByName.get(r.strategy);
              if (!r.traffic_split.length) return <div className="flex flex-col gap-0.5">{r.members.map((m) => memberSummary(m, modelName, strategy))}</div>;
              const pct = splitShares(r.traffic_split.map((s) => s.weight));
              return (
                <div className="flex flex-col gap-1.5">
                  {r.traffic_split.map((s, i) => (
                    <div key={s.name} className="flex flex-col gap-0.5">
                      <Badge tone="ok">
                        {s.name} {pct[i]}
                      </Badge>
                      {s.members.map((m) => memberSummary(m, modelName, strategy))}
                    </div>
                  ))}
                </div>
              );
            },
          },
          {
            key: "health",
            header: "Health",
            cell: (r) => <Health router={r} strategy={strategyByName.get(r.strategy)} />,
          },
          {
            key: "fallback",
            header: "Fallback",
            hideBelow: "md",
            cell: (r) =>
              r.fallback_model_ids.length || r.retry || r.sticky !== "none" ? (
                <div className="flex flex-wrap gap-1">
                  {r.fallback_model_ids.map((id, i) => (
                    <Badge key={id}>
                      {i + 1}. → {modelName(id)}
                    </Badge>
                  ))}
                  {r.retry && <Badge tone="info">retry ×{r.retry.max_attempts ?? 2}</Badge>}
                  {r.sticky !== "none" && <Badge>sticky: {r.sticky === "header" ? r.sticky_header : r.sticky}</Badge>}
                </div>
              ) : (
                <span className="text-muted-foreground">—</span>
              ),
          },
          {
            key: "aliases",
            header: "Aliases",
            hideBelow: "md",
            cell: (r) => {
              const names = aliasesOf(r);
              return names.length ? (
                <div className="flex flex-wrap gap-1">
                  {names.map((n) => (
                    <Badge key={n} tone="info">
                      ↳ {n}
                    </Badge>
                  ))}
                </div>
              ) : (
                <span className="text-muted-foreground">—</span>
              );
            },
          },
          {
            key: "actions",
            header: "",
            cell: (r) => <RowActions canEdit={perms.isInstallationAdmin} canDelete={perms.isInstallationAdmin} onEdit={() => edit(r)} onDelete={() => setDeleting(r)} />,
          },
        ]}
      />

      <StrategiesTable strategies={strategies.data} loading={strategies.isPending} error={strategies.error} onRetry={() => void strategies.refetch()} />

      <ConfirmDialog
        open={!!deleting}
        title="Delete router"
        message={`Delete router “${deleting?.name}”? Clients requesting this name will get an error. A router an alias points at cannot be deleted.`}
        onConfirm={() => remove.mutateAsync(deleting!.id)}
        onClose={() => setDeleting(null)}
      />
    </>
  );
}

function StrategiesTable({ strategies, loading, error, onRetry }: { strategies: Plugin[] | undefined; loading: boolean; error: Error | null; onRetry: () => void }) {
  // Connected gateways known from any strategy's support or missing list.
  const total = useMemo(() => {
    const ids = new Set<string>();
    for (const s of strategies ?? []) {
      s.gateways.forEach((g) => ids.add(g.gateway_id));
      s.missing_gateways.forEach((g) => ids.add(g));
    }
    return ids.size;
  }, [strategies]);
  return (
    <section className="mt-8" aria-labelledby="strategies-h">
      <h2 id="strategies-h" className="mb-3 text-base font-semibold">
        Strategies
      </h2>
      <DataTable
        rows={strategies}
        loading={loading}
        error={error}
        onRetry={onRetry}
        rowKey={(s) => s.name}
        empty="No strategies are known yet."
        columns={[
          {
            key: "strategy",
            header: "Strategy",
            cell: (s) => (
              <div className="flex flex-col">
                <strong>{strategyTitle(s)}</strong>
                <code className="text-muted-foreground">{s.name}</code>
              </div>
            ),
          },
          {
            key: "source",
            header: "Source",
            cell: (s) => <Badge tone={s.builtin ? "muted" : "info"}>{s.builtin ? "Built-in" : "WASM"}</Badge>,
          },
          {
            key: "settings",
            header: "Settings",
            hideBelow: "md",
            cell: (s) => {
              const titles = [...settingTitles(s.params_schema), ...(s.uses_weight ? ["Weight"] : []), ...settingTitles(s.member_schema)];
              return titles.length ? <span>{titles.join(", ")}</span> : <span className="text-muted-foreground">—</span>;
            },
          },
          {
            key: "gateways",
            header: "Gateways",
            align: "right",
            cell: (s) => {
              if (!total) return <span className="text-muted-foreground">—</span>;
              const supported = new Set(s.gateways.map((g) => g.gateway_id)).size;
              return (
                <Badge tone={s.missing_gateways.length ? "warn" : "ok"}>
                  {supported} / {total}
                </Badge>
              );
            },
          },
        ]}
      />
    </section>
  );
}
