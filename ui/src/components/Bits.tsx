import { useState, type ReactNode } from "react";
import type { Key } from "../api";

export function KeyBadge({ k }: { k: Key }) {
  if (k.lifecycle === "revoked") return <span className="badge badge-muted">Revoked</span>;
  if (k.lifecycle === "suspended") return <span className="badge badge-warn">Suspended</span>;
  if (k.expires_at && new Date(k.expires_at) < new Date()) return <span className="badge badge-danger">Expired</span>;
  return <span className="badge badge-ok">Active</span>;
}

export function Badge({ children, tone = "muted" }: { children: ReactNode; tone?: "ok" | "warn" | "danger" | "muted" | "info" }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

/** Money is stored as integer nano-USD (1 USD = 1e9). */
export const NANO = 1_000_000_000;
export const nanoToUsd = (n: number | null | undefined) => (n === null || n === undefined ? null : n / NANO);
export const usdToNano = (u: number | null) => (u === null ? null : Math.round(u * NANO));
export const fmtUsd = (nano: number | null | undefined, empty = "—") =>
  nano === null || nano === undefined ? empty : (nano / NANO).toLocaleString(undefined, { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 6 });

/** Shows a secret exactly once, with copy. Parent decides when to drop it. */
export function SecretOnce({ secret }: { secret: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(secret);
      setCopied(true);
    } catch {
      /* clipboard blocked: user can select the text */
    }
  };
  return (
    <div className="secret">
      <p className="form-warn" role="alert">
        Copy this secret now. It is stored only as a hash and will never be shown again.
      </p>
      <div className="secret-row">
        <code className="secret-value" tabIndex={0}>
          {secret}
        </code>
        <button type="button" className="btn" onClick={copy}>
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
    </div>
  );
}

export function PageHeader({ title, description, actions }: { title: string; description?: string; actions?: ReactNode }) {
  return (
    <header className="page-head">
      <div>
        <h1>{title}</h1>
        {description && <p className="muted">{description}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </header>
  );
}

export const fmtDate = (iso: string | null) => (iso ? new Date(iso).toLocaleDateString() : "—");
export const fmtDateTime = (iso: string) => new Date(iso).toLocaleString();
