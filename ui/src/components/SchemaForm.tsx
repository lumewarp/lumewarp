import { Plus, X } from "lucide-react";
import { useEffect, useId, useState } from "react";

import { cn } from "../lib/utils";
import { HEADER_LIST_WIDGET, type Field as SchemaField, type Values } from "../lib/schema";
import { Field } from "./Form";
import { Button } from "./ui/button";
import { Checkbox } from "./ui/checkbox";
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

function setKey(values: Values, key: string, value: unknown): Values {
  const next = { ...values };
  if (value === undefined) delete next[key];
  else next[key] = value;
  return next;
}

export function SchemaFields({ fields, values, onChange, errors = {}, path = "", idPrefix }: FieldsProps) {
  return (
    <>
      {fields.map((f) => {
        const full = `${path}${f.key}`;
        const id = `${idPrefix}-${full}`;
        if (f.kind === "object") {
          const inner = typeof values[f.key] === "object" && values[f.key] !== null ? (values[f.key] as Values) : {};
          return (
            <fieldset key={f.key} className="form-section" data-testid={`schema-${full}`}>
              <legend>{f.title}</legend>
              {f.description && <p className="field-hint">{f.description}</p>}
              <SchemaFields fields={f.fields ?? []} values={inner} onChange={(next) => onChange(setKey(values, f.key, Object.keys(next).length ? next : undefined))} errors={errors} path={`${full}.`} idPrefix={idPrefix} />
            </fieldset>
          );
        }
        const label = f.required ? `${f.title} *` : f.title;
        const hint = f.description || undefined;
        if (f.kind === "boolean") {
          return (
            <div key={f.key} className="flex flex-col gap-1.5">
              <div className="flex items-center gap-2">
                <Checkbox id={id} checked={values[f.key] === true || (values[f.key] === undefined && f.default === true)} onCheckedChange={(c) => onChange(setKey(values, f.key, c === true))} />
                <Label htmlFor={id} className="cursor-pointer font-semibold">
                  {f.title}
                </Label>
              </div>
              {hint && <p className="field-hint">{hint}</p>}
              {errors[full] && (
                <p className="field-error" role="alert">
                  {errors[full]}
                </p>
              )}
            </div>
          );
        }
        if (f.kind === "array") {
          return (
            <Field key={f.key} label={label} hint={hint} error={errors[full]}>
              {(p) => <StringList {...p} header={f.widget === HEADER_LIST_WIDGET} title={f.title} value={Array.isArray(values[f.key]) ? (values[f.key] as string[]) : []} onChange={(next) => onChange(setKey(values, f.key, next.length ? next : undefined))} />}
            </Field>
          );
        }
        if (f.enum) {
          const current = values[f.key];
          return (
            <Field key={f.key} label={label} hint={hint} error={errors[full]}>
              {(p) => (
                <select
                  {...p}
                  className={cn("flex h-9 w-full min-w-0 rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-xs outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 dark:bg-input/30", errors[full] && "border-destructive")}
                  value={current === undefined ? "" : String(current)}
                  onChange={(e) => {
                    const raw = e.target.value;
                    onChange(setKey(values, f.key, raw === "" ? undefined : f.kind === "string" ? raw : Number(raw)));
                  }}
                >
                  <option value="">{f.required ? "Select…" : f.default !== undefined ? `Default (${String(f.default)})` : "Not set"}</option>
                  {f.enum!.map((o) => (
                    <option key={String(o)} value={String(o)}>
                      {String(o)}
                    </option>
                  ))}
                </select>
              )}
            </Field>
          );
        }
        if (f.kind === "string") {
          return (
            <Field key={f.key} label={label} hint={hint} error={errors[full]}>
              {(p) => <Input {...p} value={typeof values[f.key] === "string" ? (values[f.key] as string) : ""} placeholder={f.default !== undefined ? String(f.default) : undefined} onChange={(e) => onChange(setKey(values, f.key, e.target.value === "" ? undefined : e.target.value))} />}
            </Field>
          );
        }
        // integer | number
        return (
          <Field key={f.key} label={label} hint={[hint, bounds(f)].filter(Boolean).join(" ") || undefined} error={errors[full]}>
            {(p) => <NumberInput {...p} integer={f.kind === "integer"} min={f.minimum} max={f.maximum} placeholder={f.default !== undefined ? String(f.default) : undefined} value={typeof values[f.key] === "number" ? (values[f.key] as number) : undefined} onChange={(n) => onChange(setKey(values, f.key, n))} />}
          </Field>
        );
      })}
    </>
  );
}

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

/** Add/remove list of strings (header names for the `header-list` widget). */
function StringList({ id, value, onChange, header, title, ...aria }: { id: string; value: string[]; onChange: (v: string[]) => void; header: boolean; title: string } & Record<string, unknown>) {
  return (
    <div className="flex flex-col gap-1.5" id={id} role="group" aria-label={title} data-widget={header ? HEADER_LIST_WIDGET : "string-list"} {...aria}>
      {value.map((entry, i) => (
        <div key={i} className="flex items-center gap-1.5">
          <Input aria-label={`${title} ${i + 1}`} value={entry} placeholder={header ? "x-session-id" : undefined} onChange={(e) => onChange(value.map((x, j) => (j === i ? e.target.value : x)))} />
          <Button type="button" variant="ghost" size="icon" className="size-8 shrink-0 text-muted-foreground" aria-label={`Remove ${title} ${i + 1}`} onClick={() => onChange(value.filter((_, j) => j !== i))}>
            <X aria-hidden="true" />
          </Button>
        </div>
      ))}
      <div>
        <Button type="button" variant="outline" size="sm" onClick={() => onChange([...value, ""])}>
          <Plus aria-hidden="true" />
          {header ? "Add header" : "Add entry"}
        </Button>
      </div>
    </div>
  );
}

/**
 * Raw JSON object editor: the fallback when no usable schema is known. Reports a parse problem through
 * `onProblem` (null when the text is a valid JSON object) so the owning form can refuse to submit.
 */
export function JsonEditor({ label, value, onChange, onProblem, error, hint, idPrefix }: { label: string; value: Values; onChange: (v: Values) => void; onProblem: (message: string | null) => void; error?: string; hint?: string; idPrefix?: string }) {
  const initial = JSON.stringify(value, null, 2);
  const [text, setText] = useState(initial);
  const [problem, setProblem] = useState<string | null>(null);
  const id = useId();
  // Unmounting (a member was unchecked, the strategy changed) clears the problem it reported.
  useEffect(() => () => onProblem(null), []); // eslint-disable-line react-hooks/exhaustive-deps
  // A different router was loaded into the form.
  useEffect(() => {
    setText(initial);
    setProblem(null);
    onProblem(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [idPrefix]);
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
