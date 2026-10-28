import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useEffect, useState, type FormEvent } from "react";
import { api } from "../api";
import { clearToken, setToken } from "../auth";
import { TextField } from "../components/Form";

const SSO_ERRORS: Record<string, string> = {
  invalid_state: "Your SSO sign-in expired or was already used. Please try again.",
  access_denied: "Your account was not granted access. Contact your administrator.",
  oidc_error: "Single sign-on failed. Please try again or use an access token.",
};

function intendedPath() {
  const next = new URLSearchParams(window.location.search).get("next");
  if (!next?.startsWith("/") || next.startsWith("//")) return "/";
  return new URL(next, window.location.origin).origin === window.location.origin ? next : "/";
}

export function Login() {
  const [token, setTok] = useState("");
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
    if (!token.trim()) return setError("Token is required");
    setBusy(true);
    setError(undefined);
    setToken(token.trim());
    try {
      await api.me();
      qc.clear();
      await navigate({ href: next });
    } catch (err) {
      clearToken();
      setError(err instanceof Error && err.message !== "Session expired" ? err.message : "Invalid token");
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
            ? "Sign in with your identity provider or use an access token."
            : "Use a personal access token or the bootstrap admin token."}
        </p>
        {ssoError && <p className="form-error" role="alert">{ssoError}</p>}
        {session.data?.oidc_enabled && (
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => {
              clearToken();
              window.location.assign(`/api/v1/session/oidc/login?next=${encodeURIComponent(next)}`);
            }}
          >
            Sign in with SSO
          </button>
        )}
        <TextField label="Access token" type="password" value={token} onChange={setTok} error={error} autoFocus />
        <button className={`btn${session.data?.oidc_enabled ? "" : " btn-primary"}`} disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
