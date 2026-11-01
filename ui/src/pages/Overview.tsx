import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { PageHeader } from "../components/Bits";
import { useAllTeamKeys, useCan, useTeams } from "../hooks";
import { Stat } from "./Usage";

export function Overview() {
  const perms = useCan();
  const teams = useTeams();
  const keys = useAllTeamKeys(teams.data);
  const routers = useQuery({ queryKey: ["routers"], queryFn: () => api.routers() });
  const gateways = useQuery({ queryKey: ["gateways"], queryFn: () => api.gateways(), enabled: perms.isSuper });
  const versions = useQuery({ queryKey: ["config-versions"], queryFn: () => api.configVersions(), enabled: perms.isSuper });

  const dash = (v: number | undefined) => (v === undefined ? "…" : String(v));
  const latest = versions.data?.reduce((m, v) => Math.max(m, v.version), 0);
  const active = keys.data?.filter((k) => k.lifecycle === "active" && !(k.expires_at && new Date(k.expires_at) < new Date())).length;

  return (
    <>
      <PageHeader title="Overview" description="Control-plane state at a glance." />
      <div className="stats">
        <Stat label="Teams" value={dash(teams.data?.length)} />
        <Stat label="Active keys" value={dash(active)} />
        <Stat label="Routers" value={dash(routers.data?.length)} />
        {perms.isSuper && <Stat label="Config version" value={latest ? `v${latest}` : "…"} />}
        {perms.isSuper && <Stat label="Gateways connected" value={gateways.data ? `${gateways.data.filter((g) => g.connected).length} / ${gateways.data.length}` : "…"} />}
      </div>
      {(teams.error || keys.error) && <p className="state-error" role="alert">{(teams.error ?? keys.error)?.message}</p>}
    </>
  );
}
