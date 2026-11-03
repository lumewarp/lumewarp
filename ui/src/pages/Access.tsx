import { Link } from "@tanstack/react-router";
import { Component, useEffect, useState, type ReactNode } from "react";

import { Alert, AlertDescription } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { Card } from "../components/ui/card";
import { hasAccess, useCan, useMe, type Access } from "../hooks";

/** `authz_unavailable` means the backend could not answer, which is not a permission decision. */
export function AuthzNotice({ className }: { className?: string }) {
  return (
    <Alert variant="warn" role="status" data-testid="authz-unavailable" className={className}>
      <AlertDescription>
        Authorization is temporarily unavailable, so admin access cannot be verified. This is not a permission
        decision. Reload to try again.
      </AlertDescription>
    </Alert>
  );
}

/** The temporary state on My keys: the caller's teams are unknown, which is not "no teams". */
export function AuthzTemporary({ onRetry }: { onRetry: () => void }) {
  return (
    <Card className="mx-auto max-w-lg items-start gap-3 p-6" data-testid="authz-temporary">
      <h1 className="m-0">Your access cannot be verified right now</h1>
      <p className="m-0 text-sm text-muted-foreground">
        Authorization is temporarily unavailable, so your teams and keys cannot be shown. This is not a
        permission decision.
      </p>
      <Button onClick={onRetry}>Retry</Button>
    </Card>
  );
}

/** The emergency session has no user, so it owns no keys and cannot join a team. */
export function BreakGlassNoKeys() {
  return (
    <Card className="items-start gap-2 p-6" data-testid="break-glass-no-keys">
      <h2 className="m-0">The emergency session does not own API keys</h2>
      <p className="m-0 text-sm text-muted-foreground">
        Keys belong to a user account. Sign in with your own account to create or manage keys.
      </p>
    </Card>
  );
}

export function NoAccess() {
  return (
    <Card className="mx-auto max-w-lg items-start gap-3 p-6" data-testid="no-access">
      <h1 className="m-0">You do not have access to this page</h1>
      <p className="m-0 text-sm text-muted-foreground">Your account can manage its own API keys.</p>
      <Button asChild>
        <Link to="/my-keys">Go to My keys</Link>
      </Button>
    </Card>
  );
}

export function PageLoading({ label = "Loading…" }: { label?: string }) {
  return (
    <p className="p-8 text-center text-muted-foreground" aria-busy="true">
      {label}
    </p>
  );
}

/** A dynamic import that failed: the module never arrived, so its error is about the download. */
function isImportFailure(error: unknown) {
  const message = error instanceof Error ? error.message : String(error ?? "");
  return /dynamically imported module|error loading dynamically imported module|Importing a module script failed|module script failed/i.test(message);
}

/**
 * A route that failed. Two different stories: a chunk that never arrived (a deploy replaced the hashed files
 * while this tab was open, and the browser caches the rejected import, so Reload is the only way back), and a
 * page that loaded and then threw, which is a bug and must not be blamed on a deploy.
 */
export function PageLoadFailed({ error }: { error?: unknown }) {
  const missing = isImportFailure(error);
  return (
    <Card className="mx-auto max-w-lg items-start gap-3 p-6" data-testid="page-load-failed" data-reason={missing ? "chunk" : "render"}>
      <h1 className="m-0">{missing ? "This page could not be loaded" : "This page could not be shown"}</h1>
      {missing ? (
        <p className="m-0 text-sm text-muted-foreground">
          Its code did not arrive. That usually means a new version was deployed while this tab was open. This
          tab will not ask for it again, so Reload is the way back.
        </p>
      ) : (
        <p className="m-0 text-sm text-muted-foreground">
          Something went wrong while showing it. The rest of the app still works: open another page, or Reload
          to start this one again.
        </p>
      )}
      <Button onClick={() => location.reload()}>Reload</Button>
    </Card>
  );
}

/** Catches a page that failed to load or to render, so a route never leaves a blank screen. */
export class PageBoundary extends Component<{ children: ReactNode }, { error: unknown | null }> {
  state: { error: unknown | null } = { error: null };
  static getDerivedStateFromError(error: unknown) {
    return { error: error ?? true };
  }
  render() {
    // React logs the error and its component stack; nothing here silences it.
    return this.state.error ? <PageLoadFailed error={this.state.error} /> : this.props.children;
  }
}

