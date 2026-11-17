import { BoxesIcon, CheckIcon, ChevronsUpDownIcon, CornerDownRightIcon, LayersIcon, RouteIcon, type LucideIcon } from "lucide-react";
import { Fragment, useId, useMemo, useState, type ReactNode } from "react";

import { cn } from "@/lib/utils";
import type { CatalogEntry, CatalogKind } from "../api";
import { useCatalog } from "../hooks";
import { Field, type ControlProps } from "./Form";
import { TypeBadge } from "./Bits";
import { typeLabel } from "../lib/modelType";
import { MISSING_LABEL } from "./choice";
import { Button } from "./ui/button";
import { Checkbox } from "./ui/checkbox";
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList, CommandSeparator } from "./ui/command";
import { Input } from "./ui/input";
import { Label } from "./ui/label";
import { Popover, PopoverAnchor, PopoverContent, PopoverTrigger } from "./ui/popover";
import { Select, SelectContent, SelectGroup, SelectItem, SelectLabel, SelectSeparator, SelectTrigger, SelectValue } from "./ui/select";
import { Separator } from "./ui/separator";
import { Skeleton } from "./ui/skeleton";
import { Switch } from "./ui/switch";

/*
 * One set of pickers for every place that chooses a model, alias or router, so they all list the same
 * three kinds in the same order, with the same icons and the same heading per kind:
 *
 *   CatalogSelect       one choice from the catalog (shadcn Select; SelectGroup / SelectLabel / SelectSeparator)
 *   CatalogFilter       a filter that also accepts free text (CommandGroup heading / CommandSeparator)
 *   CatalogMultiSelect  a checkbox list with the same sections (policies, router members)
 *
 * The data is the catalog (`useCatalog`): kind, name, id and model type of every callable name.
 */

export type CatalogEntries = CatalogEntry[];

const KINDS: { kind: CatalogKind; title: string; icon: LucideIcon }[] = [
  { kind: "model", title: "Models", icon: BoxesIcon },
  { kind: "alias", title: "Aliases", icon: CornerDownRightIcon },
  { kind: "router", title: "Routers", icon: RouteIcon },
];
const ICON: Record<CatalogKind, LucideIcon> = {
  model: BoxesIcon,
  alias: CornerDownRightIcon,
  router: RouteIcon,
};

export interface CatalogFilterProps {
  /** Allowed kinds; every kind when unset. */
  kinds?: CatalogKind[];
  /** Only entries of this model type. */
  modelType?: CatalogEntry["model_type"];
  /** Only these names; null or unset is every name. */
  scope?: string[] | null;
  /** Names or ids never offered (for example the thing being edited). */
  exclude?: string[];
}

/** The catalog narrowed by the shared props, with its loading and error state. */
function useEntries({ kinds, modelType, scope, exclude }: CatalogFilterProps, enabled = true) {
  const catalog = useCatalog(enabled);
  const all = useMemo(() => catalog.data ?? [], [catalog.data]);
  const entries = useMemo(() => {
    const names = scope ? new Set(scope) : null;
    const skip = new Set(exclude ?? []);
    return all.filter((e) => (!kinds || kinds.includes(e.kind)) && (!modelType || e.model_type === modelType) && (!names || names.has(e.name)) && !skip.has(e.name) && !skip.has(e.id));
  }, [all, kinds, modelType, scope, exclude]);
  return {
    all,
    entries,
    pending: catalog.isPending && enabled,
    error: catalog.error,
  };
}

/** The entries of one kind, in the catalog's order sorted by name. */
const ofKind = (entries: CatalogEntries, kind: CatalogKind) => entries.filter((e) => e.kind === kind).sort((a, b) => a.name.localeCompare(b.name));

/** The kinds that have entries, each with its entries; empty kinds are skipped. */
function sections(entries: CatalogEntries) {
  return KINDS.map((k) => ({ ...k, items: ofKind(entries, k.kind) })).filter((s) => s.items.length > 0);
}

