import { useQuery } from "@tanstack/react-query";
import { Split } from "lucide-react";
import { useState } from "react";

import { api, type UsageQuery, type UsageRow } from "../../api";
import { fmtNumber as n } from "../Bits";
import { Badge } from "../Bits";
import { Button } from "../ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "../ui/dialog";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../ui/table";
import { Tooltip, TooltipContent, TooltipTrigger } from "../ui/tooltip";
import { fmtMs } from "./format";

const KIND_TONE = { model: "muted", alias: "info", router: "warn", unknown: "danger" } as const;

/** The model name the client used, with its kind for a caller who can read routing; a tooltip lists every spelling a rename left in the group. */
export function CalledCell({ row, showKind = false }: { row: UsageRow; showKind?: boolean }) {
  const names = row.called_names ?? [];
  const kind = row.called_kind ?? "unknown";
  const label = (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      <strong className="[overflow-wrap:anywhere]">{row.called || "—"}</strong>
      {showKind && <Badge tone={KIND_TONE[kind]}>{kind}</Badge>}
      {names.length > 1 && <span className="text-xs text-muted-foreground">{names.length} spellings</span>}
    </span>
  );
  if (names.length < 2) return label;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span tabIndex={0} data-testid="called-names">
          {label}
        </span>
      </TooltipTrigger>
      <TooltipContent>
        <div className="font-medium">Spellings clients used</div>
        <ul className="m-0 list-none p-0">
          {names.map((name) => (
            <li key={name}>
              <code>{name}</code>
            </li>
          ))}
        </ul>
      </TooltipContent>
    </Tooltip>
  );
}

/** Called × served for one called name: which model answered, which upstream name it went to, how often by fallback. */
export function CalledBreakdown({ row, filters }: { row: UsageRow; filters: UsageQuery }) {
  const [open, setOpen] = useState(false);
  const query = {
    ...filters,
    group_by: "called,model",
    called: row.called ?? undefined,
  } satisfies UsageQuery;
  const result = useQuery({ queryKey: ["usage", query], queryFn: () => api.usage(query), enabled: open, retry: false });
  const rows = result.data ?? [];
  return (
    <>
      {/* An icon keeps the Log column narrow; the name and the tooltip say what it opens. */}
      <Tooltip>
        <TooltipTrigger asChild>
          <Button type="button" variant="ghost" size="icon" className="size-7" aria-label="Served by" aria-haspopup="dialog" onClick={() => setOpen(true)}>
            <Split aria-hidden="true" />
          </Button>
        </TooltipTrigger>
        <TooltipContent>Served by</TooltipContent>
      </Tooltip>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-3xl" data-testid="called-breakdown">
          <DialogHeader>
            <DialogTitle>{row.called} — served by</DialogTitle>
            <DialogDescription>Which model answered calls to this name, over the page's range and filters.</DialogDescription>
          </DialogHeader>
          {result.error ? (
            <p className="text-destructive" role="alert">
              {result.error.message}
            </p>
          ) : result.isPending ? (
            <p className="text-muted-foreground">Loading…</p>
          ) : rows.length === 0 ? (
            <p className="text-muted-foreground">No usage.</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Served model</TableHead>
                  <TableHead>Upstream model</TableHead>
                  <TableHead className="text-right">Requests</TableHead>
                  <TableHead className="text-right">Fallback</TableHead>
                  <TableHead className="text-right">p95</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((served) => (
                  <TableRow key={`${served.model_id ?? served.model}`}>
                    <TableCell className="font-medium">{served.model || "—"}</TableCell>
                    <TableCell>
                      {served.upstream_model ? (
                        <code>{served.upstream_model}</code>
                      ) : served.upstream_models?.length ? (
                        served.upstream_models.map((name) => <code key={name} className="mr-1">{name}</code>)
                      ) : (
                        "—"
                      )}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{n(served.requests)}</TableCell>
                    <TableCell className="text-right tabular-nums" data-testid="breakdown-fallback">
                      {served.requests ? `${((served.fallback_count / served.requests) * 100).toLocaleString(undefined, { maximumFractionDigits: 1 })}%` : "—"}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{fmtMs(served.p95_duration_ms)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}
