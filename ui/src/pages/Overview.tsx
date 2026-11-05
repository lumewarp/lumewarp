import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";

import { api } from "../api";
import { PageHeader } from "../components/Bits";
import { UsageChart } from "../components/UsageChart";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Card } from "../components/ui/card";
import { dayStart, isoDay, shiftDay } from "../lib/days";
import { useUsageMetric } from "../lib/usageMetric";
import { useAllTeamKeys, useCan, useTeams } from "../hooks";

function Counter({ label, value }: { label: string; value: string }) {
  return (
    <Card className="gap-1 p-3.5">
      <div className="text-sm text-muted-foreground">{label}</div>
      <div className="text-2xl font-bold tabular-nums">{value}</div>
    </Card>
  );
}

export function Overview() {
  const perms = useCan();
  const teams = useTeams();
  const keys = useAllTeamKeys(teams.data);
  // Routing details are installation-level reads: a team admin must not request them.
  const routers = useQuery({ queryKey: ["routers"], queryFn: () => api.routers(), enabled: perms.isInstallationAdmin });
  const gateways = useQuery({ queryKey: ["gateways"], queryFn: () => api.gateways(), enabled: perms.isInstallationAdmin });
  const versions = useQuery({ queryKey: ["config-versions"], queryFn: () => api.configVersions(), enabled: perms.isInstallationAdmin });

  // The last 14 UTC days, whatever this principal may read: the API already limits rows to readable teams.
  const [metric, setMetric] = useUsageMetric();
  const today = isoDay(new Date());
  const from = shiftDay(today, -13);
  const filters = { from: dayStart(from), to: dayStart(shiftDay(today, 1)), group_by: "day,model" };
  const usage = useQuery({
    queryKey: ["usage", filters],
    queryFn: () => api.usage(filters),
    // A refusal or an outage stays inside the card, with its own retry: it must not take the page down.
    retry: false,
  });

  const dash = (v: number | undefined) => (v === undefined ? "…" : String(v));
  const latest = versions.data?.reduce((m, v) => Math.max(m, v.version), 0);
  const active = keys.data?.filter((k) => k.lifecycle === "active" && !(k.expires_at && new Date(k.expires_at) < new Date())).length;

  return (
    <>
      <PageHeader title="Overview" description="Control-plane state at a glance." />
      <div className="grid grid-cols-[repeat(auto-fit,minmax(160px,1fr))] gap-3">
        <Counter label="Teams" value={dash(teams.data?.length)} />
        <Counter label="Active keys" value={dash(active)} />
        {perms.isInstallationAdmin && <Counter label="Routers" value={dash(routers.data?.length)} />}
        {perms.isInstallationAdmin && <Counter label="Config version" value={latest ? `v${latest}` : "…"} />}
        {perms.isInstallationAdmin && <Counter label="Gateways connected" value={gateways.data ? `${gateways.data.filter((g) => g.connected).length} / ${gateways.data.length}` : "…"} />}
      </div>
      {(teams.error || keys.error || (perms.isInstallationAdmin && routers.error)) && (
        <Alert variant="destructive" className="mt-4" data-testid="state-error">
          <AlertDescription>{(teams.error ?? keys.error ?? routers.error)?.message}</AlertDescription>
        </Alert>
      )}
      <div className="mt-4">
        <UsageChart
          rows={usage.data}
          from={from}
          to={today}
          metric={metric}
          onMetricChange={setMetric}
          loading={usage.isPending}
          error={usage.error}
          onRetry={() => void usage.refetch()}
          empty="No usage in the last 14 days."
          title="Usage"
          footer={
            <Link to="/usage" className="font-medium">
              Open Usage
            </Link>
          }
        />
      </div>
    </>
  );
}
