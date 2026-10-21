import { useMock } from "../api";
import { Badge, PageHeader } from "../components/Bits";
import { useMe } from "../hooks";
import { useTheme } from "../theme";

export function Settings() {
  const me = useMe();
  const { theme, cycle } = useTheme();
  return (
    <>
      <PageHeader title="Settings" description="Your session and UI preferences. Organization-wide settings (SSO, notifiers) arrive with later phases." />
      <section className="card pad kv">
        <h2>Session</h2>
        <dl>
          <dt>Name</dt>
          <dd>{me.data?.name ?? "…"}</dd>
          <dt>Email</dt>
          <dd>{me.data?.email ?? "…"}</dd>
          <dt>Role</dt>
          <dd>{me.data && <Badge tone="info">{me.data.role}</Badge>}</dd>
          <dt>API</dt>
          <dd>{useMock ? "In-memory mock (VITE_API_MOCK)" : "/api/v1"}</dd>
        </dl>
      </section>
      <section className="card pad kv">
        <h2>Appearance</h2>
        <button className="btn" onClick={cycle}>
          Theme: {theme}
        </button>
      </section>
    </>
  );
}
