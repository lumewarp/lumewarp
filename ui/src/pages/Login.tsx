import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useEffect, useState, type FormEvent } from "react";
import { api, client } from "../api";
import { TextField } from "../components/Form";

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
    <div className="login">
      <form className="card login-card form" onSubmit={submit} noValidate>
        <div className="brand">
          <span className="brand-mark" aria-hidden="true" />
          Lumewarp
        </div>
        <h1>Sign in</h1>
        <p className="muted">
          {session.data?.oidc_enabled
            ? "Sign in with your identity provider."
            : "Single sign-on is not configured."}
        </p>
        {ssoError && <p className="form-error" role="alert">{ssoError}</p>}
        {session.data?.oidc_enabled && (
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => {
              window.location.assign(`/api/v1/session/oidc/login?next=${encodeURIComponent(next)}`);
            }}
          >
            Sign in with SSO
          </button>
        )}
        {session.data?.break_glass_enabled && (
          <>
            <p className="muted">Emergency sign-in · break-glass</p>
            <TextField label="Break-glass password" type="password" value={password} onChange={setPassword} error={error} autoFocus />
            <button className={`btn${session.data?.oidc_enabled ? "" : " btn-primary"}`} disabled={busy}>
              {busy ? "Signing in…" : "Sign in"}
            </button>
          </>
        )}
        {session.error && <p className="form-error" role="alert">{session.error.message}</p>}
      </form>
    </div>
  );
}
