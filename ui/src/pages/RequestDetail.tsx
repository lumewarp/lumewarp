import { ArrowDownToLineIcon, BoxIcon, CornerDownRightIcon, GitForkIcon, ReplyIcon, ServerIcon, UndoDotIcon } from "lucide-react";
import type { ReactNode } from "react";

import type { RequestRow, Schemas } from "../api";
import { Badge, fmtUsd } from "../components/Bits";
import { DescriptionDetails, DescriptionList, DescriptionTerm } from "../components/ui/description-list";
import { HelpTip } from "../components/ui/help-tip";
import { Separator } from "../components/ui/separator";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "../components/ui/sheet";
import { endpointLabel } from "../lib/endpoints";
import { cn } from "../lib/utils";

/** A request's key: its name and its `lw…` display prefix, or a short id once the key is gone. */
export function KeyName({ row }: { row: RequestRow }) {
  if (!row.key_name) return <span className="[overflow-wrap:anywhere]">{row.key_id.slice(0, 8)}…</span>;
  return (
    <span className="inline-flex flex-wrap items-baseline gap-x-1.5">
      <span className="[overflow-wrap:anywhere]">{row.key_name}</span>
      {row.key_prefix && <code className="text-xs text-muted-foreground" data-testid="request-key-prefix">{row.key_prefix}…</code>}
    </span>
  );
}

type Tone = "ok" | "warn" | "danger" | "muted" | "info";

export const OUTCOME_TONE: Record<Schemas["RequestOutcome"], Tone> = {
  completed: "ok",
  client_cancelled: "muted",
  upstream_error: "danger",
  rejected: "warn",
  unspecified: "muted",
};

export const outcomeLabel = (outcome: Schemas["RequestOutcome"]) => {
  const label = outcome.replaceAll("_", " ");
  return label.charAt(0).toUpperCase() + label.slice(1);
};

const USAGE_STATE: Record<Schemas["UsageState"], string> = {
  reported: "Reported by the upstream",
  partial: "Partial: the stream ended before the usage report",
  missing: "Missing: the upstream reported none",
  invalid: "Invalid: the upstream's report was rejected",
  unspecified: "Unspecified",
};

const DOT: Record<Tone, string> = {
  ok: "bg-emerald-500",
  warn: "bg-amber-500",
  danger: "bg-destructive",
  muted: "bg-muted-foreground",
  info: "bg-primary",
};

interface Step {
  key: string;
  icon: typeof BoxIcon;
  label: string;
  name: ReactNode;
  detail?: ReactNode;
  tone?: Tone;
}

/** The route a request took, in order; a step the request never reached is left out. */
function steps(row: RequestRow): Step[] {
  const out: Step[] = [
    {
      key: "received",
      icon: ArrowDownToLineIcon,
      label: "Received",
      name: row.requested || "—",
      detail: (
        <>
          {row.endpoint ? endpointLabel(row.endpoint) : row.kind.toUpperCase()} · <KeyName row={row} />
          {row.stream ? " · streaming" : ""}
        </>
      ),
      tone: "info",
    },
  ];
  if (row.alias) out.push({ key: "alias", icon: CornerDownRightIcon, label: "Alias", name: row.alias });
  if (row.router) out.push({ key: "router", icon: GitForkIcon, label: "Router", name: row.router });
  if (row.fallback) out.push({ key: "fallback", icon: UndoDotIcon, label: "Fallback", name: "Served by the router's fallback", tone: "warn" });
  if (row.model) out.push({ key: "model", icon: BoxIcon, label: "Model", name: row.model });
  if (row.upstream)
    out.push({
      key: "upstream",
      icon: ServerIcon,
      label: "Upstream",
      name: row.upstream,
      detail: (
        <>
          {row.upstream_model || "—"}
          {row.attempts > 1 ? ` · ${row.attempts} attempts` : ""}
        </>
      ),
    });
  out.push({
    key: "response",
    icon: ReplyIcon,
    label: "Response",
    name: (
      <span className="inline-flex flex-wrap items-center gap-1">
        {row.status ? `HTTP ${row.status}` : "No status"}
        <Badge tone={OUTCOME_TONE[row.outcome]}>{outcomeLabel(row.outcome)}</Badge>
      </span>
    ),
    detail: `${row.duration_ms.toLocaleString()} ms`,
    tone: OUTCOME_TONE[row.outcome],
  });
  return out;
}

