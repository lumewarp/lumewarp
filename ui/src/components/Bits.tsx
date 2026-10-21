import clsx from "clsx";
import { useState, type ReactNode } from "react";
import type { KeyStatus } from "../api";

const STATUS: Record<KeyStatus, { label: string; tone: string }> = {
  active: { label: "Active", tone: "ok" },
  revoked: { label: "Revoked", tone: "muted" },
  budget_exceeded: { label: "Budget exceeded", tone: "danger" },
  suspended: { label: "Suspended", tone: "warn" },
};

export function StatusBadge({ status }: { status: KeyStatus }) {
  const s = STATUS[status];
  return <span className={`badge badge-${s.tone}`}>{s.label}</span>;
}

export function Badge({ children, tone = "muted" }: { children: ReactNode; tone?: "ok" | "warn" | "danger" | "muted" | "info" }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

export const usd = (n: number) => n.toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: n < 10 ? 2 : 0 });

/** Spend against a budget; null budget means unlimited. */
export function BudgetBar({ spent, budget }: { spent: number; budget: number | null }) {
  if (budget === null) return <span className="muted">{usd(spent)} · no limit</span>;
  const pct = budget > 0 ? Math.min(100, (spent / budget) * 100) : 100;
  const tone = pct >= 100 ? "danger" : pct >= 80 ? "warn" : "ok";
  return (
    <div className="budget">
      <div className="bar" role="progressbar" aria-valuemin={0} aria-valuemax={budget} aria-valuenow={Math.min(spent, budget)} aria-label="Budget used">
        <div className={clsx("bar-fill", `bar-${tone}`)} style={{ width: `${pct}%` }} />
      </div>
      <span className="budget-text">
        {usd(spent)} / {usd(budget)}
      </span>
    </div>
  );
}

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
        Copy this key now. It is stored only as a hash and will never be shown again.
      </p>
      <div className="secret-row">
        <code className="secret-value" tabIndex={0}>
          {secret}
        </code>
        <button className="btn" onClick={copy}>
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
