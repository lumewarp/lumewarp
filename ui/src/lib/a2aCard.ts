import type { A2aAgent } from "../api";

/** The control plane's own bound for a pasted Agent Card (`A2aAgentCreate.card_json`). */
export const MAX_CARD_BYTES = 64 * 1024;

/** UTF-8 length, as the API counts it. */
export const utf8Bytes = (text: string) => new TextEncoder().encode(text).length;

/** Beyond this the preview lists the count instead: a card can carry up to 64 KiB of skills. */
export const PREVIEW_SKILLS = 50;

const asText = (value: unknown) => (typeof value === "string" ? value.trim() : "");
const asTexts = (value: unknown) =>
  Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

export interface CardSkill {
  id: string;
  name: string;
  description: string;
  tags: string[];
  inputModes: string[];
  outputModes: string[];
}

/** What the preview shows of a card. Every member is text: a card is untrusted data. */
export interface CardView {
  name: string;
  description: string;
  version: string;
  inputModes: string[];
  outputModes: string[];
  /** At most `PREVIEW_SKILLS`. */
  skills: CardSkill[];
  skillCount: number;
  capabilities: { name: string; on: boolean }[];
}

/**
 * A pasted or gateway-fetched Agent Card, parsed for the preview. Returns null when the text is not a
 * JSON object (`__proto__` included: `JSON.parse` keeps it as the own property it is, and it is only
 * ever shown as text).
 */
export function cardView(json: string): CardView | null {
  if (!json.trim()) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(json);
  } catch {
    return null;
  }
  if (!isObject(parsed)) return null;
  const skills: CardSkill[] = (Array.isArray(parsed.skills) ? parsed.skills : []).filter(isObject).map((skill) => ({
    id: asText(skill.id),
    name: asText(skill.name),
    description: asText(skill.description),
    tags: asTexts(skill.tags),
    inputModes: asTexts(skill.inputModes),
    outputModes: asTexts(skill.outputModes),
  }));
  const capabilities = isObject(parsed.capabilities)
    ? Object.entries(parsed.capabilities)
        .filter(([, on]) => typeof on === "boolean")
        .map(([name, on]) => ({ name, on: on === true }))
    : [];
  return {
    name: asText(parsed.name),
    description: asText(parsed.description),
    version: asText(parsed.version),
    inputModes: asTexts(parsed.defaultInputModes),
    outputModes: asTexts(parsed.defaultOutputModes),
    skills: skills.slice(0, PREVIEW_SKILLS),
    skillCount: skills.length,
    capabilities,
  };
}

export interface CardStatus {
  tone: "ok" | "info" | "danger" | "muted";
  label: string;
  title: string;
}

/** Which card a gateway serves today: a fetched one, a pasted one, or none. */
export function cardStatus(agent: Pick<A2aAgent, "card_json" | "last_card_at" | "last_card_error">): CardStatus {
  if (agent.last_card_error) {
    return { tone: "danger", label: "Fetch failed", title: agent.last_card_error };
  }
  if (agent.last_card_at) {
    return { tone: "ok", label: "Fetched", title: `Fetched by a gateway at ${new Date(agent.last_card_at).toLocaleString()}` };
  }
  if (agent.card_json.trim()) {
    return { tone: "info", label: "Pasted", title: "No gateway has fetched a card; the pasted one is served." };
  }
  return { tone: "muted", label: "None", title: "No gateway has fetched a card and none is pasted." };
}

/** Where a client reaches the agent on a gateway (the public base is a gateway setting, not an API value). */
export const clientPath = (name: string) => `/a2a/${name.trim() || "<name>"}`;
