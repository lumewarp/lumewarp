import { useEffect, useState } from "react";

/** What the usage charts draw. Tokens, requests and errors are always known; cost is not (a model without a price). */
export type UsageMetric = "tokens" | "cost" | "requests" | "errors";

export const METRICS: UsageMetric[] = ["tokens", "cost", "requests", "errors"];
/** The metrics the Usage lens offers; the Cost lens draws cost and nothing else. */
export const USAGE_METRICS: UsageMetric[] = ["tokens", "requests", "errors"];

/** The page-level view of the LLM tab: how much traffic ("usage") or what it costs ("cost"). */
export type UsageLens = "usage" | "cost";

const LENS_KEY = "lumewarp.usage-lens";
const METRIC_KEY = "lumewarp.usage-chart-metric";
/** Before the lens existed the chart's metric was the only switch; a stored `cost` starts the Cost lens. */
const LEGACY_KEY = "lumewarp.usage-metric";

function read(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null; // storage blocked: the default, for this page load only
  }
}

function write(key: string, value: string) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* storage blocked: the choice lasts until the next page load */
  }
}

function storedLens(): UsageLens {
  const value = read(LENS_KEY);
  if (value === "usage" || value === "cost") return value;
  return read(LEGACY_KEY) === "cost" ? "cost" : "usage";
}

function storedMetric(): UsageMetric {
  for (const key of [METRIC_KEY, LEGACY_KEY]) {
    const value = read(key);
    if (USAGE_METRICS.includes(value as UsageMetric)) return value as UsageMetric;
  }
  return "tokens";
}

/** The lens is a per-browser preference, a client-side view choice: localStorage, `usage` by default. */
export function useUsageLens(): [UsageLens, (lens: UsageLens) => void] {
  const [lens, setLens] = useState<UsageLens>(storedLens);
  useEffect(() => write(LENS_KEY, lens), [lens]);
  return [lens, setLens];
}

/** The Usage lens' chart metric (tokens, requests or errors), remembered apart from the lens. */
export function useUsageMetric(): [UsageMetric, (metric: UsageMetric) => void] {
  const [metric, setMetric] = useState<UsageMetric>(storedMetric);
  useEffect(() => write(METRIC_KEY, metric), [metric]);
  return [metric, setMetric];
}

export const METRIC_LABEL: Record<UsageMetric, string> = { tokens: "Tokens", cost: "Cost", requests: "Requests", errors: "Errors" };
