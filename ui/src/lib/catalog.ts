import type { Alias, Model, Router } from "../api";

/** Model ids a router reaches: members, every split's members, and fallbacks. */
export function routerModelIds(r: Router): string[] {
  const ids = [...r.members.map((m) => m.model_id), ...r.traffic_split.flatMap((s) => s.members.map((m) => m.model_id)), ...r.fallback_model_ids];
  return [...new Set(ids)];
}

export function usedBy(model: Model, routers: Router[], aliases: Alias[]) {
  return {
    routers: routers.filter((r) => routerModelIds(r).includes(model.id)).map((r) => r.name),
    aliases: aliases.filter((a) => a.target === model.id).map((a) => a.name),
  };
}

/** Upstream model names behind a router's members (not its fallbacks), de-duplicated and in order. */
export function routerUpstreamModels(r: Router, models: Map<string, Model>): string[] {
  const ids = [...r.members.map((m) => m.model_id), ...r.traffic_split.flatMap((s) => s.members.map((m) => m.model_id))];
  return [...new Set(ids.map((id) => models.get(id)?.upstream_model).filter((x): x is string => !!x))];
}

/** The upstream model(s) an alias finally reaches. */
export function aliasUpstreamModels(a: Alias, models: Map<string, Model>, routers: Map<string, Router>): string[] {
  if (a.target_kind === "model") {
    const m = models.get(a.target);
    return m ? [m.upstream_model] : [];
  }
  const r = routers.get(a.target);
  return r ? routerUpstreamModels(r, models) : [];
}

/** Case-insensitive "contains" over any of the texts. */
export function matches(query: string, ...texts: (string | undefined | null)[]): boolean {
  const q = query.trim().toLowerCase();
  return !q || texts.some((t) => !!t && t.toLowerCase().includes(q));
}
