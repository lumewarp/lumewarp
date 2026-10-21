import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { api } from "../api";
import { Badge, BudgetBar, PageHeader } from "../components/Bits";
import { Stat } from "./Usage";

export function Overview() {
  const orgs = useQuery({ queryKey: ["orgs"], queryFn: () => api.orgs.list() });
  const keys = useQuery({ queryKey: ["keys"], queryFn: () => api.keys.list() });
  const routes = useQuery({ queryKey: ["routes"], queryFn: () => api.routes.list() });
  const cfg = useQuery({ queryKey: ["config-status"], queryFn: () => api.configStatus() });
  const active = keys.data?.filter((k) => k.status === "active").length;
  const flagged = keys.data?.filter((k) => k.status === "budget_exceeded").length;
  const dash = (v: number | undefined) => (v === undefined ? "…" : String(v));

  return (
    <>
      <PageHeader title="Overview" description="Control-plane state at a glance." />
      <div className="stats">
        <Stat label="Config version" value={cfg.data ? `v${cfg.data.version}` : "…"} />
        <Stat label="Active keys" value={dash(active)} />
        <Stat label="Over budget" value={dash(flagged)} />
        <Stat label="Routes" value={dash(routes.data?.length)} />
      </div>
      <div className="grid-2">
        <section className="card pad">
          <h2>Gateways</h2>
          {cfg.isPending ? <div className="skeleton" /> : cfg.error ? <p className="state-error">{cfg.error.message}</p> : (
            <ul className="plain">
              {cfg.data.gateways.map((g) => (
                <li key={g.id}>
                  <code>{g.id}</code> <Badge tone={g.connected ? "ok" : "danger"}>{g.connected ? "connected" : "disconnected"}</Badge> <span className="muted">at v{g.version}{g.version < cfg.data.version ? " (behind)" : ""}</span>
                </li>
              ))}
            </ul>
          )}
        </section>
        <section className="card pad">
          <h2>Organization budgets</h2>
          {orgs.isPending ? <div className="skeleton" /> : orgs.error ? <p className="state-error">{orgs.error.message}</p> : (
            <ul className="plain">
              {orgs.data.map((o) => (
                <li key={o.id}>
                  <strong>{o.name}</strong>
                  <BudgetBar spent={o.spent_usd} budget={o.budget_usd} />
                </li>
              ))}
            </ul>
          )}
          <Link to="/orgs">Manage organizations →</Link>
        </section>
      </div>
    </>
  );
}
