import { useMemo } from "react";

import type { Plugin, PluginKind } from "../api";
import { Badge, PageHeader } from "../components/Bits";
import { DataTable } from "../components/Table";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { usePlugins } from "../hooks";
import { settingTitles, strategyDescription, strategyTitle } from "../lib/schema";

/*
 * The plugin inventory: read-only. A plugin is a built-in of the gateway or a WASM file it loaded from its
 * plugin directory at startup; there is nothing to upload or edit here. Everything but the name and the
 * source comes from a gateway's Hello and is rendered as plain text.
 */

const KINDS: { kind: PluginKind; title: string }[] = [
  { kind: "router-strategy", title: "Router strategies" },
  { kind: "upstream-auth", title: "Upstream auth" },
  { kind: "a2a-middleware", title: "A2A middleware" },
];

export function Plugins() {
  const plugins = usePlugins(undefined, true, 10_000);
  const rows = plugins.data ?? [];

  // Every connected gateway any plugin mentions, so "which gateways have it" has a denominator.
  const total = useMemo(() => {
    const all = new Set<string>();
    for (const p of rows) {
      for (const g of p.gateways) if (g.connected) all.add(g.gateway_id);
      for (const g of p.missing_gateways) all.add(g);
    }
    return all.size;
  }, [rows]);

  const header = <PageHeader title="Plugins" hint="A gateway loads the built-ins and every .wasm file in its plugin directories at startup and reports them; there is nothing to upload here." />;
  if (plugins.error) {
    return (
      <>
        {header}
        <Alert variant="destructive" data-testid="state-error">
          <AlertDescription className="flex flex-col items-start gap-3">
            <p>{plugins.error.message}</p>
            <Button variant="outline" size="sm" onClick={() => void plugins.refetch()}>
              Retry
            </Button>
          </AlertDescription>
        </Alert>
      </>
    );
  }
  return (
    <>
      {header}
      {KINDS.map((k) => (
        <section key={k.kind} className="mb-8 last:mb-0" aria-labelledby={`plugins-${k.kind}`}>
          <h2 id={`plugins-${k.kind}`} className="mb-3 text-base font-semibold">
            {k.title}
          </h2>
          <DataTable
            rows={rows.filter((p) => p.kind === k.kind)}
            loading={plugins.isPending}
            rowKey={(p) => `${p.kind}:${p.name}`}
            empty={`No ${k.title.toLowerCase()} are known yet.`}
            columns={[
              {
                key: "plugin",
                header: "Plugin",
                sort: (a, b) => a.name.localeCompare(b.name),
                cell: (p) => (
                  <div className="flex min-w-40 flex-col">
                    {/* Titles and names are gateway data: plain text, clamped, the full text on hover. */}
                    <strong className="line-clamp-2 [overflow-wrap:anywhere]" title={strategyTitle(p)}>
                      {strategyTitle(p)}
                    </strong>
                    <code className="text-muted-foreground">{p.name}</code>
                  </div>
                ),
              },
              {
                key: "description",
                header: "Description",
                hideBelow: "lg",
                cell: (p) => (
                  <span className="line-clamp-3 max-w-96 [overflow-wrap:anywhere]" title={strategyDescription(p)}>
                    {strategyDescription(p) || "—"}
                  </span>
                ),
              },
              {
                key: "source",
                header: "Source",
                cell: (p) => (
                  <div className="flex flex-col items-start gap-1">
                    <Badge tone={p.builtin ? "muted" : "info"}>{p.builtin ? "Built-in" : "WASM"}</Badge>
                    {!p.builtin && p.wasm_sha256 && <code className="text-xs text-muted-foreground">{p.wasm_sha256.slice(0, 12)}</code>}
                  </div>
                ),
              },
              {
                key: "settings",
                header: "Settings",
                hideBelow: "md",
                cell: (p) => <Settings p={p} />,
              },
              {
                key: "gateways",
                header: "Gateways",
                align: "right",
                cell: (p) => <Gateways p={p} total={total} />,
              },
            ]}
          />
        </section>
      ))}
    </>
  );
}

/** What a plugin lets a user set: the schema's titles, plus the member weight a strategy uses. */
function Settings({ p }: { p: Plugin }) {
  const titles = [...settingTitles(p.params_schema), ...(p.uses_weight ? ["Weight"] : []), ...settingTitles(p.member_schema)];
  return (
    <div className="flex max-w-56 flex-col gap-1">
      {titles.length ? (
        <span className="line-clamp-3 [overflow-wrap:anywhere]" title={titles.join(", ")}>
          {titles.join(", ")}
        </span>
      ) : (
        <span className="text-muted-foreground">—</span>
      )}
      {p.schema_problems.length > 0 && (
        <span className="line-clamp-2 text-xs text-muted-foreground" data-testid="schema-problems" title={p.schema_problems.join("; ")}>
          {p.schema_problems.slice(0, 2).join("; ")}
        </span>
      )}
    </div>
  );
}

/** Which gateways have it, and which connected ones do not (they would reject a config that uses it). */
function Gateways({ p, total }: { p: Plugin; total: number }) {
  // A gateway is one process: one entry per connected gateway that reports the plugin.
  const supported = p.gateways.filter((g) => g.connected).map((g) => g.gateway_id);
  if (!total) return <span className="text-muted-foreground">—</span>;
  return (
    <div className="flex flex-col items-end gap-1">
      <Badge tone={p.missing_gateways.length ? "warn" : "ok"}>
        {supported.length} / {total}
      </Badge>
      {supported.map((id) => (
        <code key={id} className="whitespace-nowrap text-xs text-muted-foreground" data-testid={`plugin-gateway-${id}`}>
          {id}
        </code>
      ))}
      {p.missing_gateways.length > 0 && (
        <span className="text-xs text-destructive [overflow-wrap:anywhere]">
          missing:{" "}
          {p.missing_gateways.map((id, n) => (
            <span key={id}>
              {n > 0 && ", "}
              <code data-testid={`plugin-missing-${id}`}>{id}</code>
            </span>
          ))}
        </span>
      )}
    </div>
  );
}