/** Icon, name and model type; the type is read out after the name, like a ModelPill. */
function EntryLabel({ entry, badge = true }: { entry: Pick<CatalogEntry, "kind" | "name" | "model_type">; badge?: boolean }) {
  const Icon = ICON[entry.kind];
  return (
    <>
      <Icon aria-hidden="true" className="size-3.5 shrink-0 text-muted-foreground" />
      <span className="min-w-0 truncate">{entry.name}</span>
      {entry.model_type && <span className="sr-only"> ({typeLabel(entry.model_type)})</span>}
      {badge && <EntryBadge entry={entry} />}
    </>
  );
}

function EntryBadge({ entry }: { entry: Pick<CatalogEntry, "model_type"> }) {
  if (!entry.model_type) return null;
  return (
    <span aria-hidden="true" className="shrink-0">
      <TypeBadge type={entry.model_type} />
    </span>
  );
}

const HEADING = "text-xs font-semibold tracking-wide text-muted-foreground uppercase";

interface FieldProps {
  label: string;
  hint?: string;
  note?: ReactNode;
  error?: string;
  placeholder?: string;
  disabled?: boolean;
}

/** What to say while the catalog loads or when it failed; undefined when there is nothing to say. */
function loadNote(pending: boolean, error: Error | null, what = "models, aliases and routers") {
  if (error) return `Could not load ${what}: ${error.message}`;
  if (pending) return `Loading ${what}…`;
  return undefined;
}

/* ------------------------------------------------------------------------------------------ CatalogSelect */

export interface CatalogSelectProps extends CatalogFilterProps, FieldProps {
  /** The chosen entry's id or name (`by`). Empty is nothing chosen. */
  value: string;
  onChange: (value: string, entry: CatalogEntry) => void;
  /** What `value` holds. Alias and router targets are ids; a name a key may call is a name. */
  by?: "id" | "name";
}

/** One choice from the catalog, one heading per kind. */
export function CatalogSelect({ label, hint, note, error, ...rest }: CatalogSelectProps) {
  const { pending, error: loadError } = useEntries(rest);
  return (
    <Field label={label} error={error} hint={hint} note={note ?? loadNote(pending, loadError)}>
      {(p) => <CatalogSelectControl {...rest} {...p} />}
    </Field>
  );
}

/** The select without label or message, for places that label it themselves (`aria-label`). */
export function CatalogSelectControl({
  placeholder = "Select…",
  disabled,
  value,
  onChange,
  by = "name",
  ...rest
}: Omit<CatalogSelectProps, "label" | "hint" | "note" | "error"> & Partial<ControlProps> & { "aria-label"?: string }) {
  const { kinds, modelType, scope, exclude, ...aria } = rest;
  const { entries, pending } = useEntries({ kinds, modelType, scope, exclude });
  const keyOf = (e: CatalogEntry) => (by === "id" ? e.id : e.name);
  const chosen = entries.find((e) => keyOf(e) === value);
  const groups = sections(entries);
  return (
    <Select
      value={value}
      onValueChange={(v) => {
        const e = entries.find((x) => keyOf(x) === v);
        if (e) onChange(v, e);
      }}
      disabled={disabled || (pending && !value)}
    >
      <SelectTrigger {...aria} className="w-full min-w-0">
        <SelectValue placeholder={placeholder}>{value && !chosen && !pending ? MISSING_LABEL : chosen ? <EntryLabel entry={chosen} /> : undefined}</SelectValue>
      </SelectTrigger>
      <SelectContent position="popper">
        {groups.map((g, i) => (
          <Fragment key={g.kind}>
            {i > 0 && <SelectSeparator />}
            <SelectGroup>
              <SelectLabel>{g.title}</SelectLabel>
              {g.items.map((e) => (
                <SelectItem key={e.id} value={keyOf(e)} textValue={e.name}>
                  <EntryLabel entry={e} />
                </SelectItem>
              ))}
            </SelectGroup>
          </Fragment>
        ))}
      </SelectContent>
    </Select>
  );
}

