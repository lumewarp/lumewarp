import { useMemo } from "react";

import type { CatalogEntry, CatalogKind, ModelAccess } from "../api";
import { useCatalog } from "../hooks";
import { ModelPill } from "./Bits";
import { Checkbox } from "./ui/checkbox";
import { HelpTip } from "./ui/help-tip";
import { Label } from "./ui/label";
import { RadioGroup, RadioGroupItem } from "./ui/radio-group";
import { Skeleton } from "./ui/skeleton";

const GROUPS: { kind: CatalogKind; title: string }[] = [
  { kind: "model", title: "Models" },
  { kind: "alias", title: "Aliases" },
  { kind: "router", title: "Routers" },
];

interface Props {
  isTeam?: boolean;
  access: ModelAccess;
  onAccess: (a: ModelAccess) => void;
  selected: string[];
  onSelected: (names: string[]) => void;
  /** Names the parent level allows; null = every name. Members and service accounts may only narrow it. */
  scope: string[] | null;
  error?: string;
}

/**
 * Who may call what, by name: team = All | Selected, member / service account = Inherit | Selected.
 * Selected shows every model, alias and router the parent level allows as a checkbox.
 */
export function PermissionPicker({ isTeam, access, onAccess, selected, onSelected, scope, error }: Props) {
  // Only fetched once there is something to pick from.
  const catalog = useCatalog(access === "list");
  const inScope = useMemo(() => (scope === null ? null : new Set(scope)), [scope]);
  const entries = useMemo(() => (catalog.data ?? []).filter((e) => inScope === null || inScope.has(e.name)), [catalog.data, inScope]);
  const known = new Set((catalog.data ?? []).map((e) => e.name));
  // Names already chosen that are no longer offered (left the team's scope or were deleted) stay visible to be unchecked.
  const stale = selected.filter((n) => !entries.some((e) => e.name === n));
  const first = isTeam ? ({ value: "all", label: "All", hint: "Every name, including ones added later." } as const) : ({ value: "inherit", label: "Inherit", hint: "Whatever the team allows." } as const);
  const toggle = (name: string, on: boolean) => onSelected(on ? [...selected, name] : selected.filter((n) => n !== name));
  return (
    <fieldset className="flex min-w-0 flex-col gap-2" data-testid="permission-picker">
      <legend className="mb-1 text-sm font-semibold">
        <span className="flex items-center gap-1">
          Model access
          <HelpTip label="Model access" text={access === "list" ? isTeam ? "Only the names ticked below." : "Only the names ticked below, within what the team allows; none ticked denies everything." : first.hint} />
        </span>
      </legend>
      <RadioGroup value={access === "list" ? "list" : first.value} onValueChange={(v) => onAccess(v as ModelAccess)} aria-label="Model access" className="flex flex-wrap gap-x-5 gap-y-2" aria-invalid={!!error}>
        {[first, { value: "list", label: "Selected", hint: "" } as const].map((o) => (
          <div key={o.value} className="flex items-center gap-2">
            <RadioGroupItem value={o.value} id={`access-${o.value}`} />
            <Label htmlFor={`access-${o.value}`} className="cursor-pointer font-normal">
              {o.label}
            </Label>
          </div>
        ))}
      </RadioGroup>
      {access === "list" && (
        <div className="flex max-h-72 flex-col gap-3 overflow-y-auto rounded-md border p-3" data-testid="permission-options">
          {catalog.isPending && <Skeleton className="h-4 w-full" />}
          {catalog.error && (
            <p className="text-[13px] text-destructive" role="alert">
              {catalog.error.message}
            </p>
          )}
          {!catalog.isPending && !catalog.error && entries.length === 0 && stale.length === 0 && <p className="text-sm text-muted-foreground">No models, aliases or routers {isTeam ? "exist yet" : "are available in this team"}.</p>}
          {GROUPS.map(({ kind, title }) => {
            const items = entries.filter((e: CatalogEntry) => e.kind === kind);
            if (!items.length) return null;
            return (
              <div key={kind} role="group" aria-label={title} className="flex flex-col gap-1.5">
                <span className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">{title}</span>
                <div className="grid gap-1.5 sm:grid-cols-2">
                  {items.map((e) => (
                    <div key={e.id} className="flex items-center gap-2">
                      <Checkbox id={`perm-${e.id}`} checked={selected.includes(e.name)} onCheckedChange={(c) => toggle(e.name, c === true)} />
                      <Label htmlFor={`perm-${e.id}`} className="min-w-0 cursor-pointer font-normal">
                        {e.model_type ? <ModelPill name={e.name} type={e.model_type} /> : <span className="[overflow-wrap:anywhere]">{e.name}</span>}
                      </Label>
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
          {stale.length > 0 && (
            <div role="group" aria-label="Not offered" className="flex flex-col gap-1.5">
              <span className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">Not offered</span>
              {stale.map((n) => (
                <div key={n} className="flex items-center gap-2">
                  <Checkbox id={`perm-stale-${n}`} checked onCheckedChange={(c) => toggle(n, c === true)} />
                  <Label htmlFor={`perm-stale-${n}`} className="cursor-pointer font-normal [overflow-wrap:anywhere]">
                    {n} <span className="text-muted-foreground">{known.has(n) ? "(outside the team's scope)" : "(no longer exists)"}</span>
                  </Label>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
      {error && (
        <p className="text-[13px] text-destructive" role="alert">
          {error}
        </p>
      )}
    </fieldset>
  );
}
