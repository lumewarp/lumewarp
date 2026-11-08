import { useMemo, useState } from "react";

import { api, type McpDefinition, type McpDefinitionState, type McpServer } from "../api";
import { fmtDateTime, Badge } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { Section } from "../components/EditPage";
import { SelectField } from "../components/Form";
import { DataTable } from "../components/Table";
import { Button } from "../components/ui/button";
import { useApprovedDefinitions, useMcpDefinitions, useMut } from "../hooks";
import { condense, diffCounts, lineDiff, prettyJson, type DiffRow } from "../lib/diff";
import { cn } from "../lib/utils";

/*
 * The review queue of one MCP server.
 *
 * A gateway withholds a definition whose hash it has not been told to serve, and reports it here. The list
 * comes newest first with a `<`-cursor (so only forward), and the filter starts at `pending`: the queue is
 * what an administrator opens the page for. Approving a hash supersedes the other pending hashes of the same
 * name on the server and deletes them, so the queue shrinks as it is worked through.
 *
 * The diff of a pending definition is against the approved definition of the same `(kind, name)`: that is
 * what tells an administrator whether an upstream changed a tool under the same name. Everything shown is
 * gateway data and stays plain text.
 */

const STATES: { value: McpDefinitionState | "all"; label: string }[] = [
  { value: "pending", label: "Pending" },
  { value: "approved", label: "Approved" },
  { value: "rejected", label: "Rejected" },
  { value: "all", label: "All states" },
];

const STATE_TONE: Record<McpDefinitionState, "warn" | "ok" | "danger"> = { pending: "warn", approved: "ok", rejected: "danger" };

const keyOf = (row: { kind: string; name: string }) => `${row.kind}:${row.name}`;

export function McpDefinitions({ server }: { server: McpServer }) {
  const [state, setState] = useState<McpDefinitionState | "all">("pending");
  // The cursor trail: the `<`-cursor only goes forward, so previous pages are remembered here.
  const [trail, setTrail] = useState<(string | undefined)[]>([undefined]);
  const cursor = trail[trail.length - 1];
  const definitions = useMcpDefinitions(server.id, state === "all" ? undefined : state, cursor);
  const items = definitions.data?.items ?? [];
  // Only the names in this page's queue: the approved set is read for those alone.
  const wanted = useMemo(() => items.filter((row) => row.state === "pending").map(keyOf).sort(), [items]);
  const pending = items.filter((row) => row.state === "pending");
  const approved = useApprovedDefinitions(server.id, wanted);
  const [approving, setApproving] = useState<McpDefinition | null>(null);
  const [rejecting, setRejecting] = useState<McpDefinition | null>(null);
  const approve = useMut((row: McpDefinition) => api.approveMcpDefinition(server.id, row.id), "Definition approved");
  const reject = useMut((row: McpDefinition) => api.rejectMcpDefinition(server.id, row.id), "Definition rejected");

  return (
    <Section title="Definitions" hint="Every tool and prompt a gateway has seen on this server; a definition the upstream changed stays withheld until it is approved here.">
      <div className="max-w-64">
        <SelectField
          label="Show"
          value={state}
          options={STATES}
          onChange={(next) => {
            setState(next as McpDefinitionState | "all");
            setTrail([undefined]);
          }}
        />
      </div>

      <DataTable
        rows={items}
        loading={definitions.isPending}
        error={definitions.error}
        onRetry={() => void definitions.refetch()}
        rowKey={(row) => row.id}
        empty={state === "pending" ? "Nothing is waiting for review." : "No definitions in this state."}
        paging={{
          summary: `Page ${trail.length}${definitions.data?.next_cursor ? " · more" : ""}`,
          onPrevious: trail.length > 1 ? () => setTrail(trail.slice(0, -1)) : undefined,
          onNext: definitions.data?.next_cursor ? () => setTrail([...trail, definitions.data!.next_cursor!]) : undefined,
        }}
        columns={[
          { key: "kind", header: "Kind", cell: (row) => <Badge tone="info">{row.kind}</Badge> },
          { key: "name", header: "Name", cell: (row) => <code className="[overflow-wrap:anywhere]">{row.name}</code> },
          {
            key: "hash",
            header: "Hash",
            hideBelow: "sm",
            cell: (row) => (
              <code className="text-xs text-muted-foreground" title={row.hash}>
                {row.hash.slice(0, 12)}
              </code>
            ),
          },
          { key: "state", header: "State", cell: (row) => <Badge tone={STATE_TONE[row.state]}>{row.state}</Badge> },
          { key: "last_seen", header: "Last seen", hideBelow: "md", cell: (row) => <span className="text-muted-foreground">{fmtDateTime(row.last_seen_at)}</span> },
          {
            key: "decided",
            header: "Decided",
            hideBelow: "md",
            cell: (row) => <span className="text-muted-foreground">{row.decided_at ? fmtDateTime(row.decided_at) : "—"}</span>,
          },
          {
            key: "actions",
            header: "",
            cell: (row) =>
              row.state === "pending" ? (
                <div className="flex justify-end gap-1">
                  <Button type="button" variant="outline" size="sm" onClick={() => setApproving(row)}>
                    Approve
                  </Button>
                  <Button type="button" variant="ghost" size="sm" className="text-destructive hover:bg-destructive/10 hover:text-destructive" onClick={() => setRejecting(row)}>
                    Reject
                  </Button>
                </div>
              ) : null,
          },
        ]}
      />

      {pending.length > 0 && (
        <div className="flex flex-col gap-4">
          {pending.map((row) => (
            <PendingDiff key={row.id} row={row} approved={approved.data?.[keyOf(row)]} loading={approved.isPending} />
          ))}
        </div>
      )}

      <ConfirmDialog
        open={!!approving}
        title="Approve definition"
        confirmLabel="Approve"
        destructive={false}
        message={`Approve “${approving?.name}” (${approving?.kind})? Gateways serve this hash within a moment, and any other pending hash of the same name is dropped.`}
        onConfirm={() => approve.mutateAsync(approving!)}
        onClose={() => setApproving(null)}
      />
      <ConfirmDialog
        open={!!rejecting}
        title="Reject definition"
        confirmLabel="Reject"
        message={`Reject “${rejecting?.name}” (${rejecting?.kind})? The gateways never serve this hash, and a later report of the same hash stays rejected.`}
        onConfirm={() => reject.mutateAsync(rejecting!)}
        onClose={() => setRejecting(null)}
      />
    </Section>
  );
}

