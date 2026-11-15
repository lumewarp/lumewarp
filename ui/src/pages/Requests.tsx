import { useQuery } from "@tanstack/react-query";
import { useSearch } from "@tanstack/react-router";
import { ArrowRightIcon, CheckIcon, CopyIcon } from "lucide-react";
import { useState } from "react";

import { api, type RequestRow, type RequestsQuery, type Schemas } from "../api";
import { Badge, fmtUsd, PageHeader } from "../components/Bits";
import { NumberField, SelectField, TextField } from "../components/Form";
import { DataTable } from "../components/Table";
import { Button } from "../components/ui/button";
import { Card } from "../components/ui/card";
import { ComboboxField } from "../components/ui/combobox-field";
import { useCan, useCatalog, useMe, useNames, useTeamDirectory, useTeams } from "../hooks";
import { dayStart, shiftDay } from "../lib/days";
import type { RequestsSearch } from "../lib/requests";
import { localDayOf } from "./keyText";

const KINDS: { value: Schemas["TrafficKind"]; label: string }[] = [
  { value: "llm", label: "LLM" },
  { value: "mcp", label: "MCP" },
  { value: "a2a", label: "A2A" },
];

const OUTCOMES: { value: Schemas["RequestOutcome"] | ""; label: string }[] = [
  { value: "", label: "Any outcome" },
  { value: "completed", label: "Completed" },
  { value: "client_cancelled", label: "Client cancelled" },
  { value: "upstream_error", label: "Upstream error" },
  { value: "rejected", label: "Rejected" },
  { value: "unspecified", label: "Unspecified" },
];

const TONE: Record<Schemas["RequestOutcome"], "ok" | "warn" | "danger" | "muted"> = {
  completed: "ok",
  client_cancelled: "muted",
  upstream_error: "danger",
  rejected: "warn",
  unspecified: "muted",
};

const outcomeLabel = (outcome: Schemas["RequestOutcome"]) => OUTCOMES.find((item) => item.value === outcome)?.label ?? outcome.replaceAll("_", " ");

/** A short local timestamp; the table's main column is the name chain, not the date. */
const fmtWhen = (iso: string) => new Date(iso).toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" });

interface Filters {
  kind: Schemas["TrafficKind"];
  teamId: string;
  keyId: string;
  /** `user:<id>` or `sa:<id>`; empty means every owner. */
  owner: string;
  requested: string;
  model: string;
  alias: string;
  router: string;
  status: number | null;
  outcome: Schemas["RequestOutcome"] | "";
  from: string;
  to: string;
  requestId: string;
}

/** A URL day or ISO instant as the date input's local day. */
const dayOf = (value: string | undefined) => {
  if (!value) return "";
  return /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : localDayOf(value);
};

const initialFilters = (search: RequestsSearch): Filters => ({
  kind: search.kind === "mcp" || search.kind === "a2a" ? search.kind : "llm",
  teamId: search.team_id ?? "",
  keyId: search.key_id ?? "",
  owner: search.user_id ? `user:${search.user_id}` : search.service_account_id ? `sa:${search.service_account_id}` : "",
  requested: search.requested ?? "",
  model: search.model ?? "",
  alias: search.alias ?? "",
  router: search.router ?? "",
  status: search.status && /^\d+$/.test(search.status) ? Number(search.status) : null,
  outcome: OUTCOMES.some((item) => item.value !== "" && item.value === search.outcome) ? (search.outcome as Schemas["RequestOutcome"]) : "",
  from: dayOf(search.from),
  to: dayOf(search.to),
  requestId: search.request_id ?? "",
});

const trimmed = (value: string) => value.trim() || undefined;

/** The API query: the day range is inclusive on screen and exclusive at its end, as on Usage. */
function queryOf(filters: Filters, cursor: string | undefined): RequestsQuery {
  return {
    kind: filters.kind,
    team_id: filters.teamId || undefined,
    key_id: filters.keyId || undefined,
    user_id: filters.owner.startsWith("user:") ? filters.owner.slice("user:".length) : undefined,
    service_account_id: filters.owner.startsWith("sa:") ? filters.owner.slice("sa:".length) : undefined,
    requested: trimmed(filters.requested),
    model: trimmed(filters.model),
    alias: trimmed(filters.alias),
    router: trimmed(filters.router),
    status: filters.status ?? undefined,
    outcome: filters.outcome || undefined,
    request_id: trimmed(filters.requestId),
    from: filters.from ? dayStart(filters.from) || undefined : undefined,
    to: filters.to ? dayStart(shiftDay(filters.to, 1)) || undefined : undefined,
    cursor,
  };
}

