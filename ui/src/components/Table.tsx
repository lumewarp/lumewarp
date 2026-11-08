import { ArrowDown, ArrowUp, ChevronLeftIcon, ChevronRightIcon, ChevronsLeftIcon, ChevronsRightIcon } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";

import { cn } from "../lib/utils";
import { Alert, AlertDescription } from "./ui/alert";
import { Button } from "./ui/button";
import { Card } from "./ui/card";
import { Empty } from "./ui/empty";
import { Skeleton } from "./ui/skeleton";
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
  /** Server-side paging (a cursor API): replaces the client-side pages, and the footer always shows. */
  paging?: Paging;
}

export interface Paging {
  /** What the footer says, for example the range shown. */
  summary: ReactNode;
  onPrevious?: () => void;
  onNext?: () => void;
  onFirst?: () => void;
  onLast?: () => void;
}

/**
 * The pager under a table: a summary, then icon buttons. An undefined handler disables its button; leaving
 * `onFirst` or `onLast` out of the props hides it (a cursor API has no last page).
 */
export function TablePagination(props: Paging) {
  const { summary, onFirst, onPrevious, onNext, onLast } = props;
  const nav = (label: string, Icon: typeof ChevronLeftIcon, onClick: (() => void) | undefined) => (
    <Button type="button" variant="outline" size="icon" className="size-8" aria-label={label} disabled={!onClick} onClick={onClick}>
      <Icon aria-hidden="true" />
    </Button>
  );
  return (
    <div className="flex flex-wrap items-center justify-end gap-2 border-t px-3.5 py-2 text-sm text-muted-foreground" data-testid="table-pagination">
      <span className="mr-2 tabular-nums">{summary}</span>
      {"onFirst" in props && nav("First page", ChevronsLeftIcon, onFirst)}
      {nav("Previous page", ChevronLeftIcon, onPrevious)}
      {nav("Next page", ChevronRightIcon, onNext)}
      {"onLast" in props && nav("Last page", ChevronsRightIcon, onLast)}
    </div>
  );
}

export function DataTable<T>({ columns, rows, rowKey, loading, error, onRetry, empty = "Nothing here yet.", pageSize = 10, paging }: Props<T>) {
  const [sort, setSort] = useState<{ key: string; dir: 1 | -1 } | null>(null);
  const [page, setPage] = useState(0);

  const sorted = useMemo(() => {
    const col = columns.find((c) => c.key === sort?.key);
    if (!rows || !col?.sort || !sort) return rows ?? [];
    return [...rows].sort((a, b) => col.sort!(a, b) * sort.dir);
  }, [rows, columns, sort]);

  const pages = paging ? 1 : Math.max(1, Math.ceil(sorted.length / pageSize));
  const cur = Math.min(page, pages - 1);
  const visible = paging ? sorted : sorted.slice(cur * pageSize, (cur + 1) * pageSize);

  const toggle = (key: string) =>
    setSort((s) => (s?.key !== key ? { key, dir: 1 } : s.dir === 1 ? { key, dir: -1 } : null));

  let body: ReactNode;
  if (error) {
    body = (
      <Alert variant="destructive" className="rounded-none border-0 py-6" data-testid="state-error">
        <AlertDescription className="col-start-2 flex flex-col items-center gap-3 text-center text-sm">
          <p>{error.message}</p>
          {onRetry && (
            <Button variant="outline" size="sm" onClick={onRetry}>
              Retry
            </Button>
          )}
        </AlertDescription>
      </Alert>
    );
  } else if (loading) {
    body = (
      <div className="p-8" aria-busy="true">
        {[0, 1, 2].map((i) => (
          <Skeleton key={i} className="mx-auto mb-3 h-4 w-full" />
        ))}
      </div>
    );
  } else if (sorted.length === 0) {
    body = (
      <Empty className="border-0 p-8">
        <div className="text-sm text-muted-foreground">{empty}</div>
      </Empty>
    );
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
                      <Button type="button" variant="ghost" size="sm" className="-mx-2 h-7 gap-1 px-2 text-left font-[inherit] uppercase tracking-[0.04em] text-[inherit] hover:text-foreground" onClick={() => toggle(c.key)}>
                        {c.header}
                        {active && (sort.dir === 1 ? <ArrowUp className="size-3" aria-hidden="true" /> : <ArrowDown className="size-3" aria-hidden="true" />)}
                      </Button>
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
      {paging && !error ? (
        <TablePagination {...paging} />
      ) : (
        !body &&
        pages > 1 && (
          <TablePagination
            summary={`${cur * pageSize + 1}–${Math.min((cur + 1) * pageSize, sorted.length)} of ${sorted.length}`}
            onFirst={cur > 0 ? () => setPage(0) : undefined}
            onPrevious={cur > 0 ? () => setPage(cur - 1) : undefined}
            onNext={cur < pages - 1 ? () => setPage(cur + 1) : undefined}
            onLast={cur < pages - 1 ? () => setPage(pages - 1) : undefined}
          />
        )
      )}
    </Card>
  );
}
