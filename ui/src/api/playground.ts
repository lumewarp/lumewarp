import { createOpenAICompatible } from "@ai-sdk/openai-compatible";

import { getCsrfToken } from "../auth";
import type { Schemas } from ".";

export type PlaygroundKey = Schemas["PlaygroundKey"];

export const PLAYGROUND_BASE = "/api/v1/playground";

/** The message of an error body: the gateway's `{error:{message}}` or the control plane's `{detail}`. */
export async function errorText(response: Response): Promise<string | undefined> {
  try {
    const body = (await response.clone().json()) as { error?: { message?: string } | string; detail?: unknown };
    if (typeof body.error === "object" && body.error?.message) return body.error.message;
    if (typeof body.error === "string") return body.error;
    if (typeof body.detail === "string") return body.detail;
  } catch {
    // Not JSON: the caller falls back to the status line.
  }
  return undefined;
}

/** What the last response of one chat request looked like, filled in by the custom fetch. */
export interface ResponseInfo {
  status?: number;
  requestId?: string | null;
  error?: string;
}

/**
 * An AI SDK provider that talks to the control plane's playground proxy as the signed-in session: it adds the
 * CSRF header the api client adds and names the key to act as. `info` is written on every response, so the page
 * can show the request id and the gateway's own error message (the SDK's error text is less direct). `extra` is
 * merged into the request body as the user typed it; `model`, `messages` and `stream` stay the SDK's.
 */
export function playgroundProvider(keyId: string, info: ResponseInfo, extra: Record<string, unknown> = {}) {
  return createOpenAICompatible({
    name: "lumewarp-playground",
    // The SDK builds URLs with `new URL`, which needs an absolute one.
    baseURL: `${window.location.origin}${PLAYGROUND_BASE}/v1`,
    includeUsage: true,
    fetch: async (input, init) => {
      const headers = new Headers(init?.headers);
      headers.delete("Authorization");
      headers.set("x-lumewarp-act-as", keyId);
      const csrf = getCsrfToken();
      if (csrf) headers.set("X-Lumewarp-CSRF", csrf);
      info.status = undefined;
      info.requestId = undefined;
      info.error = undefined;
      let body = init?.body;
      if (typeof body === "string" && Object.keys(extra).length > 0) {
        const sent = JSON.parse(body) as Record<string, unknown>;
        body = JSON.stringify({ ...sent, ...extra, model: sent.model, messages: sent.messages, stream: sent.stream });
      }
      const response = await fetch(input, { ...init, body, headers, credentials: "same-origin" });
      info.status = response.status;
      info.requestId = response.headers.get("x-lumewarp-request-id");
      if (!response.ok) info.error = (await errorText(response)) ?? `HTTP ${response.status}`;
      return response;
    },
  });
}