/**
 * The request log: one row per gateway request, newest first. It is not the audit log (administrative
 * changes) and stores no prompt or response content; the request id finds the details in OTel.
 */
export function Requests() {
  const search = useSearch({ strict: false }) as RequestsSearch;
  const [filters, setFilters] = useState<Filters>(() => initialFilters(search));
  const [trail, setTrail] = useState<(string | undefined)[]>([undefined]);
  const cursor = trail[trail.length - 1];
  const teams = useTeams();
  const perms = useCan();
  const me = useMe();
  const catalog = useCatalog();
  const administered = (teams.data ?? []).filter((team) => perms.teamAdmin(team.id));
  const directoryEnabled = administered.length > 0;
  const directory = useTeamDirectory(administered, directoryEnabled);
  const teamName = useNames(teams.data);
  const memberName = useNames(directory.data?.members.map((member) => ({ id: member.user_id, name: member.email })));
  const accountName = useNames(directory.data?.serviceAccounts);

  const keys = useQuery({
    queryKey: ["team-keys", filters.teamId],
    queryFn: () => api.teamKeys(filters.teamId),
    enabled: !!filters.teamId,
  });
  const ownersEnabled = !!filters.teamId && perms.teamAdmin(filters.teamId);
  const members = useQuery({
    queryKey: ["members", filters.teamId],
    queryFn: () => api.members(filters.teamId),
    enabled: ownersEnabled,
  });
  const accounts = useQuery({
    queryKey: ["service-accounts", filters.teamId],
    queryFn: () => api.serviceAccounts(filters.teamId),
    enabled: ownersEnabled,
  });

  const query = useQuery({
    queryKey: ["requests", filters, cursor],
    queryFn: () => api.requests(queryOf(filters, cursor)),
  });

  const set = (patch: Partial<Filters>) =>
    setFilters((current) => {
      const next = { ...current, ...patch };
      // A team changes what its keys and owners mean; the filters built for the old team do not carry.
      if (patch.teamId !== undefined && patch.teamId !== current.teamId) {
        next.keyId = "";
        next.owner = "";
      }
      return next;
    });
  const change = (patch: Partial<Filters>) => {
    set(patch);
    setTrail([undefined]);
  };

  const next = query.data?.next_cursor ?? undefined;
  const keyOptions = [
    { value: "", label: "All keys" },
    ...(keys.data ?? []).map((key) => ({
      value: key.id,
      label:
        key.owner_kind === "service_account"
          ? `${key.service_account_name ?? "Service account"} / ${key.name}`
          : `${key.name} · ${key.display_prefix}…`,
    })),
    ...(filters.keyId && !keys.data?.some((key) => key.id === filters.keyId)
      ? [{ value: filters.keyId, label: "Selected key" }]
      : []),
  ];
  const ownerOptions = [
    { value: "", label: "All owners" },
    ...(members.data ?? []).map((member) => ({ value: `user:${member.user_id}`, label: member.email })),
    ...(accounts.data ?? []).map((account) => ({ value: `sa:${account.id}`, label: `Service account: ${account.name}` })),
    ...(filters.owner && !ownersEnabled ? [{ value: filters.owner, label: "Selected owner" }] : []),
  ];
  const names = (kind: "model" | "alias" | "router") => (catalog.data ?? []).filter((entry) => entry.kind === kind).map((entry) => entry.name);
  const requestedNames = [...new Set((catalog.data ?? []).map((entry) => entry.name))];

  const ownerLabel = (row: RequestRow) => {
    if (row.owner_kind === "service_account") return accountName(row.owner_id);
    if (row.owner_id === me.data?.user_id) return me.data.email ?? "You";
    return directoryEnabled ? memberName(row.owner_id) : row.owner_id.slice(0, 8);
  };
  const personName = (id: string) =>
    id === me.data?.user_id ? (me.data.email ?? "You") : directoryEnabled ? memberName(id) : id.slice(0, 8);

  return (
    <>
      <PageHeader title="Requests" hint="No prompt or response content is stored; copy a request id to look it up in OTel." />
      <Card className="mb-4 grid grid-cols-[repeat(auto-fit,minmax(180px,1fr))] gap-3 p-4 shadow-none">
        <SelectField label="Kind" value={filters.kind} onChange={(value) => change({ kind: value as Schemas["TrafficKind"] })} options={KINDS} />
        <ComboboxField
          label="Team"
          value={filters.teamId}
          onChange={(value) => change({ teamId: value })}
          options={[{ value: "", label: "All teams" }, ...(teams.data ?? []).map((team) => ({ value: team.id, label: team.name }))]}
        />
        <ComboboxField
          label="Key"
          value={filters.keyId}
          onChange={(value) => change({ keyId: value })}
          options={keyOptions}
          disabled={!filters.teamId}
          note={!filters.teamId ? "Choose a team first." : keys.isPending ? "Loading keys…" : undefined}
        />
        {ownersEnabled && <ComboboxField label="Owner" value={filters.owner} onChange={(value) => change({ owner: value })} options={ownerOptions} />}
        <ComboboxField free label="Requested" value={filters.requested} onChange={(value) => change({ requested: String(value) })} options={requestedNames.map((name) => ({ value: name, label: name }))} placeholder="Any name" />
        <ComboboxField free label="Model" value={filters.model} onChange={(value) => change({ model: String(value) })} options={names("model").map((name) => ({ value: name, label: name }))} placeholder="Any model" />
        <ComboboxField free label="Alias" value={filters.alias} onChange={(value) => change({ alias: String(value) })} options={names("alias").map((name) => ({ value: name, label: name }))} placeholder="Any alias" />
        <ComboboxField free label="Router" value={filters.router} onChange={(value) => change({ router: String(value) })} options={names("router").map((name) => ({ value: name, label: name }))} placeholder="Any router" />
        <NumberField label="Status" value={filters.status} onChange={(value) => change({ status: value })} min={100} placeholder="Any status" />
        <SelectField label="Outcome" value={filters.outcome} onChange={(value) => change({ outcome: value as Filters["outcome"] })} options={OUTCOMES} />
        <TextField label="From" type="date" value={filters.from} onChange={(value) => change({ from: value })} note="Empty starts 24 hours ago." />
        <TextField label="To (inclusive)" type="date" value={filters.to} onChange={(value) => change({ to: value })} note="Empty ends now." />
        <TextField label="Request id" value={filters.requestId} onChange={(value) => change({ requestId: value })} placeholder="Full request id" hint="The id shown under the time of a request." />
      </Card>
      <DataTable
        rows={query.data?.items}
        loading={query.isPending || teams.isPending || (directoryEnabled && directory.isPending)}
        error={query.error ?? teams.error ?? (directoryEnabled ? directory.error : null)}
        onRetry={() => void query.refetch()}
        rowKey={(row) => row.request_id}
        empty="No requests in this range."
        paging={{
          summary: `Page ${trail.length}${next ? " · more" : ""}`,
          onPrevious:
            trail.length > 1 && !query.isFetching
              ? () => setTrail((current) => current.slice(0, -1))
              : undefined,
          onNext:
            next && !query.isFetching
              ? () => setTrail((current) => [...current, next])
              : undefined,
        }}
        columns={[
          { key: "chain", header: "Called → Served", cell: (row) => <Chain row={row} /> },
          {
            key: "when",
            header: "When",
            hideBelow: "md",
            cell: (row) => (
              <span className="flex flex-col gap-0.5 text-xs text-muted-foreground">
                <span className="whitespace-nowrap">{fmtWhen(row.ts)}</span>
                <RequestId id={row.request_id} />
              </span>
            ),
          },
          {
            key: "status",
            header: "Status",
            cell: (row) => (
              <span className="flex flex-col items-start gap-0.5">
                <Badge tone={TONE[row.outcome]}>{outcomeLabel(row.outcome)}</Badge>
                <span className="text-xs text-muted-foreground tabular-nums">HTTP {row.status}</span>
                {row.playground_user_id && (
                  <span className="flex flex-wrap items-center gap-1 text-xs text-muted-foreground" data-testid="playground-badge">
                    <Badge tone="info">Playground</Badge>
                    {personName(row.playground_user_id)}
                  </span>
                )}
              </span>
            ),
          },
          {
            key: "traffic",
            header: "Traffic",
            hideBelow: "sm",
            cell: (row) => (
              <span className="flex flex-col gap-0.5 text-muted-foreground">
                <span>
                  {row.attempts} {row.attempts === 1 ? "attempt" : "attempts"}
                </span>
                <span>{row.stream ? "Streamed" : "Buffered"}</span>
              </span>
            ),
          },
          {
            key: "tokens",
            header: "Tokens",
            align: "right",
            hideBelow: "sm",
            cell: (row) => (
              <span title={`${row.prompt_tokens.toLocaleString()} prompt + ${row.completion_tokens.toLocaleString()} completion`}>
                {(row.prompt_tokens + row.completion_tokens).toLocaleString()}
              </span>
            ),
          },
          {
            key: "cost",
            header: "Cost",
            align: "right",
            cell: (row) => (
              <span title={row.usage_state === "reported" ? undefined : `Usage was ${row.usage_state}: cost may be partial.`}>
                {fmtUsd(row.cost_nano_usd)}
              </span>
            ),
          },
          {
            key: "attribution",
            header: "Attribution",
            hideBelow: "md",
            cell: (row) => (
              <span className="flex flex-col gap-0.5 text-xs">
                <span className="font-medium whitespace-nowrap">{teamName(row.team_id)}</span>
                <span className="[overflow-wrap:anywhere]" title={ownerLabel(row)}>
                  {ownerLabel(row)}
                </span>
                <code className="text-muted-foreground" title={row.key_id}>
                  {row.key_id.slice(0, 8)}…
                </code>
              </span>
            ),
          },
        ]}
      />
    </>
  );
}

