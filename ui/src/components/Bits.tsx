import { useState, type ReactNode } from "react";

import type { Key } from "../api";
import { Badge as ShadcnBadge } from "./ui/badge";
import { Button } from "./ui/button";

type Tone = "ok" | "warn" | "danger" | "muted" | "info";

export function KeyBadge({ k }: { k: Key }) {
  if (k.lifecycle === "revoked") return <ShadcnBadge variant="muted">Revoked</ShadcnBadge>;
  if (k.lifecycle === "suspended") return <ShadcnBadge variant="warn">Suspended</ShadcnBadge>;
  if (k.expires_at && new Date(k.expires_at) < new Date()) return <ShadcnBadge variant="danger">Expired</ShadcnBadge>;
  return <ShadcnBadge variant="ok">Active</ShadcnBadge>;
}

export function Badge({ children, tone = "muted" }: { children: ReactNode; tone?: Tone }) {
  return <ShadcnBadge variant={tone}>{children}</ShadcnBadge>;
}

const SUSPENDED_REASONS: Record<string, string> = {
  owner_removed: "owner removed from team",
  owner_deactivated: "owner deactivated",
  admin: "suspended by admin",
  owner: "suspended by owner",
};

/** Human-readable `suspended_reason` shown next to a key's lifecycle badge. */
export function SuspendedReason({ reason }: { reason: string | null }) {
  if (!reason) return null;
  return <span className="text-muted-foreground"> {SUSPENDED_REASONS[reason] ?? reason.replaceAll("_", " ")}</span>;
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
    <div className="flex flex-col gap-3">
      <p className="rounded-md bg-warning-bg px-3 py-2 text-sm text-warning" role="alert">
        Copy this secret now. It is stored only as a hash and will never be shown again.
      </p>
      <div className="flex items-stretch gap-2">
        <code
          className="secret-value min-w-0 flex-1 select-all break-all rounded-md border bg-secondary px-2.5 py-2 font-mono"
          data-testid="secret-value"
          tabIndex={0}
        >
          {secret}
        </code>
        <Button type="button" variant="outline" onClick={copy}>
          {copied ? "Copied" : "Copy"}
        </Button>
      </div>
    </div>
  );
}

export function PageHeader({ title, description, actions }: { title: string; description?: string; actions?: ReactNode }) {
  return (
    <header className="mb-5 flex flex-wrap items-start justify-between gap-4">
      <div className="min-w-0">
        <h1>{title}</h1>
        {description && <p className="mt-1 text-sm text-muted-foreground">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </header>
  );
}

export const fmtDate = (iso: string | null) => (iso ? new Date(iso).toLocaleDateString() : "—");
export const fmtDateTime = (iso: string) => new Date(iso).toLocaleString();
