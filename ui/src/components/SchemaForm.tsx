import { Plus, X } from "lucide-react";
import { useEffect, useId, useState } from "react";

import { getOwn, HEADER_LIST_WIDGET, hasOwn, withKey, withoutKey, type Field as SchemaField, type Values } from "../lib/schema";
import { Field, SelectControl } from "./Form";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Label } from "./ui/label";
import { Textarea } from "./ui/textarea";

/*
 * Forms generated from a strategy's settings schema. Everything that comes from the schema (titles,
 * descriptions, enum values) is a React text child: never markup, never a link, never evaluated.
 */

interface FieldsProps {
  fields: SchemaField[];
  values: Values;
  onChange: (values: Values) => void;
  errors?: Record<string, string>;
  /** Prefix of `errors` keys (nested objects add their own). */
  path?: string;
  idPrefix: string;
}

const BOOLEAN_OPTIONS = [
  { value: true, label: "On" },
  { value: false, label: "Off" },
];

/** `undefined` removes the key; anything else (`""`, `false`, `0`, `[]` too) is a value. */
function setKey(values: Values, key: string, value: unknown): Values {
  return value === undefined ? withoutKey(values, key) : withKey(values, key, value);
}

function Unset({ title, onClick }: { title: string; onClick: () => void }) {
  return (
    <Button type="button" variant="ghost" size="sm" className="shrink-0 text-muted-foreground" aria-label={`Unset ${title}`} onClick={onClick}>
      <X aria-hidden="true" />
      Unset
    </Button>
  );
}

