import { useId } from "react";
import { Area, AreaChart, YAxis } from "recharts";

import { ChartContainer } from "../ui/chart";

/** A thin area line over an ordered series, drawn behind a card's content. No axes, grid or tooltip. */
export function Sparkline({ values }: { values: number[] }) {
  const id = `spark-${useId().replace(/:/g, "")}`;
  const data = values.map((value, index) => ({ index, value }));
  return (
    <div className="pointer-events-none absolute inset-x-0 bottom-0 h-12" aria-hidden="true" data-testid="insight-sparkline">
      <ChartContainer config={{}} className="aspect-auto h-full w-full">
        <AreaChart data={data} accessibilityLayer={false} margin={{ top: 0, right: 0, bottom: 1, left: 0 }}>
          <defs>
            <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="var(--series-1)" stopOpacity={0.12} />
              <stop offset="100%" stopColor="var(--series-1)" stopOpacity={0} />
            </linearGradient>
          </defs>
          <YAxis hide domain={[0, (max: number) => (max > 0 ? max : 1)]} />
          <Area type="monotone" dataKey="value" stroke="var(--series-1)" strokeOpacity={0.45} strokeWidth={1.25} fill={`url(#${id})`} isAnimationActive={false} dot={false} activeDot={false} />
        </AreaChart>
      </ChartContainer>
    </div>
  );
}
