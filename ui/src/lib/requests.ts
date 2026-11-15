/**
 * The Requests page reads its filters from the URL, so a Usage row can open it with the same names,
 * team, key and range. Every value is a string, as a URL carries it; the page validates what it sends.
 */
export interface RequestsSearch {
  kind?: string;
  team_id?: string;
  key_id?: string;
  user_id?: string;
  service_account_id?: string;
  requested?: string;
  model?: string;
  alias?: string;
  router?: string;
  status?: string;
  outcome?: string;
  from?: string;
  to?: string;
  request_id?: string;
}

const FIELDS: (keyof RequestsSearch)[] = [
  "kind",
  "team_id",
  "key_id",
  "user_id",
  "service_account_id",
  "requested",
  "model",
  "alias",
  "router",
  "status",
  "outcome",
  "from",
  "to",
  "request_id",
];

/** A route validator: only the known string fields survive, unknown query values are dropped. */
export function requestsSearch(search: Record<string, unknown>): RequestsSearch {
  const out: RequestsSearch = {};
  for (const field of FIELDS) {
    const value = search[field];
    if (typeof value === "string") out[field] = value;
  }
  return out;
}
