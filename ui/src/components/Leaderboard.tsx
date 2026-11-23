import { useQuery } from "@tanstack/react-query";

import { api, type LeaderboardRow } from "../api";
import { Badge, fmtCompact, fmtNumber as n, fmtUsd } from "./Bits";
import { KeyPill } from "./KeyPill";
import { Card, CardContent, CardHeader, CardTitle } from "./ui/card";

import type { UsageLens } from "../lib/usageMetric";

type By = "called" | "key" | "owner";

const BOARDS: { by: By; title: string }[] = [
  { by: "called", title: "Called names" },
  { by: "key", title: "Keys" },
  { by: "owner", title: "Users" },
];

/** Where a row has no readable identity the server sends a "Key #3" style label: it is shown muted. */
function Label({ row, by }: { row: LeaderboardRow; by: By }) {
  if (by === "key" && row.key)
    return <KeyPill keyRef={row.key} fallbackId={row.key.id} />;
  const anonymous =
    by !== "called" && !(by === "key" ? row.key : row.owner_id);
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

function Board({
  by,
  title,
  from,
  to,
  teamId,
  zone,
  lens,
}: {
  lens: UsageLens;
  by: By;
  title: string;
  from: string;
  to: string;
  teamId: string;
  /** The `?zone=` value; absent = every zone. */
  zone?: string;
}) {
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
  const query = useQuery({
    queryKey: ["usage-leaderboard", params],
    queryFn: () => api.usageLeaderboard(params),
    retry: false,
  });
  // The server ranks by the lens: tokens, or spend in the Cost lens.
  const rows = (query.data ?? []).map((row, i) => ({ row, rank: i + 1 }));
  const value = (row: LeaderboardRow) => (lens === "cost" ? row.cost_nano_usd : row.total_tokens);
  const peak = Math.max(1, ...rows.map(({ row }) => value(row)));
  return (
    <Card className="gap-3" data-testid={`leaderboard-${by}`}>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent>
        {query.isPending ? (
          <p className="text-muted-foreground">Loading…</p>
        ) : query.error ? (
          <p className="text-destructive" role="alert">
            {query.error.message}
          </p>
        ) : rows.length === 0 ? (
          <p className="text-muted-foreground">No usage in this range.</p>
        ) : (
          <ol className="m-0 grid list-none gap-2.5 p-0">
            {rows.map(({ row }, at) => (
              <li
                key={`${row.zone}|${row.rank}|${row.label}`}
                className="grid grid-cols-[1.5rem_minmax(0,1fr)_auto] items-center gap-x-2 gap-y-1"
                data-testid="leaderboard-row"
              >
                <span className="text-muted-foreground tabular-nums">
                  {at + 1}
                </span>
                <span className="flex min-w-0 flex-wrap items-center gap-1.5">
                  <Label row={row} by={by} />
                  {row.is_me && <Badge tone="info">you</Badge>}
                  {!zone && <Badge tone="info">{row.zone}</Badge>}
                </span>
                <span className="text-right tabular-nums">
                  {lens === "cost" ? fmtUsd(row.cost_nano_usd) : <span title={`${n(row.total_tokens)} tokens`}>{fmtCompact(row.total_tokens)}</span>}
                </span>
                <span />
                <span className="flex items-center gap-2 text-sm tabular-nums">
                  <span
                    className="h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-muted"
                    aria-hidden="true"
                  >
                    <span
                      className="block h-full bg-primary"
                      style={{ width: `${(value(row) / peak) * 100}%` }}
                    />
                  </span>
                  <span>{n(row.requests)} req</span>
                </span>
                <span />
              </li>
            ))}
          </ol>
        )}
      </CardContent>
    </Card>
  );
}

/** Top ten by called name, key and owner for the page's range and team; stacked on a narrow screen. */
export function Leaderboard({
  from,
  to,
  teamId,
  zone,
  lens,
}: {
  lens: UsageLens;
  from: string;
  to: string;
  teamId: string;
  zone?: string;
}) {
  return (
    <section
      className="my-4 grid gap-3 lg:grid-cols-3"
      aria-label="Leaderboards"
      data-testid="leaderboards"
    >
      {BOARDS.map((board) => (
        <Board key={board.by} {...board} from={from} to={to} teamId={teamId} zone={zone} lens={lens} />
      ))}
    </section>
  );
}
