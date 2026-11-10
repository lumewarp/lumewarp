import { AudioLinesIcon, MessageSquareTextIcon, SplitIcon, WaypointsIcon, type LucideIcon } from "lucide-react";

import type { Schemas } from "../api";

export type ModelType = Schemas["ModelType"];

export const MODEL_TYPES: { value: ModelType; label: string }[] = [
  { value: "llm", label: "LLM" },
  { value: "embedding", label: "Embedding" },
  { value: "transcription", label: "Transcription" },
  { value: "decision", label: "Decision" },
];

/** One icon per type, beside its label wherever a type is shown. */
export const TYPE_ICON: Record<ModelType, LucideIcon> = {
  llm: MessageSquareTextIcon,
  embedding: WaypointsIcon,
  transcription: AudioLinesIcon,
  decision: SplitIcon,
};

export const typeLabel = (t: ModelType | null | undefined): string => MODEL_TYPES.find((x) => x.value === t)?.label ?? "—";

/** The type a router takes: that of its first member (plain, then split members, then fallbacks); undefined while it has none. */
export function typeOfFirst(ids: string[], typeById: (id: string) => ModelType | undefined): ModelType | undefined {
  for (const id of ids) {
    const t = typeById(id);
    if (t) return t;
  }
  return undefined;
}
