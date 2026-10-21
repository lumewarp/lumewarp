import clsx from "clsx";
import { useMemo, useState, type ReactNode } from "react";

export interface Column<T> {
  key: string;
  header: string;
  cell: (row: T) => ReactNode;
  /** Provide to make the column sortable. */
  sort?: (a: T, b: T) => number;
  align?: "right";
}

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
      <div className="state state-error" role="alert">
        <p>{error.message}</p>
        {onRetry && (
          <button className="btn" onClick={onRetry}>
            Retry
          </button>
        )}
      </div>
    );
  } else if (loading) {
    body = (
      <div className="state" aria-busy="true">
        {[0, 1, 2].map((i) => (
          <div key={i} className="skeleton" />
        ))}
      </div>
    );
  } else if (sorted.length === 0) {
    body = <div className="state">{empty}</div>;
  }

  return (
    <div className="card table-card">
      {body ?? (
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                {columns.map((c) => {
                  const active = sort?.key === c.key;
                  return (
                    <th key={c.key} scope="col" className={clsx(c.align === "right" && "num")} aria-sort={active ? (sort.dir === 1 ? "ascending" : "descending") : undefined}>
                      {c.sort ? (
                        <button className="th-btn" onClick={() => toggle(c.key)}>
                          {c.header}
                          <span aria-hidden="true">{active ? (sort.dir === 1 ? " ▲" : " ▼") : ""}</span>
                        </button>
                      ) : (
                        c.header
                      )}
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody>
              {visible.map((row) => (
                <tr key={rowKey(row)}>
                  {columns.map((c) => (
                    <td key={c.key} className={clsx(c.align === "right" && "num")}>
                      {c.cell(row)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {!body && pages > 1 && (
        <div className="pager">
          <span>
            {cur * pageSize + 1}–{Math.min((cur + 1) * pageSize, sorted.length)} of {sorted.length}
          </span>
          <button className="btn btn-sm" disabled={cur === 0} onClick={() => setPage(cur - 1)}>
            Previous
          </button>
          <button className="btn btn-sm" disabled={cur >= pages - 1} onClick={() => setPage(cur + 1)}>
            Next
          </button>
        </div>
      )}
    </div>
  );
}