/* ------------------------------------------------------------------------------------------ CatalogFilter */

export interface CatalogFilterFieldProps extends CatalogFilterProps, FieldProps {
  value: string;
  onChange: (value: string) => void;
  /**
   * Free text: the typed text is the value, so a name that no longer exists can be filtered on, and the
   * catalog only completes it. Off, the value is one of the rows.
   */
  free?: boolean;
  /** The value of an entry; its name by default. Free text is always the name. */
  entryValue?: (entry: CatalogEntry) => string;
  /** Rows before the first heading that are not catalog names ("Any", "None"). */
  leading?: { value: string; label: string }[];
  searchLabel?: string;
  emptyText?: string;
}

const match = (_key: string, search: string, keywords?: string[]) => ((keywords?.join(" ") ?? "").toLowerCase().includes(search.trim().toLowerCase()) ? 1 : 0);

/** A filter on a name: the catalog grouped by kind, typed text allowed (`free`) or one of the rows. */
export function CatalogFilter({
  label,
  hint,
  note,
  error,
  placeholder,
  disabled,
  value,
  onChange,
  free = false,
  entryValue,
  leading,
  searchLabel,
  emptyText = "No match",
  ...filter
}: CatalogFilterFieldProps) {
  const { entries, pending, error: loadError } = useEntries(filter);
  const status = loadNote(pending, loadError);
  const of = entryValue ?? ((e: CatalogEntry) => e.name);
  return (
    <Field label={label} error={error} hint={hint} note={note ?? status}>
      {(p) =>
        free ? (
          <FreeCatalogInput {...p} value={value} onChange={onChange} entries={entries} placeholder={placeholder} disabled={disabled} />
        ) : (
          <ChoiceCombobox
            {...p}
            value={value}
            onChange={onChange}
            entries={entries}
            of={of}
            leading={leading}
            placeholder={placeholder}
            disabled={disabled}
            searchLabel={searchLabel ?? `Search ${label}`}
            emptyText={emptyText}
            pending={pending}
          />
        )
      }
    </Field>
  );
}

/** The searchable, grouped choice without label or message, for places that label it themselves (`aria-label`). */
export function CatalogChoiceControl({
  value,
  onChange,
  entryValue,
  leading,
  placeholder,
  disabled,
  searchLabel,
  emptyText = "No match",
  ...rest
}: Omit<CatalogFilterFieldProps, "label" | "hint" | "note" | "error" | "free" | "searchLabel"> & { searchLabel: string } & Partial<ControlProps> & { "aria-label"?: string }) {
  const { kinds, modelType, scope, exclude, ...aria } = rest;
  const { entries, pending } = useEntries({ kinds, modelType, scope, exclude });
  const of = entryValue ?? ((e: CatalogEntry) => e.name);
  return (
    <ChoiceCombobox
      {...aria}
      value={value}
      onChange={onChange}
      entries={entries}
      of={of}
      leading={leading}
      placeholder={placeholder}
      disabled={disabled}
      searchLabel={searchLabel}
      emptyText={emptyText}
      pending={pending}
    />
  );
}

