import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

import { dialog, optionNames, pick } from "./helpers";

/*
 * Model types: the type select on the model page decides which settings show, the list filters by type,
 * a router offers only models of its own type, and usage groups and filters by endpoint and type.
 */

const BREAK_GLASS = "e2e-break-glass-password";
const admin: Record<string, string> = {};

test.beforeEach(async ({ request }) => {
  const response = await request.post("/api/v1/session/break-glass", { data: { password: BREAK_GLASS } });
  expect(response.status()).toBe(204);
  admin["X-Lumewarp-CSRF"] = (await (await request.get("/api/v1/session")).json()).csrf_token;
});

test.afterEach(async ({ request }) => {
  for (const path of ["/routers", "/aliases", "/models", "/upstreams"]) {
    const rows = (await (await request.get(`/api/v1${path}?limit=200`, { headers: admin })).json()).items as { id: string; name: string }[];
    for (const r of rows) if (r.name.startsWith("mt-")) await request.delete(`/api/v1${path}/${r.id}`, { headers: admin });
  }
});

async function signIn(page: Page, request: APIRequestContext, path: string) {
  await page.context().addCookies((await request.storageState()).cookies);
  await page.goto(path);
}

async function post(request: APIRequestContext, path: string, data: object) {
  const response = await request.post(`/api/v1${path}`, { headers: admin, data });
  expect(response.ok(), await response.text()).toBe(true);
  return response.json();
}

async function seed(request: APIRequestContext) {
  const up = await post(request, "/upstreams", { name: "mt-up", kind: "vllm", base_url: "http://127.0.0.1:9/v1" });
  const make = (name: string, model_type: string) => post(request, "/models", { name, upstream_id: up.id, upstream_model: "m", model_type });
  return {
    up,
    llm: await make("mt-llm", "llm"),
    llm2: await make("mt-llm2", "llm"),
    emb: await make("mt-emb", "embedding"),
    asr: await make("mt-asr", "transcription"),
    dec: await make("mt-dec", "decision"),
  };
}

test("the type is required and decides which settings the model page shows", async ({ page, request }) => {
  const up = await post(request, "/upstreams", { name: "mt-up", kind: "vllm", base_url: "http://127.0.0.1:9/v1" });
  await page.route(`**/api/v1/upstreams/${up.id}/models`, (route) => route.fulfill({ json: { models: [] } }));
  await signIn(page, request, "/models/new");
  const main = page.locator("#main");

  // No default type: Test now waits for it and saving is refused.
  await expect(main.getByRole("combobox", { name: "Type", exact: true })).toHaveText("Select a type…");
  await main.getByLabel("Name", { exact: true }).fill("mt-new");
  await main.getByRole("combobox", { name: "Upstream", exact: true }).click();
  await page.getByRole("option", { name: "mt-up" }).click();
  await main.getByRole("combobox", { name: "Upstream model", exact: true }).fill("m");
  await page.getByRole("option", { name: "Use “m” (custom)" }).click();
  await expect(main.getByRole("button", { name: "Test now" })).toBeDisabled();
  await main.getByRole("button", { name: "Create" }).click();
  await expect(main.getByText("Type is required")).toBeVisible();

  const price = (label: string) => main.getByLabel(label);
  await pick(main, "Type", "LLM");
  await expect(main.getByLabel("Max output (tokens)")).toBeVisible();
  await expect(main.getByText("Reasoning effort", { exact: true })).toBeVisible();
  await expect(main.getByLabel("JSON object")).toBeVisible();
  await expect(price("Output price (USD / 1M tokens)")).toBeVisible();
  await expect(price("Audio price (USD / second)")).toHaveCount(0);

  await pick(main, "Type", "Embedding");
  await expect(main.getByLabel("Max output (tokens)")).toHaveCount(0);
  await expect(main.getByText("Reasoning effort", { exact: true })).toHaveCount(0);
  await expect(main.getByLabel("JSON object")).toBeVisible();
  await expect(price("Input price (USD / 1M tokens)")).toBeVisible();
  await expect(price("Output price (USD / 1M tokens)")).toHaveCount(0);
  await expect(main.getByText("Periodic health check", { exact: true })).toBeVisible();
  await expect(main.getByRole("button", { name: "Test now" })).toBeEnabled();

  await pick(main, "Type", "Transcription");
  await expect(main.getByLabel("JSON object")).toHaveCount(0);
  await expect(main.getByLabel("Max output (tokens)")).toHaveCount(0);
  await expect(price("Output price (USD / 1M tokens)")).toBeVisible();
  await expect(price("Audio price (USD / second)")).toBeVisible();

  await pick(main, "Type", "Decision");
  await expect(main.getByLabel("Max output (tokens)")).toHaveCount(0);
  await expect(main.getByText("Reasoning effort", { exact: true })).toHaveCount(0);
  await expect(main.getByLabel("JSON object")).toBeVisible();
  await expect(price("Output price (USD / 1M tokens)")).toBeVisible();
  await expect(price("Audio price (USD / second)")).toHaveCount(0);

  // Test now carries the type; a transcription model saves its audio price in nano-USD per second.
  const sent: any[] = [];
  await page.route("**/api/v1/models/test", async (route) => {
    sent.push(route.request().postDataJSON());
    await route.fulfill({ json: { credential: "none", probe: { url: "http://127.0.0.1:9/v1/audio/transcriptions", ok: true, status: 200, latency_ms: 3, error: null }, reply: null } });
  });
  await pick(main, "Type", "Transcription");
  await price("Audio price (USD / second)").fill("0.0001");
  await main.getByRole("button", { name: "Test now" }).click();
  await expect(main.getByTestId("model-test-result")).toContainText("HTTP 200");
  expect(sent[0].model_type).toBe("transcription");
  await main.getByRole("button", { name: "Create" }).click();
  await expect(page).toHaveURL(/\/models$/);
  const stored = ((await (await request.get("/api/v1/models?limit=200", { headers: admin })).json()).items as any[]).find((m) => m.name === "mt-new");
  expect(stored.model_type).toBe("transcription");
  expect(stored.price_nano_usd_per_audio_second).toBe(100_000);
  expect(stored.default_body).toBeNull();
});

