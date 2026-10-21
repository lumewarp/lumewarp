import { useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useState, type FormEvent } from "react";
import { api, useMock } from "../api";
import { clearToken, setToken } from "../auth";
import { TextField } from "../components/Form";

export function Login() {
  const [token, setTok] = useState("");
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();
  const qc = useQueryClient();

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!token.trim()) return setError("Token is required");
    setBusy(true);
    setError(undefined);
    setToken(token.trim());
    try {
      await api.me();
      qc.clear();
      await navigate({ to: "/" });
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
        <p className="muted">Use a personal access token or the bootstrap admin token.</p>
        {useMock && <p className="muted">Mock mode: any token works; start it with “viewer” for a read-only session.</p>}
        <TextField label="Access token" type="password" value={token} onChange={setTok} error={error} autoFocus />
        <button className="btn btn-primary" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
