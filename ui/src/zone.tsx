import { useQuery } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

import { api, type Zone } from "./api";

/*
 * The zone the Admin UI is looking at. It is a per-tab choice: sessionStorage holds it for the tab, and
 * localStorage remembers the last switch so a new tab starts there. Both are conveniences and may throw
 * (private window, blocked storage); the zone then lasts until the page reloads.
 *
 * The default zone (`LUMEWARP_ZONE`) is the one the API marks `is_default`; before the list loads the
 * stored choice or `default` stands in.
 */

const KEY = "lumewarp.zone";
export const DEFAULT_ZONE = "default";

function read(storage: () => Storage): string | null {
  try {
    return storage().getItem(KEY) || null;
  } catch {
    return null;
  }
}
function write(storage: () => Storage, value: string) {
  try {
    storage().setItem(KEY, value);
  } catch {
    // Storage blocked: the choice lasts until the page reloads.
  }
}

/** The tab's stored choice, else the last switch in any tab. */
const initialName = () => read(() => sessionStorage) ?? read(() => localStorage);

let current = initialName() ?? DEFAULT_ZONE;

/** The zone id for code outside React (the resolved one once the zone list is known). */
export const currentZone = () => current;

/** The default zone's id among live zones. */
export function defaultZoneId(zones: Zone[] | undefined): string {
  const live = (zones ?? []).filter((z) => !z.archived_at);
  return live.find((z) => z.is_default)?.id ?? live[0]?.id ?? DEFAULT_ZONE;
}

const ZoneContext = createContext<{ chosen: string | null; choose: (id: string) => void } | null>(null);

export function ZoneProvider({ children }: { children: ReactNode }) {
  const [chosen, setChosen] = useState<string | null>(initialName);
  const choose = useCallback((id: string) => {
    current = id;
    write(() => sessionStorage, id);
    write(() => localStorage, id);
    setChosen(id);
  }, []);
  const value = useMemo(() => ({ chosen, choose }), [chosen, choose]);
  return <ZoneContext.Provider value={value}>{children}</ZoneContext.Provider>;
}

export const zonesKey = ["zones"] as const;

export interface ZoneState {
  /** The current zone's id; usable as a query key part and as `?zone=` even while the list loads. */
  id: string;
  /** The current zone's record, once the list is loaded. */
  zone: Zone | undefined;
  /** Live zones as the API returns them. */
  zones: Zone[];
  defaultId: string;
  isLoading: boolean;
  setZone: (id: string) => void;
}

/** The current zone and the live zone list. A chosen zone that is gone (archived) falls back to the default. */
export function useZone(): ZoneState {
  const ctx = useContext(ZoneContext);
  if (!ctx) throw new Error("useZone must be used inside ZoneProvider");
  const list = useQuery({ queryKey: zonesKey, queryFn: () => api.zones(), staleTime: 60_000 });
  const zones = useMemo(() => list.data ?? [], [list.data]);
  const defaultId = defaultZoneId(list.data);
  const known = ctx.chosen !== null && zones.some((z) => z.id === ctx.chosen);
  const id = list.data ? (known ? ctx.chosen! : defaultId) : (ctx.chosen ?? DEFAULT_ZONE);
  if (list.data) current = id;
  return { id, zone: zones.find((z) => z.id === id), zones, defaultId, isLoading: list.isPending, setZone: ctx.choose };
}

/** Tailwind classes per palette colour for the zone dot. Static strings so Tailwind sees them. */
export const ZONE_COLORS: Record<string, { dot: string; label: string }> = {
  gray: { dot: "bg-zinc-500", label: "Gray" },
  blue: { dot: "bg-blue-500", label: "Blue" },
  green: { dot: "bg-green-500", label: "Green" },
  amber: { dot: "bg-amber-500", label: "Amber" },
  red: { dot: "bg-red-500", label: "Red" },
  violet: { dot: "bg-violet-500", label: "Violet" },
};
export const zoneColor = (color: string | undefined) => ZONE_COLORS[color ?? "gray"] ?? ZONE_COLORS.gray;

/** The zone's colour dot and name (the id when the name is empty). */
export function ZoneBadge({ zone, muted = false }: { zone: Pick<Zone, "id" | "name" | "color">; muted?: boolean }) {
  return (
    <span className={`inline-flex items-center gap-1.5 ${muted ? "opacity-60" : ""}`} data-testid={`zone-badge-${zone.id}`}>
      <span className={`size-2.5 shrink-0 rounded-full ${zoneColor(zone.color).dot}`} aria-hidden="true" />
      <span className="truncate">{zone.name || zone.id}</span>
    </span>
  );
}
