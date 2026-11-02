/*
 * The one place where a domain value becomes a widget value and back, shared by SelectField (Radix
 * Select) and ComboboxField (cmdk). Widgets only ever see an opaque key; the domain value is the single
 * piece of state and is never stored in the widget.
 *
 *   ""      nothing is shown as chosen (the placeholder)
 *   "u"     the explicit "not set" row
 *   "i:<n>" the n-th option, derived at render time from the current options
 *
 * Why not use the value itself as the key: Radix treats "" as "no value" (so a legal "" cannot be an
 * item), cmdk trims and lower-cases what it matches on, and enum values can be numbers or booleans.
 */

export interface ChoiceOption<T> {
  value: T;
  label: string;
}

/** A row that means "no value": picking it calls `onSelect`; it is shown as chosen while the value is undefined. */
export interface ChoiceUnset {
  label: string;
  onSelect: () => void;
}

export const UNSET_KEY = "u";
export const NO_KEY = "";
export const MISSING_LABEL = "Current value not in list";

export const optionKey = (index: number) => `i:${index}`;

export interface Selection {
  /** What the widget is given as its value. */
  key: string;
  /** The value is set but is none of the options (not loaded yet, option removed, enum changed). */
  missing: boolean;
}

/**
 * `strict` off: an empty string that matches no option counts as "nothing chosen" (a form that starts
 * blank before its options load). `strict` on: any value that matches no option is "not in the list",
 * and nothing is ever selected on its behalf.
 */
export function selection<T>(value: T | undefined, options: ChoiceOption<T>[], unset: ChoiceUnset | undefined, strict: boolean): Selection {
  if (value === undefined) return { key: unset ? UNSET_KEY : NO_KEY, missing: false };
  const index = options.findIndex((o) => o.value === value);
  if (index >= 0) return { key: optionKey(index), missing: false };
  if (!strict && (value as unknown) === "") return { key: NO_KEY, missing: false };
  return { key: NO_KEY, missing: true };
}

export type Resolved<T> = { kind: "option"; value: T } | { kind: "unset" } | null;

/** Back from a widget key to the domain value. Anything out of range or malformed resolves to nothing. */
export function resolve<T>(key: string, options: ChoiceOption<T>[], unset: ChoiceUnset | undefined): Resolved<T> {
  if (key === UNSET_KEY) return unset ? { kind: "unset" } : null;
  const m = /^i:(\d+)$/.exec(key);
  if (!m) return null;
  const index = Number(m[1]);
  return index < options.length ? { kind: "option", value: options[index].value } : null;
}

/** Apply a resolved choice to the caller's callbacks. */
export function applyChoice<T>(key: string, options: ChoiceOption<T>[], unset: ChoiceUnset | undefined, onChange: (v: T) => void) {
  const r = resolve(key, options, unset);
  if (r?.kind === "option") onChange(r.value);
  else if (r?.kind === "unset") unset!.onSelect();
}

export interface ChoiceProps<T> {
  value: T | undefined;
  onChange: (v: T) => void;
  options: ChoiceOption<T>[];
  /** Adds an explicit "not set" row at the top; chosen while `value` is undefined. */
  unset?: ChoiceUnset;
  /** Shown while nothing is chosen. */
  placeholder?: string;
  /** See `selection`. */
  strict?: boolean;
  disabled?: boolean;
}
