import { useId, useState, type FormEvent, type ReactNode } from "react";

import { ApiError } from "../api/errors";
import { Dialog } from "./Dialog";
import { applyChoice, MISSING_LABEL, optionKey, selection, UNSET_KEY, type ChoiceProps } from "./choice";
import { Alert, AlertDescription } from "./ui/alert";
import { Button } from "./ui/button";
import { Checkbox } from "./ui/checkbox";
import { Field as UiField, FieldDescription, FieldError, FieldLabel } from "./ui/field";
import { Input } from "./ui/input";
import { Label } from "./ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "./ui/select";

type Validators<V> = { [K in keyof V]?: (value: V[K], all: V) => string | undefined };

/** Minimal form state: values, per-field validators, touched-on-submit errors. */
export function useForm<V extends object>(initial: V, validators: Validators<V> = {}) {
  const [values, setValues] = useState<V>(initial);
  const [errors, setErrors] = useState<Partial<Record<keyof V, string>>>({});
  const validate = (v: V) => {
    const next: Partial<Record<keyof V, string>> = {};
    for (const k of Object.keys(validators) as (keyof V)[]) {
      const msg = validators[k]?.(v[k], v);
      if (msg) next[k] = msg;
    }
    setErrors(next);
    return Object.keys(next).length === 0;
  };
  return {
    values,
    errors,
    set<K extends keyof V>(k: K, value: V[K]) {
      setValues((v) => ({ ...v, [k]: value }));
      setErrors((e) => ({ ...e, [k]: undefined }));
    },
    validate: () => validate(values),
    setError(k: keyof V, message: string) {
      setErrors((e) => ({ ...e, [k]: message }));
    },
    /**
     * Run a submit; a 422 naming one of this form's fields is shown inline on that field
     * (keep the dialog open) instead of as a form-level error. `alias` maps API field names
     * to form field names (e.g. budget_nano_usd -> budget_usd).
     */
    async run(fn: () => Promise<unknown>, alias: Record<string, keyof V> = {}): Promise<boolean | void> {
      try {
        await fn();
      } catch (err) {
        const field = err instanceof ApiError ? err.field : undefined;
        const key = field && (alias[field] ?? (field in (values as object) ? (field as keyof V) : undefined));
        if (err instanceof ApiError && key) {
          setErrors((e) => ({ ...e, [key]: err.message }));
          return false;
        }
        throw err;
      }
    },
    reset: (v: V = initial) => {
      setValues(v);
      setErrors({});
    },
  };
}

export const required = (label: string) => (v: string) => (v.trim() ? undefined : `${label} is required`);

/**
 * Label, control and message. Layout is utilities only: the old `field` class sized the router form's split
 * children and went with the legacy stylesheet.
 */
export function Field({ label, error, hint, children }: { label: string; error?: string; hint?: string; children: (p: ControlProps) => ReactNode }) {
  const id = useId();
  const descId = error || hint ? `${id}-d` : undefined;
  return (
    <UiField className="min-w-0 gap-1.5" data-invalid={error ? true : undefined}>
      <FieldLabel htmlFor={id} className="font-semibold">
        {label}
      </FieldLabel>
      {children({ id, "aria-invalid": !!error, "aria-describedby": descId })}
      {error ? <FieldError id={descId}>{error}</FieldError> : hint && <FieldDescription id={descId}>{hint}</FieldDescription>}
    </UiField>
  );
}

export interface ControlProps {
  id: string;
  "aria-invalid": boolean;
  "aria-describedby"?: string;
}

interface TextProps {
  label: string;
  value: string;
  onChange: (v: string) => void;
  error?: string;
  hint?: string;
  type?: "text" | "password" | "url" | "date";
  placeholder?: string;
  autoFocus?: boolean;
  /** For date inputs: the accepted range, as `YYYY-MM-DD`. */
  min?: string;
  max?: string;
}
export function TextField({ label, value, onChange, error, hint, type = "text", placeholder, autoFocus, min, max }: TextProps) {
  return (
    <Field label={label} error={error} hint={hint}>
      {(p) => <Input {...p} type={type} value={value} min={min} max={max} placeholder={placeholder} autoFocus={autoFocus} onChange={(e) => onChange(e.target.value)} />}
    </Field>
  );
}

