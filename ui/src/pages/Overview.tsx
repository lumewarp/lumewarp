import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { PageHeader } from "../components/Bits";
import { useAllKeys, useCan, useTree } from "../hooks";
import { Stat } from "./Usage";

export function Overview() {
  const perms = useCan();
  const tree = useTree();
  const keys = useAllKeys(tree.data?.projects);
  const routes = useQuery({ queryKey: ["routes"], queryFn: () => api.routes() });
  const gateways = useQuery({ queryKey: ["gateways"], queryFn: () => api.gateways(), enabled: perms.isSuper });
  const versions = useQuery({ queryKey: ["config-versions"], queryFn: () => api.configVersions(), enabled: perms.isSuper });

  const dash = (v: number | undefined) => (v === undefined ? "…" : String(v));
  const latest = versions.data?.reduce((m, v) => Math.max(m, v.version), 0);
  const active = keys.data?.filter((k) => k.lifecycle === "active" && !(k.expires_at && new Date(k.expires_at) < new Date())).length;

  return (
    <>
      <PageHeader title="Overview" description="Control-plane state at a glance." />
      <div className="stats">
        <Stat label="Organizations" value={dash(tree.data?.orgs.length)} />
        <Stat label="Teams" value={dash(tree.data?.teams.length)} />
        <Stat label="Projects" value={dash(tree.data?.projects.length)} />
        <Stat label="Active keys" value={dash(active)} />
        <Stat label="Routes" value={dash(routes.data?.length)} />
        {perms.isSuper && <Stat label="Config version" value={latest ? `v${latest}` : "…"} />}
        {perms.isSuper && <Stat label="Gateways connected" value={gateways.data ? `${gateways.data.filter((g) => g.connected).length} / ${gateways.data.length}` : "…"} />}
      </div>
      {(tree.error || keys.error) && <p className="state-error" role="alert">{(tree.error ?? keys.error)?.message}</p>}
    </>
  );
}
