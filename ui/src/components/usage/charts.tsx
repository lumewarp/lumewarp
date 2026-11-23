import { Bar, BarChart, CartesianGrid, Cell, Pie, PieChart, XAxis, YAxis } from "recharts";

import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from "../ui/chart";

/** One slice or bar: a name, its value and the colour it keeps in every view of the same data. */
export interface Slice {
  name: string;
  value: number;
  fill: string;
  /** The names folded into this slice (Other). */
  members?: string[];
}

const clip = (name: string) => (name.length > 18 ? `${name.slice(0, 17)}…` : name);

function Tip({ active, payload, format }: { active?: boolean; payload?: { value?: number; name?: string | number; payload?: Record<string, unknown> }[]; format: (value: number) => string }) {
  const rows = (payload ?? [])
    .filter((item) => Number(item.value) > 0)
    .map((item) => ({ ...item, dataKey: undefined, name: String(item.payload?.name ?? item.name) }));
  const members = rows.length ? (rows[0].payload?.members as string[] | undefined) : undefined;
  return (
    <ChartTooltipContent
      testId="usage-chart-tooltip"
      active={active && rows.length > 0}
      payload={rows}
      hideLabel
      formatter={(value) => format(Number(value))}
      footer={
        members?.length ? (
          <div className="text-muted-foreground [overflow-wrap:anywhere]" data-testid="usage-chart-tooltip-more">
            Other: {members.join(", ")}
          </div>
        ) : null
      }
    />
  );
}

/** Share of a total: a ring with the total in its middle. */
export function Donut({ slices, format, centre, height = 220 }: { slices: Slice[]; format: (value: number) => string; centre?: { value: string; label: string }; height?: number }) {
  const config: ChartConfig = {};
  return (
    <div className="relative">
      <ChartContainer config={config} className="aspect-auto w-full" style={{ height }}>
        <PieChart accessibilityLayer>
          <ChartTooltip content={<Tip format={format} />} />
          <Pie data={slices} dataKey="value" nameKey="name" innerRadius="55%" outerRadius="85%" paddingAngle={1} isAnimationActive={false} stroke="var(--card)">
            {slices.map((slice) => (
              <Cell key={slice.name} fill={slice.fill} />
            ))}
          </Pie>
        </PieChart>
      </ChartContainer>
      {centre && (
        <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center text-center" aria-hidden="true">
          <span className="text-xl font-bold tabular-nums">{centre.value}</span>
          <span className="text-xs text-muted-foreground">{centre.label}</span>
        </div>
      )}
    </div>
  );
}

/** Horizontal bars, largest first. */
export function RankedBars({ slices, format }: { slices: Slice[]; format: (value: number) => string }) {
  const config: ChartConfig = {};
  return (
    <ChartContainer config={config} className="aspect-auto w-full" style={{ height: 40 + slices.length * 30 }}>
      <BarChart data={slices} layout="vertical" accessibilityLayer margin={{ top: 0, right: 16, bottom: 0, left: 0 }}>
        <CartesianGrid horizontal={false} />
        <XAxis type="number" tickLine={false} axisLine={false} tickFormatter={(value: number) => format(value)} />
        <YAxis type="category" dataKey="name" tickLine={false} axisLine={false} width={120} tickFormatter={clip} />
        <ChartTooltip content={<Tip format={format} />} />
        <Bar dataKey="value" radius={3} isAnimationActive={false}>
          {slices.map((slice) => (
            <Cell key={slice.name} fill={slice.fill} />
          ))}
        </Bar>
      </BarChart>
    </ChartContainer>
  );
}

/** A legend for slices: each name with its swatch. */
export function SliceLegend({ slices, label }: { slices: Slice[]; label: string }) {
  return (
    <ul className="m-0 flex list-none flex-wrap gap-x-3 gap-y-1 p-0 text-xs" aria-label={label} data-testid="usage-chart-legend">
      {slices.map((slice) => (
        <li key={slice.name} data-series={slice.name} className="flex items-center gap-1.5">
          <span aria-hidden="true" className="size-2.5 shrink-0 rounded-sm" style={{ background: slice.fill }} />
          <span className="[overflow-wrap:anywhere]">{slice.name}</span>
        </li>
      ))}
    </ul>
  );
}
