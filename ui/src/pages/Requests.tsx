import { useQuery } from "@tanstack/react-query";
import { useSearch } from "@tanstack/react-router";
import { CheckIcon, CopyIcon, SlidersHorizontalIcon } from "lucide-react";
import { useState, type MouseEvent } from "react";

import { api, type RequestRow, type RequestsQuery, type Schemas } from "../api";
import { Badge, fmtUsd, PageHeader } from "../components/Bits";
import { NumberField, SelectField, TextField } from "../components/Form";
import { DataTable } from "../components/Table";
import { Button } from "../components/ui/button";
import { Badge as UiBadge } from "../components/ui/badge";
import { Card } from "../components/ui/card";
import { ComboboxField } from "../components/ui/combobox-field";
import { Popover, PopoverContent, PopoverTrigger } from "../components/ui/popover";
import { useCan, useCatalog, useMe, useNames, useTeamDirectory, useTeams } from "../hooks";
import { dayStart, shiftDay } from "../lib/days";
import type { RequestsSearch } from "../lib/requests";
import { localDayOf } from "./keyText";
import { OUTCOME_TONE, outcomeLabel, RequestDetail, StatusBadge } from "./RequestDetail";
import { KeyPill } from "../components/KeyPill";

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

/** A short local timestamp. */
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
  const [selected, setSelected] = useState<RequestRow | null>(null);
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

  const extraActive = [
    filters.kind !== "llm",
    !!filters.owner,
    !!filters.alias,
    !!filters.router,
    filters.status !== null,
    !!filters.outcome,
    !!filters.requestId,
  ].filter(Boolean).length;
  const clearExtra = () =>
    change({ kind: "llm", owner: "", alias: "", router: "", status: null, outcome: "", requestId: "" });

  return (
    <>
      <PageHeader title="Requests" hint="No prompt or response content is stored; copy a request id to look it up in OTel." />
      <Card className="mb-4 grid grid-cols-2 items-end gap-2 p-3 shadow-none sm:grid-cols-3 lg:grid-cols-[10rem_10rem_repeat(4,minmax(0,1fr))_auto]" data-testid="request-filters">
        <TextField label="From" type="date" value={filters.from} onChange={(value) => change({ from: value })} hint="Empty starts 24 hours ago." />
        <TextField label="To" type="date" value={filters.to} onChange={(value) => change({ to: value })} hint="Inclusive; empty ends now." />
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
          placeholder={!filters.teamId ? "Choose a team first" : undefined}
        />
        <ComboboxField free label="Requested" value={filters.requested} onChange={(value) => change({ requested: String(value) })} options={requestedNames.map((name) => ({ value: name, label: name }))} placeholder="Any name" />
        <ComboboxField free label="Resolved" value={filters.model} onChange={(value) => change({ model: String(value) })} options={names("model").map((name) => ({ value: name, label: name }))} placeholder="Any model" />
        <Popover>
          <PopoverTrigger asChild>
            <Button variant="outline" className="col-span-2 sm:col-span-1">
              <SlidersHorizontalIcon aria-hidden="true" />
              More filters
              {extraActive > 0 && <UiBadge variant="secondary">{extraActive}</UiBadge>}
            </Button>
          </PopoverTrigger>
          <PopoverContent align="end" className="grid w-80 gap-3">
            <SelectField label="Kind" value={filters.kind} onChange={(value) => change({ kind: value as Schemas["TrafficKind"] })} options={KINDS} />
            {ownersEnabled && <ComboboxField label="Owner" value={filters.owner} onChange={(value) => change({ owner: value })} options={ownerOptions} />}
            <ComboboxField free label="Alias" value={filters.alias} onChange={(value) => change({ alias: String(value) })} options={names("alias").map((name) => ({ value: name, label: name }))} placeholder="Any alias" />
            <ComboboxField free label="Router" value={filters.router} onChange={(value) => change({ router: String(value) })} options={names("router").map((name) => ({ value: name, label: name }))} placeholder="Any router" />
            <div className="grid grid-cols-2 gap-3">
              <NumberField label="Status" value={filters.status} onChange={(value) => change({ status: value })} min={100} placeholder="Any" />
              <SelectField label="Outcome" value={filters.outcome} onChange={(value) => change({ outcome: value as Filters["outcome"] })} options={OUTCOMES} />
            </div>
            <TextField label="Request id" value={filters.requestId} onChange={(value) => change({ requestId: value })} placeholder="Full request id" />
            <Button variant="ghost" size="sm" onClick={clearExtra} disabled={extraActive === 0}>
              Clear these filters
            </Button>
          </PopoverContent>
        </Popover>
      </Card>
      <DataTable
        rows={query.data?.items}
        loading={query.isPending || teams.isPending || (directoryEnabled && directory.isPending)}
        error={query.error ?? teams.error ?? (directoryEnabled ? directory.error : null)}
        onRetry={() => void query.refetch()}
        rowKey={(row) => row.request_id}
        onRowClick={setSelected}
        selectedKey={selected?.request_id}
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
          {
            key: "date",
            header: "Date",
            cell: (row) => (
              <span className="whitespace-nowrap text-muted-foreground">{fmtWhen(row.ts)}</span>
            ),
          },
          {
            key: "requested",
            header: "Requested",
            cell: (row) => (
              <Button
                type="button"
                variant="link"
                className="h-auto p-0 text-left font-semibold whitespace-normal text-foreground [overflow-wrap:anywhere]"
                data-testid="request-requested"
                onClick={(event) => {
                  event.stopPropagation();
                  setSelected(row);
                }}
              >
                {row.requested || row.endpoint || "—"}
              </Button>
            ),
          },
          { key: "resolved", header: "Resolved", cell: (row) => <Resolved row={row} /> },
          {
            key: "status",
            header: "Status",
            cell: (row) => (
              <span className="flex flex-wrap items-center gap-1" data-testid="request-status">
                <StatusBadge status={row.status} />
                {row.outcome !== "completed" && <Badge tone={OUTCOME_TONE[row.outcome]}>{outcomeLabel(row.outcome)}</Badge>}
              </span>
            ),
          },
          {
            key: "key",
            header: "Key",
            hideBelow: "sm",
            cell: (row) => (
              <span className="flex flex-wrap items-center gap-1">
                <KeyPill keyRef={row.key} fallbackId={row.key_id} owner={row.owner_kind === "user" ? ownerLabel(row) : undefined} />
                {row.playground_user_id && (
                  <span data-testid="playground-badge">
                    <Badge tone="info">Playground</Badge>
                  </span>
                )}
              </span>
            ),
          },
          {
            key: "tokens",
            header: "Tokens",
            align: "right",
            hideBelow: "sm",
            cell: (row) => (
              <span className="whitespace-nowrap">
                {(row.prompt_tokens + row.completion_tokens).toLocaleString()}
                <span className="text-muted-foreground">
                  {" "}({row.prompt_tokens.toLocaleString()}+{row.completion_tokens.toLocaleString()})
                </span>
              </span>
            ),
          },
          {
            key: "cost",
            header: "Cost",
            align: "right",
            cell: (row) => fmtUsd(row.cost_nano_usd),
          },
        ]}
      />
      <RequestDetail
        row={selected}
        onClose={() => setSelected(null)}
        team={selected ? teamName(selected.team_id) : ""}
        owner={selected ? ownerLabel(selected) : ""}
        playgroundUser={selected?.playground_user_id ? personName(selected.playground_user_id) : null}
        requestId={selected ? <RequestId id={selected.request_id} /> : null}
      />
    </>
  );
}

/**
 * The name the request resolved to: the model that served it. How it got there is in the detail sheet.
 */
function Resolved({ row }: { row: RequestRow }) {
  return (
    <span className="flex flex-wrap items-center gap-1" data-testid="request-chain">
      <span className="[overflow-wrap:anywhere]">{row.model || "—"}</span>
      {row.fallback && <Badge tone="warn">fallback</Badge>}
    </span>
  );
}

/** Copies the gateway request id for an OTel lookup; shown in the detail sheet. */
function RequestId({ id }: { id: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async (event: MouseEvent) => {
    event.stopPropagation();
    try {
      await navigator.clipboard.writeText(id);
      setCopied(true);
    } catch {
      // Clipboard blocked: the id is shown in full in the detail sheet.
    }
  };
  return (
    <span className="inline-flex items-center">
      <Button
        type="button"
        variant="ghost"
        size="icon"
        className="size-6"
        aria-label={copied ? "Copied request id" : "Copy request id"}
        onClick={(event) => void copy(event)}
      >
        {copied ? <CheckIcon aria-hidden="true" /> : <CopyIcon aria-hidden="true" />}
      </Button>
    </span>
  );
}