function ChoiceCombobox({
  value,
  onChange,
  entries,
  of,
  leading = [],
  placeholder = "Select…",
  disabled,
  searchLabel,
  emptyText,
  pending,
  ...aria
}: {
  value: string;
  onChange: (v: string) => void;
  entries: CatalogEntries;
  of: (e: CatalogEntry) => string;
  leading?: { value: string; label: string }[];
  placeholder?: string;
  disabled?: boolean;
  searchLabel: string;
  emptyText: string;
  pending: boolean;
} & Partial<ControlProps>) {
  const [open, setOpen] = useState(false);
  const generatedId = useId();
  const groups = sections(entries);
  const lead = leading.find((l) => l.value === value);
  const entry = entries.find((e) => of(e) === value);
  const shown = lead ? lead.label : entry ? entry.name : undefined;
  const missing = shown === undefined && value !== "" && !pending;
  const choose = (v: string) => {
    onChange(v);
    setOpen(false);
  };
  // cmdk trims and lower-cases the value it matches on: it only ever sees an index, the search runs on the keywords.
  const rows: { k: string; value: string }[] = [];
  const item = (value_: string, name: string, children: ReactNode, selected: boolean) => {
    const k = `r:${rows.length}`;
    rows.push({ k, value: value_ });
    return (
      <CommandItem key={k} value={k} keywords={[name]} onSelect={() => choose(value_)}>
        {children}
        <CheckIcon className={cn("ml-auto size-4", selected ? "opacity-100" : "opacity-0")} aria-hidden="true" />
      </CommandItem>
    );
  };
  return (
    <Popover open={open} onOpenChange={setOpen} modal>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="outline"
          role="combobox"
          aria-expanded={open}
          aria-haspopup="listbox"
          id={aria.id ?? generatedId}
          disabled={disabled}
          className={cn("w-full min-w-0 justify-between px-3 font-normal", shown === undefined && "text-muted-foreground")}
          {...aria}
        >
          <span className="flex min-w-0 flex-1 items-center gap-2 text-left">
            {entry && !lead ? <EntryLabel entry={entry} /> : <span className="truncate">{shown ?? (missing ? MISSING_LABEL : placeholder)}</span>}
          </span>
          <ChevronsUpDownIcon className="opacity-50" aria-hidden="true" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-(--radix-popover-trigger-width) min-w-48 p-0">
        <Command filter={match} loop>
          <CommandInput aria-label={searchLabel} placeholder="Search…" />
          <CommandList className="max-h-[min(300px,var(--radix-popover-content-available-height))]">
            <CommandEmpty>{emptyText}</CommandEmpty>
            {leading.length > 0 && <CommandGroup>{leading.map((l) => item(l.value, l.label, <span className="min-w-0 truncate">{l.label}</span>, l.value === value))}</CommandGroup>}
            {groups.map((g, i) => (
              <Fragment key={g.kind}>
                {(i > 0 || leading.length > 0) && <CommandSeparator />}
                <CommandGroup heading={g.title}>{g.items.map((e) => item(of(e), e.name, <EntryLabel entry={e} />, of(e) === value && !lead))}</CommandGroup>
              </Fragment>
            ))}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}

/*
 * Free text: the labelled control is the text input itself and the typed text is the value, so a name no
 * entry has any more is accepted. The popup is a plain listbox that never takes focus, so typing continues
 * while it is open; Enter keeps the typed text unless an arrow key moved to a suggestion. Same structure
 * as the Select: a heading per kind and a separator between kinds.
 */
function FreeCatalogInput({
  value,
  onChange,
  entries,
  placeholder,
  disabled,
  ...aria
}: {
  value: string;
  onChange: (v: string) => void;
  entries: CatalogEntries;
  placeholder?: string;
  disabled?: boolean;
} & Partial<ControlProps>) {
  const [open, setOpen] = useState(false);
  const [highlight, setHighlight] = useState(-1);
  const generatedId = useId();
  const id = aria.id ?? generatedId;
  const listId = `${id}-list`;
  const needle = value.trim().toLowerCase();
  const groups = sections(entries.filter((e) => e.name.toLowerCase().includes(needle)));
  const flat = groups.flatMap((g) => g.items);
  const visible = open && flat.length > 0;
  const active = highlight < flat.length ? highlight : -1;
  const pickAt = (index: number) => {
    const e = flat[index];
    if (e) onChange(e.name);
    setOpen(false);
    setHighlight(-1);
  };
  const type = (text: string) => {
    onChange(text);
    setOpen(true);
    setHighlight(-1);
  };
  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!flat.length) return;
      setOpen(true);
      const step = e.key === "ArrowDown" ? 1 : -1;
      setHighlight(active < 0 ? (step > 0 ? 0 : flat.length - 1) : (active + step + flat.length) % flat.length);
    } else if (e.key === "Enter" && visible) {
      e.preventDefault();
      pickAt(active);
    } else if (e.key === "Escape" && visible) {
      e.stopPropagation();
      setOpen(false);
    }
  };
  let index = -1;
  return (
    <Popover open={visible} onOpenChange={setOpen}>
      <PopoverAnchor asChild>
        <Input
          {...aria}
          id={id}
          role="combobox"
          aria-expanded={visible}
          aria-controls={visible ? listId : undefined}
          aria-autocomplete="list"
          aria-activedescendant={visible && active >= 0 ? `${listId}-${active}` : undefined}
          autoComplete="off"
          value={value}
          placeholder={placeholder}
          disabled={disabled}
          onChange={(e) => type(e.target.value)}
          onClick={() => setOpen(true)}
          onBlur={() => setOpen(false)}
          onKeyDown={onKeyDown}
        />
      </PopoverAnchor>
      <PopoverContent
        align="start"
        className="w-(--radix-popover-trigger-width) min-w-48 p-1"
        onOpenAutoFocus={(e) => e.preventDefault()}
        onCloseAutoFocus={(e) => e.preventDefault()}
        onInteractOutside={(e) => {
          if (e.target instanceof Node && document.getElementById(id)?.contains(e.target)) e.preventDefault();
        }}
      >
        <div id={listId} role="listbox" aria-label="Suggestions" className="relative max-h-[min(300px,var(--radix-popover-content-available-height,300px))] overflow-y-auto">
          {groups.map((g, gi) => (
            <Fragment key={g.kind}>
              {gi > 0 && <Separator className="my-1" />}
              <div role="group" aria-label={g.title}>
                <div role="presentation" className="px-2 py-1.5 text-xs font-medium text-muted-foreground">
                  {g.title}
                </div>
                {g.items.map((e) => {
                  const i = ++index;
                  return (
                    <div
                      key={e.id}
                      ref={i === active ? keepInView : undefined}
                      id={`${listId}-${i}`}
                      role="option"
                      aria-selected={i === active}
                      data-active={i === active || undefined}
                      className="flex cursor-default items-center gap-2 rounded-sm px-2 py-1.5 text-sm select-none data-active:bg-accent data-active:text-accent-foreground hover:bg-accent hover:text-accent-foreground"
                      onMouseDown={(ev) => ev.preventDefault()}
                      onClick={() => pickAt(i)}
                    >
                      <EntryLabel entry={e} />
                    </div>
                  );
                })}
              </div>
            </Fragment>
          ))}
        </div>
      </PopoverContent>
    </Popover>
  );
}