function Timeline({ items }: { items: Step[] }) {
  return (
    <ol className="m-0 list-none p-0" data-testid="request-timeline">
      {items.map((step, index) => {
        const Icon = step.icon;
        const last = index === items.length - 1;
        return (
          <li key={step.key} className="relative flex gap-3 pb-4 last:pb-0" data-step={step.key}>
            {!last && <span aria-hidden="true" className="absolute top-7 bottom-0 left-3.5 w-px bg-border" />}
            <span className="relative flex size-7 shrink-0 items-center justify-center rounded-full border bg-background">
              <Icon className="size-3.5 text-muted-foreground" aria-hidden="true" />
              <span aria-hidden="true" className={cn("absolute -right-0.5 -bottom-0.5 size-2 rounded-full ring-2 ring-background", DOT[step.tone ?? "muted"])} />
            </span>
            <div className="min-w-0 pt-0.5">
              <div className="text-xs uppercase tracking-[0.04em] text-muted-foreground">{step.label}</div>
              <div className="font-medium [overflow-wrap:anywhere]">{step.name}</div>
              {step.detail && <div className="text-sm text-muted-foreground [overflow-wrap:anywhere]">{step.detail}</div>}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

interface Props {
  row: RequestRow | null;
  onClose: () => void;
  team: string;
  owner: string;
  playgroundUser: string | null;
  requestId: ReactNode;
}

/** One request's details beside the log: the route it took, then who sent it and what it cost. */
export function RequestDetail({ row, onClose, team, owner, playgroundUser, requestId }: Props) {
  return (
    <Sheet open={!!row} onOpenChange={(open) => !open && onClose()}>
      <SheetContent
        className="w-full gap-0 overflow-y-auto sm:max-w-md"
        data-testid="request-detail"
        // Focusing the first control would open the route's help tooltip; focus the sheet itself.
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          (event.currentTarget as HTMLElement | null)?.focus();
        }}
      >
        {row && (
          <>
            <SheetHeader className="pr-10">
              <SheetTitle className="[overflow-wrap:anywhere]">{row.requested || row.endpoint || "Request"}</SheetTitle>
              <SheetDescription>{new Date(row.ts).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "medium" })}</SheetDescription>
            </SheetHeader>
            <div className="grid gap-5 px-4 pb-6">
              <section aria-labelledby="request-route" className="grid gap-3">
                <h3 id="request-route" className="m-0 inline-flex items-center gap-1 text-sm font-semibold">
                  Route
                  <HelpTip text="Per-attempt details are in OTel under this request id; prompts and responses are never stored." />
                </h3>
                <Timeline items={steps(row)} />
              </section>
              <Separator />
              <section aria-labelledby="request-usage" className="grid gap-3">
                <h3 id="request-usage" className="m-0 text-sm font-semibold">Usage</h3>
                <DescriptionList>
                  <DescriptionTerm>Tokens</DescriptionTerm>
                  <DescriptionDetails className="tabular-nums">
                    {(row.prompt_tokens + row.completion_tokens).toLocaleString()} ({row.prompt_tokens.toLocaleString()} in + {row.completion_tokens.toLocaleString()} out)
                  </DescriptionDetails>
                  {row.audio_ms > 0 && (
                    <>
                      <DescriptionTerm>Audio</DescriptionTerm>
                      <DescriptionDetails className="tabular-nums">{(row.audio_ms / 1000).toLocaleString()} s</DescriptionDetails>
                    </>
                  )}
                  <DescriptionTerm>Cost</DescriptionTerm>
                  <DescriptionDetails className="tabular-nums">{fmtUsd(row.cost_nano_usd)}</DescriptionDetails>
                  <DescriptionTerm>Usage report</DescriptionTerm>
                  <DescriptionDetails>{USAGE_STATE[row.usage_state]}</DescriptionDetails>
                  <DescriptionTerm>Duration</DescriptionTerm>
                  <DescriptionDetails className="tabular-nums">{row.duration_ms.toLocaleString()} ms</DescriptionDetails>
                </DescriptionList>
              </section>
              <Separator />
              <section aria-labelledby="request-sender" className="grid gap-3">
                <h3 id="request-sender" className="m-0 text-sm font-semibold">Sender</h3>
                <DescriptionList>
                  <DescriptionTerm>Key</DescriptionTerm>
                  <DescriptionDetails>
                    <KeyName row={row} />
                  </DescriptionDetails>
                  <DescriptionTerm>Team</DescriptionTerm>
                  <DescriptionDetails className="[overflow-wrap:anywhere]">{team}</DescriptionDetails>
                  <DescriptionTerm>{row.owner_kind === "service_account" ? "Service account" : "Owner"}</DescriptionTerm>
                  <DescriptionDetails className="[overflow-wrap:anywhere]">{owner}</DescriptionDetails>
                  {playgroundUser && (
                    <>
                      <DescriptionTerm>Playground</DescriptionTerm>
                      <DescriptionDetails className="[overflow-wrap:anywhere]" data-testid="request-detail-playground">
                        Sent by {playgroundUser}
                      </DescriptionDetails>
                    </>
                  )}
                  <DescriptionTerm>Request id</DescriptionTerm>
                  <DescriptionDetails className="inline-flex items-center gap-1 font-mono text-xs [overflow-wrap:anywhere]">
                    {row.request_id}
                    {requestId}
                  </DescriptionDetails>
                </DescriptionList>
              </section>
            </div>
          </>
        )}
      </SheetContent>
    </Sheet>
  );
}
