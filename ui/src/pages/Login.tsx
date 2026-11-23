import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDownIcon } from "lucide-react";
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
  const [emergency, setEmergency] = useState(false);
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

  const sso = session.data?.oidc_enabled;
  const breakGlass = session.data?.break_glass_enabled && (
    <div className="flex flex-col gap-4">
      <TextField label="Break-glass password" type="password" value={password} onChange={setPassword} error={error} autoFocus />
      <Button type="submit" variant={sso ? "outline" : "default"} disabled={busy}>
        {busy ? "Signing in…" : "Sign in"}
      </Button>
    </div>
  );

  return (
    <div className="grid min-h-svh place-items-center p-4 pb-[12vh]">
      <Card className="w-[min(380px,100%)] py-8">
        <form className="flex flex-col gap-5 px-7" onSubmit={submit} noValidate>
          <div className="flex flex-col items-center gap-3 text-center">
            <img src="/favicon.svg" alt="" aria-hidden="true" className="size-20" />
            <h1 className="text-xl font-semibold">
              <span className="sr-only">Sign in to </span>Lumewarp
            </h1>
          </div>
          {!sso && (
            <p className="text-center text-sm text-muted-foreground">
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
          {sso && (
            <Button
              type="button"
              size="lg"
              onClick={() => {
                window.location.assign(`/api/v1/session/oidc/login?next=${encodeURIComponent(next)}`);
              }}
            >
              Sign in with {session.data?.sso_display_name || "SSO"}
            </Button>
          )}
          {sso && breakGlass ? (
            // With SSO the emergency password is the exception, so it stays folded away.
            <div className="flex flex-col gap-4">
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="self-center text-muted-foreground"
                aria-expanded={emergency}
                aria-controls="emergency-sign-in"
                onClick={() => setEmergency((open) => !open)}
              >
                Emergency sign-in
                <ChevronDownIcon className={emergency ? "rotate-180 transition-transform" : "transition-transform"} aria-hidden="true" />
              </Button>
              {emergency && <div id="emergency-sign-in">{breakGlass}</div>}
            </div>
          ) : (
            breakGlass
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
