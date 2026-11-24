import * as React from "react";
import * as RechartsPrimitive from "recharts";

import { cn } from "@/lib/utils";

/*
 * shadcn `chart` (recharts): a container that gives the chart its size and colour variables, and a tooltip and
 * legend that read the chart's config. A config entry's `color` is a CSS value (here the `--series-N` palette
 * tokens, which carry their own light and dark values), exposed to the chart's CSS as `--color-<key>`.
 */

export type ChartConfig = {
  [key: string]: { label?: React.ReactNode; icon?: React.ComponentType; color?: string };
};

type ChartContextProps = { config: ChartConfig };

const ChartContext = React.createContext<ChartContextProps | null>(null);

function useChart() {
  const context = React.useContext(ChartContext);
  if (!context) throw new Error("useChart must be used within a <ChartContainer />");
  return context;
}

function ChartContainer({
  id,
  className,
  children,
  config,
  style,
  ...props
}: React.ComponentProps<"div"> & {
  config: ChartConfig;
  children: React.ComponentProps<typeof RechartsPrimitive.ResponsiveContainer>["children"];
}) {
  const uniqueId = React.useId();
  const chartId = `chart-${id ?? uniqueId.replace(/:/g, "")}`;
  const colors = Object.fromEntries(
    Object.entries(config)
      .filter(([, item]) => item.color)
      .map(([key, item]) => [`--color-${key}`, item.color]),
  );
  return (
    <ChartContext.Provider value={{ config }}>
      <div
        data-slot="chart"
        data-chart={chartId}
        style={{ ...colors, ...style } as React.CSSProperties}
        className={cn(
          "flex aspect-video justify-center text-xs [&_.recharts-cartesian-axis-tick_text]:fill-muted-foreground [&_.recharts-cartesian-grid_line[stroke='#ccc']]:stroke-border/50 [&_.recharts-curve.recharts-tooltip-cursor]:stroke-border [&_.recharts-dot[stroke='#fff']]:stroke-transparent [&_.recharts-layer]:outline-hidden [&_.recharts-polar-grid_[stroke='#ccc']]:stroke-border [&_.recharts-rectangle.recharts-tooltip-cursor]:fill-muted [&_.recharts-rectangle.recharts-tooltip-cursor]:stroke-transparent [&_.recharts-reference-line_[stroke='#ccc']]:stroke-border [&_.recharts-sector]:outline-hidden [&_.recharts-sector[stroke='#fff']]:stroke-transparent [&_.recharts-surface]:outline-hidden",
          className,
        )}
        {...props}
      >
        <RechartsPrimitive.ResponsiveContainer initialDimension={{ width: 320, height: 200 }}>{children}</RechartsPrimitive.ResponsiveContainer>
      </div>
    </ChartContext.Provider>
  );
}

const ChartTooltip = RechartsPrimitive.Tooltip;

type TooltipItem = {
  name?: string | number;
  dataKey?: string | number;
  value?: number | string;
  color?: string;
  payload?: Record<string, unknown>;
};

function ChartTooltipContent({
  active,
  payload,
  className,
  label,
  labelFormatter,
  formatter,
  hideLabel = false,
  footer,
  testId,
}: {
  active?: boolean;
  payload?: TooltipItem[];
  className?: string;
  label?: React.ReactNode;
  labelFormatter?: (label: React.ReactNode, payload: TooltipItem[]) => React.ReactNode;
  formatter?: (value: number | string, name: string, item: TooltipItem) => React.ReactNode;
  hideLabel?: boolean;
  /** Below the rows, e.g. a total. */
  footer?: React.ReactNode;
  testId?: string;
}) {
  const { config } = useChart();
  if (!active || !payload?.length) return null;
  const heading = hideLabel ? null : labelFormatter ? labelFormatter(label, payload) : label;
  return (
    <div role="tooltip" data-testid={testId} className={cn("grid min-w-32 items-start gap-1.5 rounded-lg border bg-popover px-2.5 py-1.5 text-xs text-popover-foreground shadow-xl", className)}>
      {heading ? <div className="font-medium">{heading}</div> : null}
      <div className="grid gap-1.5">
        {payload.map((item, index) => {
          const key = String(item.dataKey ?? item.name ?? index);
          const entry = config[key];
          const name = String(entry?.label ?? item.name ?? key);
          return (
            <div key={key} className="flex items-center gap-2">
              <span aria-hidden="true" className="size-2.5 shrink-0 rounded-[2px]" style={{ background: entry?.color ?? item.color ?? String(item.payload?.fill ?? "") }} />
              <span className="flex-1 text-muted-foreground [overflow-wrap:anywhere]">{name}</span>
              <span className="font-mono font-medium text-foreground tabular-nums">
                {formatter && item.value !== undefined ? formatter(item.value, name, item) : item.value?.toLocaleString()}
              </span>
            </div>
          );
        })}
      </div>
      {footer}
    </div>
  );
}

const ChartLegend = RechartsPrimitive.Legend;

function ChartLegendContent({ payload, className }: { payload?: readonly { value?: unknown; dataKey?: unknown; color?: string }[]; className?: string }) {
  const { config } = useChart();
  if (!payload?.length) return null;
  return (
    <ul className={cn("m-0 flex list-none flex-wrap items-center justify-center gap-x-4 gap-y-1 p-0", className)}>
      {payload.map((item) => {
        const key = String(item.dataKey ?? item.value);
        return (
          <li key={key} className="flex items-center gap-1.5">
            <span aria-hidden="true" className="size-2 shrink-0 rounded-[2px]" style={{ background: item.color }} />
            {config[key]?.label ?? String(item.value)}
          </li>
        );
      })}
    </ul>
  );
}

export { ChartContainer, ChartLegend, ChartLegendContent, ChartTooltip, ChartTooltipContent, useChart };
