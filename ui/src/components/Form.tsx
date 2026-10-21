import { useId, useState, type FormEvent, type ReactNode } from "react";
import { Dialog } from "./Dialog";

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
    reset: (v: V = initial) => {
      setValues(v);
      setErrors({});
    },
  };
}

export const required = (label: string) => (v: string) => (v.trim() ? undefined : `${label} is required`);

export function Field({ label, error, hint, children }: { label: string; error?: string; hint?: string; children: (p: { id: string; "aria-invalid": boolean; "aria-describedby"?: string }) => ReactNode }) {
  const id = useId();
  const descId = error || hint ? `${id}-d` : undefined;
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {children({ id, "aria-invalid": !!error, "aria-describedby": descId })}
      {error ? (
        <p id={descId} className="field-error" role="alert">
          {error}
        </p>
      ) : (
        hint && (
          <p id={descId} className="field-hint">
            {hint}
          </p>
        )
      )}
    </div>
  );
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
}
export function TextField({ label, value, onChange, error, hint, type = "text", placeholder, autoFocus }: TextProps) {
  return (
    <Field label={label} error={error} hint={hint}>
      {(p) => <input {...p} className="input" type={type} value={value} placeholder={placeholder} autoFocus={autoFocus} onChange={(e) => onChange(e.target.value)} />}
    </Field>
  );
}

/** Number input where an empty string means "no limit" (null). */
export function NumberField({ label, value, onChange, error, hint, min = 0, step }: { label: string; value: number | null; onChange: (v: number | null) => void; error?: string; hint?: string; min?: number; step?: number }) {
  return (
    <Field label={label} error={error} hint={hint}>
      {(p) => (
        <input {...p} className="input" type="number" min={min} step={step} value={value ?? ""} placeholder="unlimited" onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))} />
      )}
    </Field>
  );
}

export function SelectField({ label, value, onChange, options, error, hint }: { label: string; value: string; onChange: (v: string) => void; options: { value: string; label: string }[]; error?: string; hint?: string }) {
  return (
    <Field label={label} error={error} hint={hint}>
      {(p) => (
        <select {...p} className="input" value={value} onChange={(e) => onChange(e.target.value)}>
          {options.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      )}
    </Field>
  );
}

/** Comma-separated list input (model allow-lists). */
export function ListField({ label, value, onChange, hint }: { label: string; value: string[]; onChange: (v: string[]) => void; hint?: string }) {
  return (
    <Field label={label} hint={hint}>
      {(p) => <input {...p} className="input" value={value.join(", ")} placeholder="all models" onChange={(e) => onChange(e.target.value.split(",").map((s) => s.trim()).filter(Boolean))} />}
    </Field>
  );
}

interface FormDialogProps {
  open: boolean;
  title: string;
  submitLabel?: string;
  onClose: () => void;
  /** Return false to keep the dialog open (validation failed). Throw to show a server error. */
  onSubmit: () => Promise<boolean | void> | boolean | void;
  children: ReactNode;
  variant?: "dialog" | "drawer";
}

export function FormDialog({ open, title, submitLabel = "Save", onClose, onSubmit, children, variant }: FormDialogProps) {
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
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" form={formId} className="btn btn-primary" disabled={busy}>
            {busy ? "Saving…" : submitLabel}
          </button>
        </>
      }
    >
      <form id={formId} onSubmit={submit} noValidate className="form">
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        {children}
      </form>
    </Dialog>
  );
}
