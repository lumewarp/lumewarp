import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { PageHeader } from "../components/Bits";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Badge } from "../components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { useAllTeamKeys, useCan, useTeams } from "../hooks";

function Counter({ label, value }: { label: string; value: string }) {
  return (
    <Card className="gap-1 p-3.5">
      <div className="text-sm text-muted-foreground">{label}</div>
      <div className="text-2xl font-bold tabular-nums">{value}</div>
    </Card>
  );
}

// Fixed sample numbers (tokens, thousands per hour) for the placeholder chart. Not real data.
const SAMPLE_SERIES = [
  { name: "Model A", fill: "fill-primary", dot: "bg-primary", values: [42, 38, 30, 26, 24, 32, 55, 78, 92, 88, 80, 74] },
  { name: "Model B", fill: "fill-info", dot: "bg-info", values: [20, 18, 14, 12, 12, 16, 28, 36, 44, 42, 38, 34] },
  { name: "Model C", fill: "fill-success", dot: "bg-success", values: [8, 7, 6, 5, 5, 7, 12, 16, 20, 18, 16, 14] },
] as const;
const SAMPLE_HOURS = ["00", "02", "04", "06", "08", "10", "12", "14", "16", "18", "20", "22"];
const CHART_W = 480;
const CHART_H = 160;
const BAR_W = 24;
const SLOT = CHART_W / SAMPLE_HOURS.length;
const MAX = Math.max(...SAMPLE_HOURS.map((_, h) => SAMPLE_SERIES.reduce((sum, s) => sum + s.values[h], 0)));

function SampleUsage() {
  return (
    <Card className="mt-4">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          Usage
          <Badge variant="warn">Sample</Badge>
        </CardTitle>
        <CardDescription>Sample data, not real usage. Live figures are on the Usage page.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs" aria-label="Sample series">
          {SAMPLE_SERIES.map((s) => (
            <li key={s.name} className="inline-flex items-center gap-1.5">
              <span className={`size-2.5 rounded-sm ${s.dot}`} aria-hidden="true" />
              {s.name}
            </li>
          ))}
        </ul>
        <svg
          role="img"
          aria-label="Sample data, not real usage: stacked bars of tokens per two-hour slot for three example models"
          viewBox={`0 0 ${CHART_W} ${CHART_H + 20}`}
          className="h-auto w-full text-muted-foreground"
        >
          {[0.25, 0.5, 0.75, 1].map((f) => (
            <line key={f} x1={0} x2={CHART_W} y1={CHART_H - CHART_H * f} y2={CHART_H - CHART_H * f} className="stroke-border" strokeWidth={1} />
          ))}
          <line x1={0} x2={CHART_W} y1={CHART_H} y2={CHART_H} className="stroke-border" strokeWidth={1} />
          {SAMPLE_HOURS.map((hour, h) => {
            const x = h * SLOT + (SLOT - BAR_W) / 2;
            let y = CHART_H;
            return (
              <g key={hour}>
                {SAMPLE_SERIES.map((s) => {
                  const height = (s.values[h] / MAX) * (CHART_H - 4);
                  y -= height;
                  return <rect key={s.name} x={x} y={y} width={BAR_W} height={height} rx={2} className={s.fill} />;
                })}
                <text x={x + BAR_W / 2} y={CHART_H + 14} textAnchor="middle" fontSize={10} className="fill-current">
                  {hour}
                </text>
              </g>
            );
          })}
        </svg>
      </CardContent>
    </Card>
  );
}

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
      <div className="grid grid-cols-[repeat(auto-fit,minmax(160px,1fr))] gap-3">
        <Counter label="Teams" value={dash(teams.data?.length)} />
        <Counter label="Active keys" value={dash(active)} />
        <Counter label="Routers" value={dash(routers.data?.length)} />
        {perms.isSuper && <Counter label="Config version" value={latest ? `v${latest}` : "…"} />}
        {perms.isSuper && <Counter label="Gateways connected" value={gateways.data ? `${gateways.data.filter((g) => g.connected).length} / ${gateways.data.length}` : "…"} />}
      </div>
      {(teams.error || keys.error) && (
        <Alert variant="destructive" className="mt-4" data-testid="state-error">
          <AlertDescription>{(teams.error ?? keys.error)?.message}</AlertDescription>
        </Alert>
      )}
      <SampleUsage />
    </>
  );
}