export function SchemaFields({ fields, values, onChange, errors = {}, path = "", idPrefix }: FieldsProps) {
  return (
    <>
      {fields.map((f) => {
        const full = `${path}${f.key}`;
        const error = Object.hasOwn(errors, full) ? errors[full] : undefined;
        const present = hasOwn(values, f.key) && values[f.key] !== undefined;
        const current = getOwn(values, f.key);
        const unset = !f.required && present ? <Unset title={f.title} onClick={() => onChange(setKey(values, f.key, undefined))} /> : null;
        const label = f.required ? `${f.title} *` : f.title;
        const hint = f.description || undefined;
        if (f.kind === "object") {
          const inner = isObject(current) ? current : undefined;
          if (!present || !inner) {
            return (
              <div key={f.key} className="flex flex-col gap-1.5" data-testid={`schema-${full}`}>
                <div className="flex items-center gap-2">
                  <Button type="button" variant="outline" size="sm" onClick={() => onChange(setKey(values, f.key, Object.create(null)))}>
                    <Plus aria-hidden="true" />
                    Add {f.title}
                  </Button>
                  <span className="text-xs text-muted-foreground">{f.required ? "Required" : "Not set"}</span>
                </div>
                {hint && <p className="field-hint">{hint}</p>}
                {error && (
                  <p className="field-error" role="alert">
                    {error}
                  </p>
                )}
              </div>
            );
          }
          return (
            <fieldset key={f.key} className="form-section" data-testid={`schema-${full}`}>
              <legend>{f.title}</legend>
              {hint && <p className="field-hint">{hint}</p>}
              {error && (
                <p className="field-error" role="alert">
                  {error}
                </p>
              )}
              <SchemaFields fields={f.fields ?? []} values={inner} onChange={(next) => onChange(withKey(values, f.key, next))} errors={errors} path={`${full}.`} idPrefix={idPrefix} />
              {!f.required && (
                <div>
                  <Button type="button" variant="ghost" size="sm" className="text-muted-foreground" aria-label={`Remove ${f.title}`} onClick={() => onChange(setKey(values, f.key, undefined))}>
                    <X aria-hidden="true" />
                    Remove {f.title}
                  </Button>
                </div>
              )}
            </fieldset>
          );
        }
        if (f.kind === "boolean") {
          return (
            <Field key={f.key} label={label} hint={hint} error={error}>
              {(p) => (
                <SelectControl<boolean>
                  {...p}
                  strict
                  value={present ? (current as boolean) : undefined}
                  options={BOOLEAN_OPTIONS}
                  onChange={(b) => onChange(setKey(values, f.key, b))}
                  unset={{ label: f.required ? "Choose…" : typeof f.default === "boolean" ? `Not set (default ${f.default ? "On" : "Off"})` : "Not set", onSelect: () => onChange(setKey(values, f.key, undefined)) }}
                />
              )}
            </Field>
          );
        }
        if (f.kind === "array") {
          return (
            <Field key={f.key} label={label} hint={hint} error={error}>
              {(p) => <StringList {...p} header={f.widget === HEADER_LIST_WIDGET} title={f.title} required={f.required} value={Array.isArray(current) ? (current as string[]) : undefined} onChange={(next) => onChange(setKey(values, f.key, next))} />}
            </Field>
          );
        }
        if (f.enum) {
          const options = f.enum.map((o) => ({ value: o, label: o === "" ? "(empty text)" : String(o) }));
          return (
            <Field key={f.key} label={label} hint={hint} error={error}>
              {(p) => (
                <SelectControl<string | number>
                  {...p}
                  strict
                  value={present ? (current as string | number) : undefined}
                  options={options}
                  onChange={(v) => onChange(setKey(values, f.key, v))}
                  unset={{ label: f.required ? "Select…" : f.default !== undefined ? `Default (${String(f.default)})` : "Not set", onSelect: () => onChange(setKey(values, f.key, undefined)) }}
                />
              )}
            </Field>
          );
        }
        if (f.kind === "string") {
          return (
            <Field key={f.key} label={label} hint={hint} error={error}>
              {(p) => (
                <div className="flex items-center gap-1.5">
                  <Input {...p} value={typeof current === "string" ? current : ""} placeholder={f.default !== undefined ? String(f.default) : undefined} onChange={(e) => onChange(setKey(values, f.key, e.target.value))} />
                  {unset}
                </div>
              )}
            </Field>
          );
        }
        // integer | number: an empty box is "not set"
        return (
          <Field key={f.key} label={label} hint={[hint, bounds(f)].filter(Boolean).join(" ") || undefined} error={error}>
            {(p) => <NumberInput {...p} integer={f.kind === "integer"} min={f.minimum} max={f.maximum} placeholder={f.default !== undefined ? String(f.default) : undefined} value={typeof current === "number" ? current : undefined} onChange={(n) => onChange(setKey(values, f.key, n))} />}
          </Field>
        );
      })}
    </>
  );
}

const isObject = (v: unknown): v is Values => typeof v === "object" && v !== null && !Array.isArray(v);

function bounds(f: SchemaField): string {
  if (f.minimum !== undefined && f.maximum !== undefined) return `Between ${f.minimum} and ${f.maximum}.`;
  if (f.minimum !== undefined) return `At least ${f.minimum}.`;
  if (f.maximum !== undefined) return `At most ${f.maximum}.`;
  return "";
}

/** A number box that keeps what is being typed ("1.", "-") until it parses. */
export function NumberInput({ value, onChange, integer, min, max, ...rest }: { value: number | undefined; onChange: (n: number | undefined) => void; integer?: boolean; min?: number; max?: number } & Omit<React.ComponentProps<"input">, "value" | "onChange" | "min" | "max">) {
  const [text, setText] = useState(value === undefined ? "" : String(value));
  useEffect(() => {
    setText((t) => (t === "" && value === undefined) || Number(t) === value ? t : value === undefined ? "" : String(value));
  }, [value]);
  return (
    <Input
      {...rest}
      type="number"
      inputMode={integer ? "numeric" : "decimal"}
      step={integer ? 1 : "any"}
      min={min}
      max={max}
      value={text}
      onChange={(e) => {
        setText(e.target.value);
        const n = e.target.value === "" ? undefined : Number(e.target.value);
        // A half-typed value stays as text until it is a number; out-of-range numbers go to validation.
        if (n === undefined || Number.isFinite(n)) onChange(n);
      }}
    />
  );
}

