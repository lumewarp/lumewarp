import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { ArrowUpRightIcon, ChevronDownIcon, TriangleAlertIcon } from "lucide-react";
import { useState } from "react";

import { api, type StatusItem, type Zone } from "../api";
import { useCan } from "../hooks";
import { clock, stateRank, stateTone } from "../lib/incidents";
import { useZone, ZoneBadge } from "../zone";
import { Badge } from "./Bits";
import { Button } from "./ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "./ui/tooltip";

/** The banner's colours follow the worst state: red while something is down, amber otherwise. */
const BANNER_CLASS: Record<"danger" | "warn" | "muted", string> = {
  danger: "border-b bg-destructive/10 px-4 py-2.5 text-sm text-destructive md:px-6",
  warn: "border-b bg-warning-bg px-4 py-2.5 text-sm text-warning md:px-6",
  muted: "border-b bg-warning-bg px-4 py-2.5 text-sm text-warning md:px-6",
};

type ZoneRef = Pick<Zone, "id" | "name" | "color">;

function Since({ iso }: { iso: string }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="whitespace-nowrap opacity-80" tabIndex={0}>
          since {clock(iso)}
        </span>
      </TooltipTrigger>
      <TooltipContent>{new Date(iso).toLocaleString()}</TooltipContent>
    </Tooltip>
  );
}

function IncidentLink({ item }: { item: StatusItem }) {
  if (!item.incident_id) return null;
  return (
    <Link to="/incidents/$incidentId" params={{ incidentId: item.incident_id }} className="font-medium underline underline-offset-2" data-testid="status-incident-link">
      Incident
    </Link>
  );
}

/** One affected name on a single line; it is the banner's lead, and only its text is a live region, not the link. */
function Line({ item, zone, incidents }: { item: StatusItem; zone: ZoneRef | null; incidents: boolean }) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1" data-testid="status-row" data-name={item.name}>
      <span role="status" className="flex min-w-0 flex-1 flex-wrap items-center gap-x-3 gap-y-1">
        <Badge tone={stateTone(item.state)}>{item.state}</Badge>
        <span className="font-mono font-medium [overflow-wrap:anywhere]">{item.name}</span>
        {zone && (
          <span className="inline-flex items-center gap-1 opacity-80">
            in <ZoneBadge zone={zone} />
          </span>
        )}
        {item.summary && <span className="min-w-0 flex-1 basis-64 [overflow-wrap:anywhere]">{item.summary}</span>}
        <Since iso={item.since} />
      </span>
      {incidents && <IncidentLink item={item} />}
    </div>
  );
}

/** A row of the expanded list: state, name, then zone, since and the incident, in fixed columns so rows line up. Not a live region; the lead announces changes. */
function GridRow({ item, zone, incidents }: { item: StatusItem; zone: ZoneRef; incidents: boolean }) {
  return (
    <li className="grid min-w-0 grid-cols-[5.5rem_minmax(0,1fr)_auto] items-center gap-x-3" data-testid="status-row" data-name={item.name}>
      <span className="justify-self-start">
        <Badge tone={stateTone(item.state)}>{item.state}</Badge>
      </span>
      <span className="truncate font-mono" title={item.name}>
        {item.name}
      </span>
      <span className="inline-flex items-center gap-1.5 whitespace-nowrap text-xs opacity-80">
        <ZoneBadge zone={zone} />
        <span aria-hidden="true">·</span>
        <Since iso={item.since} />
        {incidents && item.incident_id && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Link
                to="/incidents/$incidentId"
                params={{ incidentId: item.incident_id }}
                className="ml-1 inline-flex size-6 items-center justify-center rounded text-current hover:bg-current/10"
                aria-label={`Incident for ${item.name}`}
                data-testid="status-incident-link"
              >
                <ArrowUpRightIcon className="size-3.5" aria-hidden="true" />
              </Link>
            </TooltipTrigger>
            <TooltipContent>Open the incident</TooltipContent>
          </Tooltip>
        )}
      </span>
    </li>
  );
}

/**
 * Under the header on every signed-in page: the names the caller can call that an alert says are down, congested or
 * degraded. One name is one line; several fold behind the worst state's summary and a "N names affected" toggle.
 * Polled every 10 seconds and on window focus. It renders nothing while the list is empty and nothing when the request
 * fails: a missing banner is not an error worth a message.
 */
export function StatusBanner() {
  const perms = useCan();
  const zone = useZone();
  const [open, setOpen] = useState(false);
  const status = useQuery({
    queryKey: ["status"],
    queryFn: api.status,
    refetchInterval: 10_000,
    refetchOnWindowFocus: true,
    retry: false,
  });
  const items = status.data ?? [];
  if (status.isError || items.length === 0) return null;
  const zoneOf = (id: string): ZoneRef => zone.zones.find((z) => z.id === id) ?? { id, name: id, color: "gray" };
  const showZone = zone.zones.length > 1;
  const ordered = [...items].sort((a, b) => stateRank(a.state) - stateRank(b.state) || Date.parse(a.since) - Date.parse(b.since) || a.name.localeCompare(b.name));
  const worst = ordered[0];
  const incidents = perms.isInstallationAdmin;
  // One name in two zones is one name affected.
  const names = new Set(items.map((item) => item.name)).size;
  return (
    <div className={BANNER_CLASS[stateTone(worst.state)]} data-testid="status-banner">
      <div className="mx-auto flex w-full max-w-[1200px] items-start gap-3">
        <TriangleAlertIcon className="mt-1 size-4 shrink-0" aria-hidden="true" />
        <div className="grid min-w-0 flex-1 gap-2">
          {items.length === 1 ? (
            <Line item={worst} zone={showZone ? zoneOf(worst.zone) : null} incidents={incidents} />
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <span role="status" className="flex min-w-0 flex-1 flex-wrap items-center gap-x-3 gap-y-1" data-testid="status-lead">
                  <Badge tone={stateTone(worst.state)}>{worst.state}</Badge>
                  <span className="min-w-0 flex-1 basis-64 [overflow-wrap:anywhere]">{worst.summary ?? `${worst.name} is ${worst.state}.`}</span>
                </span>
                <Button type="button" variant="outline" size="sm" className="h-7 border-current/30 bg-transparent text-current hover:bg-current/10 hover:text-current" aria-expanded={open} onClick={() => setOpen((value) => !value)} data-testid="status-count">
                  {names} {names === 1 ? "name" : "names"} affected
                  <ChevronDownIcon className={open ? "size-3.5 rotate-180 transition-transform" : "size-3.5 transition-transform"} aria-hidden="true" />
                </Button>
              </div>
              {open && (
                <ul className="m-0 grid max-h-[40vh] list-none overflow-y-auto grid-cols-[repeat(auto-fill,minmax(26rem,1fr))] gap-x-8 gap-y-1 border-t border-current/20 p-0 pt-2">
                  {ordered.map((item) => (
                    <GridRow key={`${item.zone}/${item.name}`} item={item} zone={zoneOf(item.zone)} incidents={incidents} />
                  ))}
                </ul>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