/** Number input where an empty string means "no limit" (null). */
export function NumberField({ label, value, onChange, error, hint, min = 0, step, placeholder = "unlimited" }: { label: string; value: number | null; onChange: (v: number | null) => void; error?: string; hint?: string; min?: number; step?: number | "any"; placeholder?: string }) {
  return (
    <Field label={label} error={error} hint={hint}>
      {(p) => (
        <Input {...p} type="number" min={min} step={step} value={value ?? ""} placeholder={placeholder} onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))} />
      )}
    </Field>
  );
}

/**
 * The control of a select, without label or message (for places that label it themselves, e.g. with
 * aria-label). Contract, see choice.ts: the domain value is the only state; the Radix value is
 * `i:<index>` derived on every render; a value that is none of the options is shown as "Current value
 * not in list" and nothing is selected for it; only an in-range index can change the value.
 */
export function SelectControl<T>({ value, onChange, options, unset, placeholder = "Select…", strict = false, disabled, ...aria }: ChoiceProps<T> & Partial<ControlProps> & { "aria-label"?: string }) {
  const { key, missing } = selection(value, options, unset, strict);
  return (
    <Select value={key} onValueChange={(k) => applyChoice(k, options, unset, onChange)} disabled={disabled}>
      <SelectTrigger {...aria} className="w-full min-w-0">
        <SelectValue placeholder={missing ? MISSING_LABEL : placeholder} />
      </SelectTrigger>
      <SelectContent position="popper">
        {unset && <SelectItem value={UNSET_KEY}>{unset.label}</SelectItem>}
        {options.map((o, i) => (
          <SelectItem key={i} value={optionKey(i)}>
            {o.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

export function SelectField<T = string>({ label, error, hint, ...choice }: ChoiceProps<T> & { label: string; error?: string; hint?: string }) {
  return <Field label={label} error={error} hint={hint}>{(p) => <SelectControl {...choice} {...p} />}</Field>;
}

/** A labelled checkbox (switches an optional settings group on or off). */
export function CheckField({ label, checked, onChange, hint }: { label: string; checked: boolean; onChange: (v: boolean) => void; hint?: string }) {
  const id = useId();
  const descId = hint ? `${id}-d` : undefined;
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center gap-2">
        <Checkbox id={id} checked={checked} aria-describedby={descId} onCheckedChange={(value) => onChange(value === true)} />
        <Label htmlFor={id} className="cursor-pointer font-normal">
          {label}
        </Label>
      </div>
      {hint && (
        <FieldDescription id={descId}>{hint}</FieldDescription>
      )}
    </div>
  );
}

interface FormDialogProps {
  open: boolean;
  title: string;
  submitLabel?: string;
  submitDisabled?: boolean;
  onClose: () => void;
  /** Return false to keep the dialog open (validation failed). Throw to show a server error. */
  onSubmit: () => Promise<boolean | void> | boolean | void;
  children: ReactNode;
  variant?: "dialog" | "drawer" | "wide-drawer";
}

export function FormDialog({ open, title, submitLabel = "Save", submitDisabled = false, onClose, onSubmit, children, variant }: FormDialogProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if ((await onSubmit()) !== false) onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  const formId = useId();
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={title}
      variant={variant}
      footer={
        <>
          <Button type="button" variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" form={formId} disabled={busy || submitDisabled}>
            {busy ? "Saving…" : submitLabel}
          </Button>
        </>
      }
    >
      <form id={formId} onSubmit={submit} noValidate className="flex flex-col gap-3.5">
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        {children}
      </form>
    </Dialog>
  );
}