/** A failed background check keeps the cached page; this notice offers the retry. */
export function CheckFailed({ onRetry }: { onRetry: () => void }) {
  return (
    <Alert variant="warn" role="status" data-testid="me-check-failed" className="mb-4">
      <AlertDescription>
        <div className="flex flex-wrap items-center gap-2">
          <span>Your access could not be refreshed. The page shows the last known state.</span>
          <Button size="sm" variant="outline" onClick={onRetry}>
            Retry
          </Button>
        </div>
      </AlertDescription>
    </Alert>
  );
}

function AuthzBlocked({ onRetry }: { onRetry: () => void }) {
  return (
    <Card className="mx-auto max-w-lg items-start gap-3 p-6" data-testid="authz-blocked">
      <h1 className="m-0">This page cannot be verified right now</h1>
      <p className="m-0 text-sm text-muted-foreground">
        Authorization is temporarily unavailable, so access cannot be checked. This is not a permission
        decision.
      </p>
      <div className="flex flex-wrap gap-2">
        <Button onClick={onRetry}>Retry</Button>
        <Button variant="outline" asChild>
          <Link to="/my-keys">Go to My keys</Link>
        </Button>
      </div>
    </Card>
  );
}

/**
 * Revalidate `/users/me` on entering a guarded route. The five-minute query cache is for display, not
 * for deciding access: a cached allow mounts the page while a background check runs (and replaces it
 * with the no-access state if access was taken away), and a cached deny waits for the check before it
 * says no. `entry` is the route requirement or the team id: it re-runs the check for a new target.
 */
function useRevalidated(allowed: boolean, entry: string) {
  const me = useMe();
  const [checking, setChecking] = useState(!allowed);
  useEffect(() => {
    if (me.isPending) return;
    let active = true;
    void me.refetch().finally(() => {
      if (active) setChecking(false);
    });
    return () => {
      active = false;
    };
    // `allowed` is derived from the data; including it would refetch on every result.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entry, me.isPending]);
  return { me, checking };
}

/**
 * Renders a page only when the caller may see it. A hidden page opened by URL shows a short explanation
 * instead of mounting the page, so it makes no failing requests and shows no empty tables. Not security:
 * the API refuses the same actions.
 */
export function RequireAccess({ need, children }: { need: Access; children: ReactNode }) {
  const perms = useCan();
  const allowed = hasAccess(perms, need);
  const { me, checking } = useRevalidated(allowed, need);
  if (me.isPending) return <PageLoading />;
  if (me.error && !me.data) {
    return (
      <Alert variant="destructive" data-testid="state-error">
        <AlertDescription>{me.error.message}</AlertDescription>
      </Alert>
    );
  }
  const notice = me.error && me.data ? <CheckFailed onRetry={() => void me.refetch()} /> : null;
  if (me.data?.authz_unavailable) {
    return (
      <>
        {notice}
        <AuthzBlocked onRetry={() => void me.refetch()} />
      </>
    );
  }
  if (!allowed && checking) {
    return (
      <>
        {notice}
        <PageLoading label="Checking access…" />
      </>
    );
  }
  return (
    <>
      {notice}
      {allowed ? <>{children}</> : <NoAccess />}
    </>
  );
}

/** The team-specific guard: an unreadable team mounts no panels and makes no team request. */
export function RequireTeamRead({ teamId, children }: { teamId: string; children: ReactNode }) {
  const perms = useCan();
  const allowed = perms.teamRead(teamId);
  const { me, checking } = useRevalidated(allowed, teamId);
  if (me.isPending) return <PageLoading />;
  if (me.error && !me.data) {
    return (
      <Alert variant="destructive" data-testid="state-error">
        <AlertDescription>{me.error.message}</AlertDescription>
      </Alert>
    );
  }
  const notice = me.error && me.data ? <CheckFailed onRetry={() => void me.refetch()} /> : null;
  if (me.data?.authz_unavailable) {
    return (
      <>
        {notice}
        <AuthzBlocked onRetry={() => void me.refetch()} />
      </>
    );
  }
  if (!allowed && checking) {
    return (
      <>
        {notice}
        <PageLoading label="Checking access…" />
      </>
    );
  }
  return (
    <>
      {notice}
      {allowed ? <>{children}</> : <NoAccess />}
    </>
  );
}
