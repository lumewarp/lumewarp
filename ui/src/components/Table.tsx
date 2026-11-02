import { ArrowDown, ArrowUp } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";

import { cn } from "../lib/utils";
import { Card } from "./ui/card";
import { Skeleton } from "./ui/skeleton";
import { Button } from "./ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "./ui/table";

export interface Column<T> {
  key: string;
  header: string;
  cell: (row: T) => ReactNode;
  /** Provide to make the column sortable. */
  sort?: (a: T, b: T) => number;
  align?: "right";
  /** Hide this column below the given breakpoint so a phone keeps the columns that matter. */
  hideBelow?: "sm" | "md" | "lg";
}

const HIDE = { sm: "hidden sm:table-cell", md: "hidden md:table-cell", lg: "hidden lg:table-cell" } as const;

interface Props<T> {
  columns: Column<T>[];
  rows: T[] | undefined;
  rowKey: (row: T) => string;
  loading?: boolean;
  error?: Error | null;
  onRetry?: () => void;
  empty?: ReactNode;
  pageSize?: number;
}

export function DataTable<T>({ columns, rows, rowKey, loading, error, onRetry, empty = "Nothing here yet.", pageSize = 10 }: Props<T>) {
  const [sort, setSort] = useState<{ key: string; dir: 1 | -1 } | null>(null);
  const [page, setPage] = useState(0);

  const sorted = useMemo(() => {
    const col = columns.find((c) => c.key === sort?.key);
    if (!rows || !col?.sort || !sort) return rows ?? [];
    return [...rows].sort((a, b) => col.sort!(a, b) * sort.dir);
  }, [rows, columns, sort]);

  const pages = Math.max(1, Math.ceil(sorted.length / pageSize));
  const cur = Math.min(page, pages - 1);
  const visible = sorted.slice(cur * pageSize, (cur + 1) * pageSize);

  const toggle = (key: string) =>
    setSort((s) => (s?.key !== key ? { key, dir: 1 } : s.dir === 1 ? { key, dir: -1 } : null));

  let body: ReactNode;
  if (error) {
    body = (
      <div className="state state-error" role="alert" data-testid="state-error">
        <p>{error.message}</p>
        {onRetry && (
          <Button variant="outline" size="sm" onClick={onRetry}>
            Retry
          </Button>
        )}
      </div>
    );
  } else if (loading) {
    body = (
      <div className="state" aria-busy="true">
        {[0, 1, 2].map((i) => (
          <Skeleton key={i} className="mx-auto mb-3 h-4 w-full" />
        ))}
      </div>
    );
  } else if (sorted.length === 0) {
    body = <div className="state">{empty}</div>;
  }

  return (
    <Card className="gap-0 overflow-hidden py-0">
      {body ?? (
        <Table>
          <TableHeader>
            <TableRow>
              {columns.map((c) => {
                const active = sort?.key === c.key;
                return (
                  <TableHead key={c.key} scope="col" className={cn(c.align === "right" && "text-right", c.hideBelow && HIDE[c.hideBelow])} aria-sort={active ? (sort.dir === 1 ? "ascending" : "descending") : undefined}>
                    {c.sort ? (
                      <button type="button" className="inline-flex items-center gap-1 rounded-sm text-left uppercase tracking-[0.04em] hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/50" onClick={() => toggle(c.key)}>
                        {c.header}
                        {active && (sort.dir === 1 ? <ArrowUp className="size-3" aria-hidden="true" /> : <ArrowDown className="size-3" aria-hidden="true" />)}
                      </button>
                    ) : (
                      c.header
                    )}
                  </TableHead>
                );
              })}
            </TableRow>
          </TableHeader>
          <TableBody>
            {visible.map((row) => (
              <TableRow key={rowKey(row)}>
                {columns.map((c) => (
                  <TableCell key={c.key} className={cn(c.align === "right" && "text-right tabular-nums", c.hideBelow && HIDE[c.hideBelow])}>
                    {c.cell(row)}
                  </TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      {!body && pages > 1 && (
        <div className="pager border-t">
          <span>
            {cur * pageSize + 1}–{Math.min((cur + 1) * pageSize, sorted.length)} of {sorted.length}
          </span>
          <Button variant="outline" size="sm" disabled={cur === 0} onClick={() => setPage(cur - 1)}>
            Previous
          </Button>
          <Button variant="outline" size="sm" disabled={cur >= pages - 1} onClick={() => setPage(cur + 1)}>
            Next
          </Button>
        </div>
      )}
    </Card>
  );
}
