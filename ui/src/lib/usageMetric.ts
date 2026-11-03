import { useEffect, useState } from "react";

/** What the usage chart draws. Tokens are always known; cost is not (a model without a price). */
export type UsageMetric = "tokens" | "cost";

const KEY = "lumewarp.usage-metric";

function stored(): UsageMetric {
  try {
    return localStorage.getItem(KEY) === "cost" ? "cost" : "tokens";
  } catch {
    return "tokens"; // storage blocked: the default, for this page load only
  }
}

/**
 * The chart metric is a per-browser preference: a client-side view choice, not a server setting, so it lives
 * in localStorage and defaults to tokens. Both pages share the hook, so the choice carries over.
 */
export function useUsageMetric(): [UsageMetric, (metric: UsageMetric) => void] {
  const [metric, setMetric] = useState<UsageMetric>(stored);
  useEffect(() => {
    try {
      localStorage.setItem(KEY, metric);
    } catch {
      /* storage blocked: the choice lasts until the next page load */
    }
  }, [metric]);
  return [metric, setMetric];
}

export const METRIC_LABEL: Record<UsageMetric, string> = { tokens: "Tokens", cost: "Cost" };