/** The name chain: what the client called, any alias/router, then the model and upstream that served it. */
function Chain({ row }: { row: RequestRow }) {
  const arrow = <ArrowRightIcon className="size-3 shrink-0 text-muted-foreground" aria-hidden="true" />;
  const called = row.requested || row.endpoint || "—";
  const served = row.model || "—";
  const step = (label: string, value: string) => (
    <span className="inline-flex min-w-0 items-center gap-0.5">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className="[overflow-wrap:anywhere]">{value}</span>
    </span>
  );
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-1 text-sm" data-testid="request-chain">
      <strong className="[overflow-wrap:anywhere]">{called}</strong>
      {row.alias && (
        <>
          {arrow}
          {step("alias:", row.alias)}
        </>
      )}
      {row.router && (
        <>
          {arrow}
          {step("router:", row.router)}
        </>
      )}
      {row.fallback && <Badge tone="warn">fallback</Badge>}
      {arrow}
      <strong className="[overflow-wrap:anywhere]">{served}</strong>
      {row.upstream && (
        <span className="inline-flex min-w-0 items-baseline gap-1 text-xs text-muted-foreground">
          @
          <code className="[overflow-wrap:anywhere]">
            {row.upstream}/{row.upstream_model || "—"}
          </code>
        </span>
      )}
    </div>
  );
}

/** The gateway request id, short on screen and copyable for an OTel lookup. */
function RequestId({ id }: { id: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(id);
      setCopied(true);
    } catch {
      // Clipboard blocked: the id is still selectable through its title.
    }
  };
  return (
    <span className="inline-flex items-center gap-1">
      <code className="text-xs text-muted-foreground" title={id}>
        {id.slice(0, 8)}…
      </code>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        className="size-6"
        aria-label={copied ? "Copied request id" : "Copy request id"}
        title={id}
        onClick={() => void copy()}
      >
        {copied ? <CheckIcon aria-hidden="true" /> : <CopyIcon aria-hidden="true" />}
      </Button>
    </span>
  );
}

