import { CheckIcon, ChevronsUpDownIcon } from "lucide-react";
import { useId, useState } from "react";

import { cn } from "@/lib/utils";
import { applyChoice, MISSING_LABEL, optionKey, selection, UNSET_KEY, type ChoiceOption, type ChoiceProps } from "../choice";
import { Field, type ControlProps } from "../Form";
import { Button } from "./button";
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from "./command";
import { Input } from "./input";
import { Popover, PopoverAnchor, PopoverContent, PopoverTrigger } from "./popover";

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
  const generatedId = useId();
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
        <Button type="button" variant="outline" role="combobox" aria-expanded={open} aria-haspopup="listbox" id={aria.id ?? generatedId} disabled={disabled} className={cn("w-full min-w-0 justify-between px-3 font-normal", !shown && "text-muted-foreground")} {...aria}>
          <span className="min-w-0 flex-1 truncate text-left">{shown ?? (missing ? MISSING_LABEL : placeholder)}</span>
          <ChevronsUpDownIcon className="opacity-50" aria-hidden="true" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-(--radix-popover-trigger-width) min-w-48 p-0">
        <Command filter={match} loop>
          <CommandInput aria-label={searchLabel} placeholder="Search…" />
          <CommandList className="max-h-[min(300px,var(--radix-popover-content-available-height))]">
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

/*
 * Free-text mode (`free`): the labelled control is the text input itself, and the typed text is the value, so
 * any string is accepted, including one no suggestion has any more. The suggestions only complete it: picking
 * one writes its text into the input. There is one control for the label (the input, role combobox); the popup
 * is a plain listbox that never takes focus, so typing continues while it is open. Enter keeps the typed text
 * unless an arrow key moved to a suggestion. Nothing is trimmed here: the caller decides what it sends.
 */
function keepInView(el: HTMLElement | null) {
  const list = el?.parentElement;
  if (!el || !list) return;
  if (el.offsetTop < list.scrollTop) list.scrollTop = el.offsetTop;
  else if (el.offsetTop + el.offsetHeight > list.scrollTop + list.clientHeight) list.scrollTop = el.offsetTop + el.offsetHeight - list.clientHeight;
}

function FreeCombobox({ value, onChange, options, placeholder, disabled, ...aria }: { value: string; onChange: (v: string) => void; options: ChoiceOption<string>[]; placeholder?: string; disabled?: boolean } & Partial<ControlProps>) {
  const [open, setOpen] = useState(false);
  const [highlight, setActive] = useState(-1);
  const generatedId = useId();
  const id = aria.id ?? generatedId;
  const listId = `${id}-list`;
  const needle = value.trim().toLowerCase();
  const shown = options.filter((o) => o.label.toLowerCase().includes(needle));
  const visible = open && shown.length > 0;
  // Suggestions can change under the highlight when they reload.
  const active = highlight < shown.length ? highlight : -1;
  const pickAt = (index: number) => {
    const o = shown[index];
    if (o) onChange(o.value);
    setOpen(false);
    setActive(-1);
  };
  const type = (text: string) => {
    onChange(text);
    setOpen(true);
    setActive(-1);
  };
  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!shown.length) return;
      setOpen(true);
      const step = e.key === "ArrowDown" ? 1 : -1;
      setActive(active < 0 ? (step > 0 ? 0 : shown.length - 1) : (active + step + shown.length) % shown.length);
    } else if (e.key === "Enter" && visible) {
      e.preventDefault();
      pickAt(active);
    } else if (e.key === "Escape" && visible) {
      e.stopPropagation();
      setOpen(false);
    }
  };
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
          {shown.map((o, i) => (
            <div
              key={o.value}
              ref={i === active ? keepInView : undefined}
              id={`${listId}-${i}`}
              role="option"
              aria-selected={i === active}
              data-active={i === active || undefined}
              className="flex cursor-default items-center rounded-sm px-2 py-1.5 text-sm select-none data-active:bg-accent data-active:text-accent-foreground hover:bg-accent hover:text-accent-foreground"
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => pickAt(i)}
            >
              <span className="min-w-0 flex-1 truncate">{o.label}</span>
            </div>
          ))}
        </div>
      </PopoverContent>
    </Popover>
  );
}

type FieldProps<T> = ChoiceProps<T> & { label: string; error?: string; hint?: string; searchLabel?: string; emptyText?: string; free?: boolean };

export function ComboboxField<T = string>({ label, error, hint, free, ...choice }: FieldProps<T>) {
  return (
    <Field label={label} error={error} hint={hint}>
      {(p) =>
        free ? (
          <FreeCombobox value={String(choice.value ?? "")} onChange={choice.onChange as (v: string) => void} options={choice.options as unknown as ChoiceOption<string>[]} placeholder={choice.placeholder} disabled={choice.disabled} {...p} />
        ) : (
          <Combobox {...choice} searchLabel={choice.searchLabel ?? `Search ${label}`} {...p} />
        )
      }
    </Field>
  );
}