/**
 * One pending hash against the approved definition of the same name. A name with no approved definition has
 * nothing to compare with: its whole definition is new.
 */
function PendingDiff({ row, approved, loading }: { row: McpDefinition; approved: McpDefinition | undefined; loading: boolean }) {
  const pendingText = prettyJson(row.definition_json);
  const approvedText = approved ? prettyJson(approved.definition_json) : null;
  const diff = approvedText === null ? null : lineDiff(approvedText, pendingText);
  const rows: DiffRow[] | null = diff && condense(diff);
  const counts = diff && diffCounts(diff);
  const label = `${row.kind} “${row.name}”`;
  return (
    <div className="flex flex-col gap-2 rounded-lg border border-dashed p-3" data-testid={`diff-${row.kind}-${row.name}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-semibold">{label}</span>
        {approved && <span className="text-xs text-muted-foreground">against the approved {approved.hash.slice(0, 12)}</span>}
        {counts && (
          <span className="text-xs text-muted-foreground">
            +{counts.added} −{counts.removed}
          </span>
        )}
      </div>
      {loading && <p className="text-[13px] text-muted-foreground">Loading the approved definition…</p>}
      {!loading && approved === undefined && <p className="text-[13px] text-muted-foreground">No approved definition of this name, so every line is new. The hash is {row.hash.slice(0, 12)}.</p>}
      {!loading && approved !== undefined && approved.hash === row.hash && <p className="text-[13px] text-muted-foreground">The same hash is already approved.</p>}
      {!loading && approved !== undefined && approved.hash !== row.hash && rows === null && <p className="text-[13px] text-muted-foreground">These definitions are too large to diff here; compare the hashes.</p>}
      {rows ? (
        <Diff rows={rows} label={`${label} in ${row.kind} definitions`} />
      ) : (
        <pre className="max-h-72 overflow-auto rounded-md bg-secondary p-2 text-[12px] leading-5" aria-label={`${label} definition`} data-testid="definition-json">
          {pendingText}
        </pre>
      )}
    </div>
  );
}

function Diff({ rows, label }: { rows: DiffRow[]; label: string }) {
  return (
    <pre className="max-h-72 overflow-auto rounded-md bg-secondary p-2 text-[12px] leading-5" aria-label={label} data-testid="definition-diff">
      {rows.map((line, index) =>
        line.kind === "gap" ? (
          <span key={index} className="block text-muted-foreground/70">
            {`  … ${line.count} unchanged ${line.count === 1 ? "line" : "lines"}`}
          </span>
        ) : (
          <span
            key={index}
            className={cn(
              "block whitespace-pre-wrap [overflow-wrap:anywhere]",
              line.kind === "added" && "bg-success-bg/50 text-success",
              line.kind === "removed" && "bg-destructive/10 text-destructive",
            )}
          >
            {`${line.kind === "added" ? "+" : line.kind === "removed" ? "-" : " "} ${line.text}`}
          </span>
        ),
      )}
    </pre>
  );
}