function keepInView(el: HTMLElement | null) {
  const list = el?.closest<HTMLElement>("[role=listbox]");
  if (!el || !list) return;
  const top = el.offsetTop - (el.offsetParent === list ? 0 : ((el.offsetParent as HTMLElement | null)?.offsetTop ?? 0));
  if (top < list.scrollTop) list.scrollTop = top;
  else if (top + el.offsetHeight > list.scrollTop + list.clientHeight) list.scrollTop = top + el.offsetHeight - list.clientHeight;
}

/* ------------------------------------------------------------------------------------------ CatalogMultiSelect */

/** A named model group; its entry is chosen like a name and covers its members. */
export interface PickerGroup {
  name: string;
  /** Shown disabled with the reason as its tooltip. */
  disabled?: boolean;
  reason?: string;
}

export interface CatalogMultiSelectProps extends CatalogFilterProps {
  /** Chosen ids or names (`by`). */
  selected: string[];
  onChange: (selected: string[]) => void;
  by?: "id" | "name";
  /** Model groups, listed first. */
  groups?: PickerGroup[];
  /** Name -> the group that already covers it: shown checked and disabled with "via <group>". */
  covered?: Record<string, string>;
  /** Said of a chosen name that the filters no longer offer but the catalog still has. */
  outsideText?: string;
  /** Said when nothing is offered at all. */
  emptyText?: string;
  /** Prefix of the checkbox ids. */
  idPrefix?: string;
  /** Accessible name of the list. */
  label?: string;
  testId?: string;
  /** One column instead of two. */
  single?: boolean;
  rowClassName?: string;
  rowTestId?: (entry: CatalogEntry) => string;
  /** Next to the name of a chosen row (a weight). */
  renderInline?: (entry: CatalogEntry) => ReactNode;
  /** Under a chosen row (its settings). */
  renderDetail?: (entry: CatalogEntry) => ReactNode;
  /** Enable the catalog fetch only once it is needed. */
  enabled?: boolean;
  id?: string;
  invalid?: boolean;
  describedBy?: string;
  className?: string;
}

