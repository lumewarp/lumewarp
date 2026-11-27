import { ArrowDown, ArrowUp, ChevronsLeftIcon } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";

import { cn } from "../lib/utils";
import { Alert, AlertDescription } from "./ui/alert";
import { Button } from "./ui/button";
import { Card } from "./ui/card";
import { Empty } from "./ui/empty";
import { Pagination, PaginationContent, PaginationEllipsis, PaginationItem, PaginationLink, PaginationNext, PaginationPrevious } from "./ui/pagination";
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
  /** A fixed column width (CSS length), so tables stacked on one page line up. */
  width?: string;
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
  /** Mute a row, for example a disconnected resource. */
  mutedRow?: (row: T) => boolean;
  /** Server-side paging (a cursor API): replaces the client-side pages, and the footer always shows. */
  paging?: Paging;
  /** Open a row's details on click; a cell should also hold a button doing the same for the keyboard. */
  onRowClick?: (row: T) => void;
  /** The row whose details are open, highlighted. */
  selectedKey?: string;
}

export interface Paging {
  /** What the footer says, for example the range shown. */
  summary: ReactNode;
  onFirst?: () => void;
  onPrevious?: () => void;
  onNext?: () => void;
}

/** Client-side pages: numbered links in place of First. */
interface Pages {
  current: number;
  count: number;
  onPage: (page: number) => void;
}

/** The page numbers to show: the first, the last and the current one with its neighbours, gaps as null. */
function pageWindow(current: number, count: number): (number | null)[] {
  const shown = [...new Set([0, current - 1, current, current + 1, count - 1])].filter((p) => p >= 0 && p < count).sort((a, b) => a - b);
  return shown.flatMap((p, i) => (i > 0 && p - shown[i - 1] > 1 ? [null, p] : [p]));
}

/**
 * The pager under a table, built from shadcn's Pagination: a summary, then the page controls. An undefined handler
 * disables its button; `onFirst` shows only when it is in the props, and `pages` adds numbered links.
 */
export function TablePagination(props: Paging & { pages?: Pages }) {
  const { summary, onFirst, onPrevious, onNext, pages } = props;
  return (
    <Pagination className="flex-wrap items-center justify-end gap-2 border-t px-3.5 py-2 text-sm text-muted-foreground" data-testid="table-pagination">
      <span className="mr-2 tabular-nums">{summary}</span>
      <PaginationContent>
        {"onFirst" in props && (
          <PaginationItem>
            <PaginationLink aria-label="First page" disabled={!onFirst} onClick={onFirst}>
              <ChevronsLeftIcon aria-hidden="true" />
            </PaginationLink>
          </PaginationItem>
        )}
        <PaginationItem>
          <PaginationPrevious aria-label="Previous page" disabled={!onPrevious} onClick={onPrevious} />
        </PaginationItem>
        {pages &&
          pageWindow(pages.current, pages.count).map((p, i) => (
            <PaginationItem key={p ?? `gap-${i}`}>
              {p === null ? (
                <PaginationEllipsis />
              ) : (
                <PaginationLink isActive={p === pages.current} aria-label={`Page ${p + 1}`} onClick={() => pages.onPage(p)}>
                  {p + 1}
                </PaginationLink>
              )}
            </PaginationItem>
          ))}
        <PaginationItem>
          <PaginationNext aria-label="Next page" disabled={!onNext} onClick={onNext} />
        </PaginationItem>
      </PaginationContent>
    </Pagination>
  );
}

/**
 * Paging over a cursor API, which only goes forward: the cursors of the pages already seen are kept as a trail.
 * A cursor belongs to one filter set, so a new `resetKey` starts again at the first page.
 */
export function useCursorPaging(resetKey: string) {
  const [trail, setTrail] = useState<(string | undefined)[]>([undefined]);
  // Reset during render (not in an effect) so the first query with the new filters already asks for page 1.
  const [seenKey, setSeenKey] = useState(resetKey);
  if (seenKey !== resetKey) {
    setSeenKey(resetKey);
    setTrail([undefined]);
  }
  const back = trail.length > 1;
  return {
    cursor: trail[trail.length - 1],
    /** Zero-based index of the page shown. */
    page: trail.length - 1,
    /** The footer for a page whose response carried `next`; `busy` (a fetch in flight) disables the controls. */
    paging: (next: string | null | undefined, busy: boolean, summary?: ReactNode): Paging => ({
      summary: summary ?? `Page ${trail.length}${next ? " · more" : ""}`,
      onFirst: back && !busy ? () => setTrail([undefined]) : undefined,
      onPrevious: back && !busy ? () => setTrail((current) => current.slice(0, -1)) : undefined,
      onNext: next && !busy ? () => setTrail((current) => [...current, next]) : undefined,
    }),
  };
}

export function DataTable<T>({ columns, rows, rowKey, loading, error, onRetry, empty = "Nothing here yet.", pageSize = 10, mutedRow, paging, onRowClick, selectedKey }: Props<T>) {
  const [sort, setSort] = useState<{ key: string; dir: 1 | -1 } | null>(null);
  const [page, setPage] = useState(0);

  // The column set changes with the grouping: a sort on a column that is gone is dropped, and paging restarts.
  // Adjusted during render (not in an effect) so the first paint of the new columns already shows page 1.
  const keys = columns.map((c) => c.key).join("\u0000");
  const [seenKeys, setSeenKeys] = useState(keys);
  if (seenKeys !== keys) {
    setSeenKeys(keys);
    setPage(0);
    if (sort && !columns.some((c) => c.key === sort.key && c.sort)) setSort(null);
  }
  if (import.meta.env.DEV) {
    const seen = new Set<string>();
    for (const c of columns) {
      if (seen.has(c.key)) console.error(`DataTable: duplicate column key "${c.key}"; give every column a unique key`);
      seen.add(c.key);
    }
  }

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
                  <TableHead key={c.key} scope="col" className={cn(c.align === "right" && "text-right", c.hideBelow && HIDE[c.hideBelow])} style={c.width ? { width: c.width } : undefined} aria-sort={active ? (sort.dir === 1 ? "ascending" : "descending") : undefined}>
                    {c.sort ? (
                      <Button type="button" variant="ghost" size="sm" className="-mx-2 h-7 gap-1 px-2 text-left font-[inherit] text-[length:inherit] uppercase tracking-[0.04em] text-[inherit] hover:text-foreground" onClick={() => toggle(c.key)}>
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
              <TableRow
                key={rowKey(row)}
                className={cn(mutedRow?.(row) ? "text-muted-foreground" : undefined, onRowClick ? "cursor-pointer" : undefined)}
                data-state={selectedKey !== undefined && rowKey(row) === selectedKey ? "selected" : undefined}
                onClick={onRowClick ? () => onRowClick(row) : undefined}
              >
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
            onPrevious={cur > 0 ? () => setPage(cur - 1) : undefined}
            onNext={cur < pages - 1 ? () => setPage(cur + 1) : undefined}
            pages={{ current: cur, count: pages, onPage: setPage }}
          />
        )
      )}
    </Card>
  );
}
