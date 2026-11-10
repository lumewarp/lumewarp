import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useEffect, useState, type FormEvent } from "react";
import { api, client } from "../api";
import { TextField } from "../components/Form";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { Card } from "../components/ui/card";

const SSO_ERRORS: Record<string, string> = {
  invalid_state: "Your SSO sign-in expired or was already used. Please try again.",
  access_denied: "Your account was not granted access. Contact your administrator.",
  oidc_error: "Single sign-on failed. Please try again or use emergency sign-in.",
};

function intendedPath() {
  const next = new URLSearchParams(window.location.search).get("next");
  if (!next?.startsWith("/") || next.startsWith("//")) return "/";
  return new URL(next, window.location.origin).origin === window.location.origin ? next : "/";
}

export function Login() {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();
  const qc = useQueryClient();
  const session = useQuery({ queryKey: ["session"], queryFn: () => api.session(), retry: false });
  const next = intendedPath();
  const code = new URLSearchParams(window.location.search).get("error");
  const ssoError = code ? SSO_ERRORS[code] : undefined;

  useEffect(() => {
    if (session.data?.authenticated) void navigate({ href: next, replace: true });
  }, [navigate, next, session.data?.authenticated]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!password) return setError("Password is required");
    setBusy(true);
    setError(undefined);
    try {
      const result = await client.POST("/api/v1/session/break-glass", { body: { password } });
      if (!result.response.ok) {
        throw new Error(result.response.status === 429 ? "Too many login attempts. Try again later." : "Invalid password");
      }
      setPassword("");
      await api.session();
      await api.me();
      qc.clear();
      await navigate({ href: next });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign-in failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="grid min-h-screen place-items-center p-4">
      <Card className="w-[min(380px,100%)] py-7">
        <form className="flex flex-col gap-4 px-7" onSubmit={submit} noValidate>
          <div className="flex items-center gap-2.5 text-base font-bold">
            <img src="/favicon.svg" alt="" aria-hidden="true" className="size-6" />
            Lumewarp
          </div>
          <h1 className="text-xl font-semibold">Sign in</h1>
          {!session.data?.oidc_enabled && (
            <p className="text-sm text-muted-foreground">
              {session.isPending
                ? "Checking sign-in options…"
                : session.data
                  ? "Single sign-on is not configured."
                  : "Sign-in options are unavailable."}
            </p>
          )}
          {ssoError && (
            <Alert variant="destructive">
              <AlertDescription>{ssoError}</AlertDescription>
            </Alert>
          )}
          {session.data?.oidc_enabled && (
            <Button
              type="button"
              onClick={() => {
                window.location.assign(`/api/v1/session/oidc/login?next=${encodeURIComponent(next)}`);
              }}
            >
              Sign in with SSO
            </Button>
          )}
          {session.data?.break_glass_enabled && (
            <>
              <TextField label="Break-glass password" type="password" value={password} onChange={setPassword} error={error} autoFocus />
              <Button type="submit" variant={session.data?.oidc_enabled ? "outline" : "default"} disabled={busy}>
                {busy ? "Signing in…" : "Sign in"}
              </Button>
            </>
          )}
          {session.error && (
            <Alert variant="destructive">
              <AlertDescription>{session.error.message}</AlertDescription>
            </Alert>
          )}
        </form>
      </Card>
    </div>
  );
}
