/** The gateway's endpoint labels, as the usage API filters them. */
export const ENDPOINTS = [
  { value: "chat_completions", label: "Chat completions" },
  { value: "completions", label: "Completions" },
  { value: "responses", label: "Responses" },
  { value: "embeddings", label: "Embeddings" },
  { value: "audio_transcriptions", label: "Audio transcriptions" },
  { value: "audio_translations", label: "Audio translations" },
  { value: "decisions", label: "Decisions" },
  { value: "systemone", label: "SystemOne" },
];
export const endpointLabel = (value: string | null | undefined) => ENDPOINTS.find((e) => e.value === value)?.label ?? value ?? "—";
