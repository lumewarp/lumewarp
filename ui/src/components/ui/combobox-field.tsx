import { CheckIcon, ChevronsUpDownIcon } from "lucide-react";
import { useId, useState } from "react";

import { cn } from "@/lib/utils";
import { applyChoice, MISSING_LABEL, optionKey, selection, UNSET_KEY, type ChoiceProps } from "../choice";
import { Field, type ControlProps } from "../Form";
import { Button } from "./button";
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from "./command";
import { Popover, PopoverContent, PopoverTrigger } from "./popover";

/*
 * Searchable choice for lists whose length comes from data. Same props and same value contract as
 * SelectField (choice.ts): the domain value is the only state, cmdk only ever sees `i:<index>` (it trims and
 * lower-cases what it matches on), and the search matches the label, not the key.
 *
 * The popover is modal so that it scrolls and traps focus when opened inside a Dialog or Sheet (whose
 * scroll lock would otherwise swallow the wheel in a portal outside them); Esc closes only the popover.
 */

const match = (_key: string, search: string, keywords?: string[]) => ((keywords?.join(" ") ?? "").toLowerCase().includes(search.trim().toLowerCase()) ? 1 : 0);

export function Combobox<T>({
  value,
  onChange,
  options,
  unset,
  placeholder = "Select…",
  strict = false,
  disabled,
  searchLabel = "Search",
  emptyText = "No match",
  ...aria
}: ChoiceProps<T> & Partial<ControlProps> & { "aria-label"?: string; searchLabel?: string; emptyText?: string }) {
  const [open, setOpen] = useState(false);
  const listId = useId();
  const { key, missing } = selection(value, options, unset, strict);
  const shown = key === UNSET_KEY ? unset?.label : key ? options[Number(key.slice(2))]?.label : undefined;
  const choose = (k: string) => {
    applyChoice(k, options, unset, onChange);
    setOpen(false);
  };
  const item = (k: string, label: string) => (
    <CommandItem key={k} value={k} keywords={[label]} onSelect={choose}>
      <span className="min-w-0 flex-1 truncate">{label}</span>
      <CheckIcon className={cn("size-4", k === key ? "opacity-100" : "opacity-0")} aria-hidden="true" />
    </CommandItem>
  );
  return (
    <Popover open={open} onOpenChange={setOpen} modal>
      <PopoverTrigger asChild>
        <Button type="button" variant="outline" role="combobox" aria-expanded={open} aria-haspopup="listbox" disabled={disabled} className={cn("w-full min-w-0 justify-between px-3 font-normal", !shown && "text-muted-foreground")} {...aria}>
          <span className="min-w-0 flex-1 truncate text-left">{shown ?? (missing ? MISSING_LABEL : placeholder)}</span>
          <ChevronsUpDownIcon className="opacity-50" aria-hidden="true" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-(--radix-popover-trigger-width) min-w-48 p-0">
        <Command filter={match} loop>
          <CommandInput aria-label={searchLabel} placeholder="Search…" />
          <CommandList id={listId} className="max-h-[min(300px,var(--radix-popover-content-available-height))]">
            <CommandEmpty>{emptyText}</CommandEmpty>
            <CommandGroup>
              {unset && item(UNSET_KEY, unset.label)}
              {options.map((o, i) => item(optionKey(i), o.label))}
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}

export function ComboboxField<T = string>({ label, error, hint, ...choice }: ChoiceProps<T> & { label: string; error?: string; hint?: string; searchLabel?: string; emptyText?: string }) {
  return (
    <Field label={label} error={error} hint={hint}>
      {(p) => <Combobox {...choice} searchLabel={choice.searchLabel ?? `Search ${label}`} {...p} />}
    </Field>
  );
}
