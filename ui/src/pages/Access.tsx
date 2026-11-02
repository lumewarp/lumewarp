import { Link } from "@tanstack/react-router";
import type { ReactNode } from "react";

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

/**
 * Renders a page only when the caller may see it. A hidden page opened by URL shows a short explanation
 * instead of mounting the page, so it makes no failing requests and shows no empty tables. Not security:
 * the API refuses the same actions.
 */
export function RequireAccess({ need, children }: { need: Access; children: ReactNode }) {
  const me = useMe();
  const perms = useCan();
  if (me.isPending) return <PageLoading />;
  if (me.error) {
    return (
      <Alert variant="destructive" data-testid="state-error">
        <AlertDescription>{me.error.message}</AlertDescription>
      </Alert>
    );
  }
  if (me.data?.authz_unavailable) {
    return (
      <Card className="mx-auto max-w-lg items-start gap-3 p-6" data-testid="authz-blocked">
        <h1 className="m-0">Admin pages are unavailable right now</h1>
        <p className="m-0 text-sm text-muted-foreground">
          Authorization is temporarily unavailable, so admin access cannot be checked. This is not a permission
          decision.
        </p>
        <Button asChild>
          <Link to="/my-keys">Go to My keys</Link>
        </Button>
      </Card>
    );
  }
  return hasAccess(perms, need) ? <>{children}</> : <NoAccess />;
}
