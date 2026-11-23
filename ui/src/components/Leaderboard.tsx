import { useQueries, type UseQueryResult } from "@tanstack/react-query";
import { useState } from "react";

import { api, type LeaderboardRow } from "../api";
import { Badge, fmtCompact, fmtNumber as n, fmtUsd, fmtUsdShort } from "./Bits";
import { cn } from "../lib/utils";
import { KeyPill } from "./KeyPill";
import { Card, CardContent, CardHeader, CardTitle } from "./ui/card";
import { Toggle } from "./usage/Toggle";

import type { UsageLens } from "../lib/usageMetric";

type By = "called" | "key" | "owner";

const BOARDS: { by: By; title: string }[] = [
  { by: "called", title: "Models" },
  { by: "key", title: "Keys" },
  { by: "owner", title: "Users" },
];

const isAnonymous = (row: LeaderboardRow, by: By) => by !== "called" && !(by === "key" ? row.key : row.owner_id);

/** Where a row has no readable identity the server sends a "Key #3" style label: it is shown muted. */
function Label({ row, by }: { row: LeaderboardRow; by: By }) {
  if (by === "key" && row.key)
    return <KeyPill keyRef={row.key} fallbackId={row.key.id} />;
  const anonymous = isAnonymous(row, by);
  return (
    <span
      className={
        anonymous
          ? "text-muted-foreground"
          : "font-medium [overflow-wrap:anywhere]"
      }
    >
      {row.label}
    </span>
  );
}

type Column = "tokens" | "requests" | "spend";

const COLUMN_LABEL: Record<Column, string> = { tokens: "Tokens", requests: "Requests", spend: "Spend" };

const KIND_LABEL: Record<string, string> = { model: "model", alias: "alias", router: "router" };

function Cell({ row, column }: { row: LeaderboardRow; column: Column }) {
  if (column === "spend") return <span title={fmtUsd(row.cost_nano_usd)}>{fmtUsdShort(row.cost_nano_usd)}</span>;
  if (column === "tokens") return <span title={`${n(row.total_tokens)} tokens`}>{fmtCompact(row.total_tokens)}</span>;
  return <span>{n(row.requests)}</span>;
}

function Board({ by, zone, lens, routing, query }: { lens: UsageLens; by: By; zone?: string; routing: boolean; query: UseQueryResult<LeaderboardRow[]> }) {
  // The server ranks by the lens: tokens, or spend in the Cost lens.
  const rows = query.data ?? [];
  const value = (row: LeaderboardRow) => (lens === "cost" ? row.cost_nano_usd : row.total_tokens);
  const peak = Math.max(1, ...rows.map(value));
  // Each lens shows only its own quantity: tokens, or spend in the Cost lens; requests in both.
  const columns: Column[] = lens === "cost" ? ["spend", "requests"] : ["tokens", "requests"];
  const strong: Column = columns[0];
  const grid = "grid grid-cols-[2rem_minmax(0,1fr)] items-start gap-x-2 gap-y-1 sm:grid-cols-[2rem_minmax(0,1fr)_6rem_6rem]";
  if (query.isPending) return <p className="text-muted-foreground">Loading…</p>;
  if (query.error)
    return (
      <p className="text-destructive" role="alert">
        {query.error.message}
      </p>
    );
  if (rows.length === 0) return <p className="text-muted-foreground">No usage in this range.</p>;
  return (
    <div>
      <div className={cn(grid, "mb-2 hidden text-xs text-muted-foreground sm:grid")} aria-hidden="true">
        <span>#</span>
        <span />
        {columns.map((c) => (
          <span key={c} className="text-right">
            {COLUMN_LABEL[c]}
          </span>
        ))}
      </div>
      <ol className="m-0 grid list-none gap-3 p-0">
        {rows.map((row, at) => (
          <li key={`${row.zone}|${row.rank}|${row.label}`} className={grid} data-testid="leaderboard-row">
            <span className="text-muted-foreground tabular-nums">{at + 1}</span>
            <div className="min-w-0">
              <span className="flex min-w-0 flex-wrap items-center gap-1.5">
                <Label row={row} by={by} />
                {row.is_me && <Badge tone="info">you</Badge>}
                {routing && by === "called" && row.called_kind && KIND_LABEL[row.called_kind] && <Badge>{KIND_LABEL[row.called_kind]}</Badge>}
                {!zone && row.zone && <Badge tone="info">{row.zone}</Badge>}
              </span>
              <span className="mt-1 block h-1.5 w-full max-w-[18rem] overflow-hidden rounded-full bg-muted" aria-hidden="true">
                <span className="block h-full bg-primary" style={{ width: `${(value(row) / peak) * 100}%` }} />
              </span>
            </div>
            <div className="col-start-2 flex gap-3 text-sm text-muted-foreground sm:contents">
              {columns.map((c) => (
                <span key={c} data-column={c} className={cn("tabular-nums sm:text-right sm:text-sm", c === strong ? "font-semibold text-foreground" : "sm:text-foreground")}>
                  <Cell row={row} column={c} />
                </span>
              ))}
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}

/** Top ten by model, key and owner for the page's range and team, one board at a time. */
export function Leaderboard({
  from,
  to,
  teamId,
  zone,
  lens,
  seesOthers,
  routing = false,
}: {
  lens: UsageLens;
  from: string;
  to: string;
  teamId: string;
  zone?: string;
  /** An installation or team admin: other people's keys and owners are readable, so every board stays. */
  seesOthers: boolean;
  /** Show whether a called name is a model, an alias or a router. */
  routing?: boolean;
}) {
  const [view, setView] = useState<By>("called");
  // An admin fetches only the board on screen; for anyone else all three (ten rows each) are
  // fetched, as the member rule below needs them to decide which boards to offer.
  const queries = useQueries({
    queries: BOARDS.map(({ by }) => {
      const params = {
        by,
        from,
        to,
        kind: "llm" as const,
        team_id: teamId || undefined,
        zone,
        limit: 10,
        order: lens === "cost" ? ("cost" as const) : ("tokens" as const),
      };
      return { queryKey: ["usage-leaderboard", params], queryFn: () => api.usageLeaderboard(params), retry: false, enabled: !seesOthers || by === view };
    }),
  });
  // For a plain member, a key or user board with no row of their own would only repeat the
  // total as "Key #1": it is left out. An admin keeps it (a deleted key is anonymous there too).
  const boards = BOARDS.map((board, i) => ({ ...board, query: queries[i] })).filter(
    ({ by, query }) => seesOthers || by === "called" || (query.isSuccess && (query.data.length === 0 || query.data.some((row) => !isAnonymous(row, by)))),
  );
  const active = boards.find((b) => b.by === view) ?? boards[0];
  return (
    <section className="my-4" aria-label="Leaderboards" data-testid="leaderboards">
      <Card className="gap-3">
        <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-2">
          <CardTitle>Top 10</CardTitle>
          <Toggle label="Leaderboard view" testId="leaderboard-view" value={active.by} options={boards.map((b) => ({ value: b.by, label: b.title }))} onChange={setView} />
        </CardHeader>
        <CardContent>
          <div data-testid={`leaderboard-${active.by}`}>
            <Board by={active.by} zone={zone} lens={lens} routing={routing} query={active.query} />
          </div>
        </CardContent>
      </Card>
    </section>
  );
}