const SEARCH_AT = 8;

/** A grouped checkbox list: model groups, Models, Aliases, Routers, with a search box and a selected-only view when it is long. */
export function CatalogMultiSelect({
  selected,
  onChange,
  by = "name",
  groups = [],
  covered = {},
  outsideText = "(not offered here)",
  emptyText = "No models, aliases or routers to choose from.",
  idPrefix = "pick",
  label = "Models",
  testId,
  single,
  rowClassName,
  rowTestId,
  renderInline,
  renderDetail,
  enabled = true,
  id,
  invalid,
  describedBy,
  className,
  ...filter
}: CatalogMultiSelectProps) {
  const { all, entries, pending, error } = useEntries(filter, enabled);
  const [search, setSearch] = useState("");
  const [only, setOnly] = useState(false);
  const keyOf = (e: CatalogEntry) => (by === "id" ? e.id : e.name);
  const offered = new Set(entries.map(keyOf).concat(groups.map((g) => g.name)));
  // Chosen but no longer offered (left the scope, changed type, deleted): shown so it can be unchecked.
  const stale = selected.filter((k) => !offered.has(k));
  const known = new Map(all.map((e) => [keyOf(e), e]));
  const total = entries.length + groups.length;
  const searchable = total > SEARCH_AT;
  const needle = searchable ? search.trim().toLowerCase() : "";
  const shows = (name: string, checked: boolean) => (!needle || name.toLowerCase().includes(needle)) && (!only || checked);
  const toggle = (k: string, on: boolean) => onChange(on ? [...selected, k] : selected.filter((x) => x !== k));
  const isOn = (k: string) => selected.includes(k);

  const row = (
    key: string,
    children: ReactNode,
    opts: {
      entry?: CatalogEntry;
      stale?: boolean;
      disabled?: boolean;
      via?: string;
      title?: string;
    },
  ) => {
    const checked = !!opts.via || isOn(key);
    // A direct choice stays removable even while a group also covers it.
    const locked = !!opts.via && !isOn(key);
    const domId = `${idPrefix}-${opts.stale ? "stale-" : ""}${opts.entry?.id ?? key}`;
    return (
      <div key={`${opts.stale ? "s" : "e"}:${key}`} className={cn("flex min-w-0 flex-col gap-2", rowClassName)} data-testid={opts.entry && rowTestId?.(opts.entry)} title={opts.title}>
        <div className="flex items-center gap-2">
          <Checkbox id={domId} checked={checked} disabled={locked || opts.disabled} onCheckedChange={(c) => toggle(key, c === true)} />
          <Label htmlFor={domId} className={cn("min-w-0 flex-1 cursor-pointer gap-2 font-normal", (locked || opts.disabled) && "cursor-default")}>
            {children}
            {opts.via && <span className="shrink-0 text-xs text-muted-foreground">via {opts.via}</span>}
          </Label>
          {opts.entry && <EntryBadge entry={opts.entry} />}
          {checked && !opts.via && opts.entry && renderInline?.(opts.entry)}
        </div>
        {checked && !opts.via && opts.entry && renderDetail?.(opts.entry)}
      </div>
    );
  };

  const sectionsToShow: { key: string; title: string; rows: ReactNode[] }[] = [];
  const shownGroups = groups.filter((g) => shows(g.name, isOn(g.name)));
  if (shownGroups.length)
    sectionsToShow.push({
      key: "groups",
      title: "Model groups",
      rows: shownGroups.map((g) =>
        row(
          g.name,
          <>
            <LayersIcon aria-hidden="true" className="size-3.5 shrink-0 text-muted-foreground" />
            <span className="min-w-0 truncate [overflow-wrap:anywhere]">{g.name}</span>
          </>,
          { disabled: g.disabled, title: g.disabled ? g.reason : undefined },
        ),
      ),
    });
  for (const s of sections(entries)) {
    const items = s.items.filter((e) => shows(e.name, !!covered[e.name] || isOn(keyOf(e))));
    if (items.length)
      sectionsToShow.push({
        key: s.kind,
        title: s.title,
        rows: items.map((e) =>
          row(keyOf(e), <EntryLabel entry={e} badge={false} />, {
            entry: e,
            via: covered[e.name],
          }),
        ),
      });
  }
  const staleShown = stale.filter((k) => !needle || (known.get(k)?.name ?? k).toLowerCase().includes(needle));
  // One kind and no groups: the field's own label already names the list, so no heading repeats it.
  const lone = filter.kinds?.length === 1 && groups.length === 0;
  const count = selected.length;
  const nothing = !pending && !error && total === 0 && stale.length === 0;

  return (
    <div className={cn("flex min-w-0 flex-col gap-2", className)} id={id} role="group" aria-label={label} aria-invalid={invalid} aria-describedby={describedBy} data-testid={testId}>
      {
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          {searchable && (
            <div className="min-w-40 flex-1">
              <Input aria-label={`Search ${label.toLowerCase()}`} placeholder="Search…" value={search} onChange={(e) => setSearch(e.target.value)} />
            </div>
          )}
          <span className="text-xs text-muted-foreground" aria-live="polite">
            {count} selected
          </span>
          <div className="flex items-center gap-2">
            <Switch id={`${idPrefix}-only`} checked={only} onCheckedChange={setOnly} aria-label="Show selected only" />
            <Label htmlFor={`${idPrefix}-only`} className="cursor-pointer text-xs font-normal">
              Selected only
            </Label>
          </div>
        </div>
      }
      <div className={"flex max-h-72 flex-col gap-3 overflow-y-auto"}>
        {pending && <Skeleton className="h-4 w-full" />}
        {error && (
          <p className="text-[13px] text-destructive" role="alert">
            {error.message}
          </p>
        )}
        {nothing && <p className="text-sm text-muted-foreground">{emptyText}</p>}
        {sectionsToShow.map((s, i) => (
          <Fragment key={s.key}>
            {i > 0 && <Separator />}
            <div role={lone ? undefined : "group"} aria-label={lone ? undefined : s.title} className="flex flex-col gap-1.5">
              {!lone && <span className={HEADING}>{s.title}</span>}
              <div className={cn("grid gap-1.5", !single && "sm:grid-cols-2")}>{s.rows}</div>
            </div>
          </Fragment>
        ))}
        {staleShown.length > 0 && (
          <>
            {sectionsToShow.length > 0 && <Separator />}
            <div role="group" aria-label="Not offered" className="flex flex-col gap-1.5">
              <span className={HEADING}>Not offered</span>
              {staleShown.map((k) =>
                row(
                  k,
                  <span className="[overflow-wrap:anywhere]">
                    {known.get(k)?.name ?? k} <span className="text-muted-foreground">{known.has(k) ? outsideText : "(no longer exists)"}</span>
                  </span>,
                  { stale: true },
                ),
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