/**
 * Add/remove list of strings (header names for the `header-list` widget). `undefined` is "not set"; an
 * empty list is a value of its own, and entries are kept exactly as typed.
 */
function StringList({ id, value, onChange, header, title, required, ...aria }: { id: string; value: string[] | undefined; onChange: (v: string[] | undefined) => void; header: boolean; title: string; required: boolean } & Record<string, unknown>) {
  const list = value ?? [];
  return (
    <div className="flex flex-col gap-1.5" id={id} role="group" aria-label={title} data-widget={header ? HEADER_LIST_WIDGET : "string-list"} {...aria}>
      {value === undefined && <p className="text-xs text-muted-foreground">{required ? "Required" : "Not set"}</p>}
      {list.map((entry, i) => (
        <div key={i} className="flex items-center gap-1.5">
          <Input aria-label={`${title} ${i + 1}`} value={entry} placeholder={header ? "x-session-id" : undefined} onChange={(e) => onChange(list.map((x, j) => (j === i ? e.target.value : x)))} />
          <Button type="button" variant="ghost" size="icon" className="size-8 shrink-0 text-muted-foreground" aria-label={`Remove ${title} ${i + 1}`} onClick={() => onChange(list.filter((_, j) => j !== i))}>
            <X aria-hidden="true" />
          </Button>
        </div>
      ))}
      <div className="flex flex-wrap gap-1.5">
        <Button type="button" variant="outline" size="sm" onClick={() => onChange([...list, ""])}>
          <Plus aria-hidden="true" />
          {header ? "Add header" : "Add entry"}
        </Button>
        {value === undefined && (
          <Button type="button" variant="outline" size="sm" onClick={() => onChange([])}>
            Use empty list
          </Button>
        )}
        {value !== undefined && !required && (
          <Button type="button" variant="ghost" size="sm" className="text-muted-foreground" aria-label={`Unset ${title}`} onClick={() => onChange(undefined)}>
            <X aria-hidden="true" />
            Unset
          </Button>
        )}
      </div>
    </div>
  );
}

/**
 * Raw JSON object editor: the fallback when no usable schema is known. Reports a parse problem through
 * `onProblem` (null when the text is a valid JSON object) so the owning form can refuse to submit.
 *
 * The text is seeded from `value` once: the owner gives it a `key` naming what is being edited (router,
 * strategy, member), so a different identity remounts it with that value's text and no stale problem.
 * `JSON.parse` and `JSON.stringify` keep a `__proto__` member as the own property it is.
 */
export function JsonEditor({ label, value, onChange, onProblem, error, hint }: { label: string; value: Values; onChange: (v: Values) => void; onProblem: (message: string | null) => void; error?: string; hint?: string }) {
  const initial = JSON.stringify(value, null, 2);
  const [text, setText] = useState(initial);
  const [problem, setProblem] = useState<string | null>(null);
  const id = useId();
  // Unmounting (a member was unchecked, the strategy changed) clears the problem it reported.
  useEffect(() => () => onProblem(null), []); // eslint-disable-line react-hooks/exhaustive-deps
  const update = (next: string) => {
    setText(next);
    try {
      const parsed: unknown = JSON.parse(next.trim() === "" ? "{}" : next);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) throw new Error("Must be a JSON object");
      setProblem(null);
      onProblem(null);
      onChange(parsed as Values);
    } catch (e) {
      const message = e instanceof Error ? e.message : "Invalid JSON";
      setProblem(message);
      onProblem(message);
    }
  };
  const shown = problem ?? error;
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <Label htmlFor={id} className="font-semibold">
        {label}
      </Label>
      <Textarea id={id} className="min-h-24 font-mono text-[13px]" spellCheck={false} value={text} aria-invalid={!!shown} onChange={(e) => update(e.target.value)} />
      {shown ? (
        <p className="field-error" role="alert">
          {shown}
        </p>
      ) : (
        hint && <p className="field-hint">{hint}</p>
      )}
    </div>
  );
}