test("the models list shows the type, filters by it and searches it", async ({ page, request }) => {
  await seed(request);
  await signIn(page, request, "/models");
  const rows = page.getByRole("row");
  await expect(rows.filter({ hasText: "mt-emb" })).toContainText("Embedding");
  await expect(rows.filter({ hasText: "mt-asr" })).toContainText("Transcription");

  await pick(page, "Filter by type", "Embedding");
  await expect(rows.filter({ hasText: "mt-emb" })).toHaveCount(1);
  await expect(rows.filter({ hasText: "mt-llm" })).toHaveCount(0);

  await pick(page, "Filter by type", "All types");
  await page.getByLabel("Search models").fill("decision");
  await expect(rows.filter({ hasText: "mt-dec" })).toHaveCount(1);
  await expect(rows.filter({ hasText: "mt-llm" })).toHaveCount(0);
});

test("changing the type of a model a router uses shows the refusal on the type", async ({ page, request }) => {
  const m = await seed(request);
  await post(request, "/routers", { name: "mt-router", members: [{ model_id: m.llm.id }, { model_id: m.llm2.id }] });
  await signIn(page, request, `/models/${m.llm.id}`);
  const main = page.locator("#main");
  await pick(main, "Type", "Embedding");
  await main.getByRole("button", { name: "Save" }).click();
  await expect(main.getByText(/router/i).first()).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`/models/${m.llm.id}$`));
});

test("a router offers only models of its type, and aliases and the list show it", async ({ page, request }) => {
  const m = await seed(request);
  await signIn(page, request, "/routers");
  await page.getByRole("button", { name: "New router" }).click();
  const d = dialog(page);
  // Nothing chosen: every model is offered.
  for (const name of ["mt-llm", "mt-emb", "mt-asr", "mt-dec"]) await expect(d.getByTestId(`member-${name}`)).toBeVisible();
  // A lone member does not pin the type: picking another type replaces it.
  await d.getByRole("checkbox", { name: "mt-emb" }).check();
  await expect(d.getByTestId("member-mt-llm")).toBeVisible();
  await d.getByRole("checkbox", { name: "mt-llm", exact: true }).check();
  await expect(d.getByRole("checkbox", { name: "mt-emb" })).not.toBeChecked();
  // With a member of a type, only that type is offered, for members and fallbacks.
  await d.getByRole("checkbox", { name: "mt-llm2" }).check();
  for (const name of ["mt-emb", "mt-asr", "mt-dec"]) await expect(d.getByTestId(`member-${name}`)).toHaveCount(0);
  expect((await optionNames(d, "Add fallback model")).filter((n) => n.startsWith("mt-"))).toEqual(["mt-llm", "mt-llm2"]);
  await page.keyboard.press("Escape");

  await post(request, "/routers", { name: "mt-router", members: [{ model_id: m.asr.id }] });
  await post(request, "/aliases", { name: "mt-alias", target: m.dec.id });
  await page.reload();
  await expect(page.getByRole("row").filter({ hasText: "mt-router" })).toContainText("Transcription");
  await page.goto("/aliases");
  await expect(page.getByRole("row").filter({ hasText: "mt-alias" })).toContainText("Decision");
});

test("usage groups and filters by endpoint and model type", async ({ page, request }) => {
  await signIn(page, request, "/usage");
  const sent = page.waitForRequest((r) => r.url().includes("/api/v1/usage") && r.url().includes("group_by=endpoint") && r.url().includes("model_type=embedding"));
  await pick(page, "Model type", "Embedding");
  await pick(page, "Group by", "Endpoint");
  expect(new URL((await sent).url()).searchParams.get("endpoint")).toBeNull();
  await expect(page.getByRole("combobox", { name: "Group by" })).toHaveText("Endpoint");

  const filtered = page.waitForRequest((r) => r.url().includes("/api/v1/usage") && r.url().includes("endpoint=audio_transcriptions"));
  await pick(page, "Endpoint", "Audio transcriptions");
  await filtered;
  await pick(page, "Group by", "Model type");
  await expect(page.getByRole("combobox", { name: "Group by" })).toHaveText("Model type");
});
