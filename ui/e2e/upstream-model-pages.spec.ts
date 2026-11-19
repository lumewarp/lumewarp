import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { createModel } from "./catalog-helpers";
import { pick } from "./helpers";

/*
 * The upstream and model edit pages: Test now probes the form as it stands (a typed key is sent,
 * never saved), and the model picker takes the upstream's list or any custom name, bringing the
 * listed model's lengths along.
 */

const BREAK_GLASS = "e2e-break-glass-password";
const admin: Record<string, string> = {};

test.beforeEach(async ({ request }) => {
  const response = await request.post("/api/v1/session/break-glass", { data: { password: BREAK_GLASS } });
  expect(response.status()).toBe(204);
  admin["X-Lumewarp-CSRF"] = (await (await request.get("/api/v1/session")).json()).csrf_token;
});

test.afterEach(async ({ request }) => {
  for (const path of ["/models", "/upstreams"]) {
    const rows = (await (await request.get(`/api/v1${path}?limit=200`, { headers: admin })).json()).items as { id: string; name: string }[];
    for (const r of rows) if (r.name.startsWith("pg-")) await request.delete(`/api/v1${path}/${r.id}`, { headers: admin });
  }
});

async function signIn(page: Page, request: APIRequestContext, path: string) {
  await page.context().addCookies((await request.storageState()).cookies);
  await page.goto(path);
}

const probe = (url: string) => ({ url, ok: true, status: 200, latency_ms: 3, error: null });

/** A saved upstream whose credential comes from the one upstream-auth plugin the e2e gateways report. */
async function pluginUpstream(request: APIRequestContext, name: string) {
  const response = await request.post("/api/v1/upstreams", {
    headers: admin,
    data: {
      name,
      kind: "vllm",
      base_url: "http://127.0.0.1:9/v1",
      auth: { plugin: "hmac_signature", params: { header: "x-signature" }, timeout_ms: 0 },
      // Gateway B does not report hmac_signature, so only a forced save is accepted.
      force: true,
    },
  });
  expect(response.status(), await response.text()).toBe(201);
  return (await response.json()) as { id: string; name: string };
}

test("Test now on a new upstream sends the typed key and saves nothing", async ({ page, request }) => {
  await signIn(page, request, "/upstreams/new");
  const sent: any[] = [];
  await page.route("**/api/v1/upstreams/test", async (route) => {
    sent.push(route.request().postDataJSON());
    await route.fulfill({ json: { credential: "entered", probe: probe("http://up.test/v1/models") } });
  });
  const main = page.locator("#main");
  await expect(main.getByRole("button", { name: "Test now" })).toBeDisabled();
  await main.getByLabel("Name", { exact: true }).fill("pg-new");
  await main.getByLabel("Base URL").fill("http://up.test/v1");
  await main.getByRole("combobox", { name: "Source" }).click();
  await page.getByRole("option", { name: "API key (stored encrypted)" }).click();
  await main.getByLabel("API key").fill("sk-typed-123");
  await main.getByRole("button", { name: "Test now" }).click();
  await expect(main.getByText("Tested with the key typed above.")).toBeVisible();
  await expect(main.getByTestId("upstream-test-result")).toContainText("HTTP 200");
  expect(sent).toEqual([{ base_url: "http://up.test/v1", health_url: null, api_key: "sk-typed-123" }]);
  const saved = (await (await request.get("/api/v1/upstreams?limit=200", { headers: admin })).json()).items as { name: string }[];
  expect(saved.map((u) => u.name)).not.toContain("pg-new");
});

test("the model picker offers the upstream's list and a custom name, and fills the lengths", async ({ page, request }) => {
  const up = await (await request.post("/api/v1/upstreams", { headers: admin, data: { name: "pg-up", kind: "vllm", base_url: "http://127.0.0.1:9/v1" } })).json();
  await page.route(`**/api/v1/upstreams/${up.id}/models`, (route) =>
    route.fulfill({
      json: {
        models: [
          { id: "Qwen/Qwen3-8B", max_input_tokens: 32768, max_output_tokens: 32768 },
          { id: "llama", max_input_tokens: 131072, max_output_tokens: 8192 },
        ],
      },
    }),
  );
  await signIn(page, request, "/models/new");
  const main = page.locator("#main");
  await main.getByLabel("Name", { exact: true }).fill("pg-model");
  await pick(main, "Type", "LLM");
  await main.getByRole("combobox", { name: "Upstream", exact: true }).click();
  await page.getByRole("option", { name: "pg-up" }).click();
  const picker = main.getByRole("combobox", { name: "Upstream model", exact: true });

  // From the list: the lengths come along.
  await picker.click();
  await page.getByRole("option", { name: "llama" }).click();
  await expect(picker).toHaveValue("llama");
  await expect(main.getByLabel("Input length (tokens)")).toHaveValue("131072");
  await expect(main.getByLabel("Max output (tokens)")).toHaveValue("8192");

  // A name the upstream does not list: offered as a custom entry, and kept.
  await picker.fill("my-finetune");
  await expect(page.getByRole("option", { name: "Use “my-finetune” (custom)" })).toBeVisible();
  await page.getByRole("option", { name: "Use “my-finetune” (custom)" }).click();
  await expect(picker).toHaveValue("my-finetune");
  await main.getByRole("button", { name: "Create" }).click();
  await expect(page).toHaveURL(/\/models$/);
  const stored = ((await (await request.get("/api/v1/models?limit=200", { headers: admin })).json()).items as any[]).find((m) => m.name === "pg-model");
  expect([stored.deployments[0].upstream_model, stored.max_input_tokens, stored.max_output_tokens]).toEqual(["my-finetune", 131072, 8192]);
});

test("model Test now asks the model, and the periodic check is saved", async ({ page, request }) => {
  const up = await (await request.post("/api/v1/upstreams", { headers: admin, data: { name: "pg-up2", kind: "vllm", base_url: "http://127.0.0.1:9/v1" } })).json();
  await page.route(`**/api/v1/upstreams/${up.id}/models`, (route) => route.fulfill({ json: { models: [] } }));
  const sent: any[] = [];
  await page.route("**/api/v1/models/test", async (route) => {
    sent.push(route.request().postDataJSON());
    await route.fulfill({ json: { credential: "none", probe: probe("http://127.0.0.1:9/v1/chat/completions"), reply: "2" } });
  });
  await signIn(page, request, "/models/new");
  const main = page.locator("#main");
  await expect(main.getByRole("button", { name: "Test now" })).toBeDisabled();
  await main.getByLabel("Name", { exact: true }).fill("pg-checked");
  await pick(main, "Type", "LLM");
  await main.getByRole("combobox", { name: "Upstream", exact: true }).click();
  await page.getByRole("option", { name: "pg-up2" }).click();
  const picker = main.getByRole("combobox", { name: "Upstream model", exact: true });
  await picker.fill("qwen");
  await page.getByRole("option", { name: "Use “qwen” (custom)" }).click();
  await main.getByLabel("JSON object").fill('{"top_k": 20}');
  await main.getByLabel("Input length (tokens)").fill("131072");
  await main.getByRole("button", { name: "Test now" }).click();
  await expect(main.getByTestId("model-test-result")).toContainText("HTTP 200");
  await expect(main.getByTestId("model-test-result")).toContainText("2");
  expect(sent).toEqual([{ model_type: "llm", upstream_id: up.id, upstream_model: "qwen", default_body: { top_k: 20 }, zone: "default" }]);

  await main.getByLabel("Check periodically").click();
  await main.getByLabel("Interval (ms)").fill("30000");
  await main.getByLabel("Timeout (ms)").fill("5000");
  await main.getByRole("button", { name: "Create" }).click();
  await expect(page).toHaveURL(/\/models$/);
  const row = page.getByRole("row", { name: /pg-checked/ });
  await expect(row).toContainText("Unknown");
  await expect(row).toContainText("128k / --");
  const stored = ((await (await request.get("/api/v1/models?limit=200", { headers: admin })).json()).items as any[]).find((m) => m.name === "pg-checked");
  expect(stored.deployments[0].chat_check).toEqual({ interval_ms: 30000, timeout_ms: 5000, unhealthy_threshold: null, healthy_threshold: null });
});

test("an upstream auth plugin disables Test now until the upstream is saved", async ({ page, request }) => {
  await signIn(page, request, "/upstreams/new");
  const main = page.locator("#main");
  await main.getByLabel("Name", { exact: true }).fill("pg-plugin-form");
  await main.getByLabel("Base URL").fill("http://up.test/v1");
  await pick(main, "Source", "Auth plugin");
  await pick(main, "Plugin", "HMAC signature");

  // The plugin's params schema is a form, exactly as on the MCP server page.
  await main.getByLabel("Header").fill("x-signature");
  await expect(main.getByTestId("plugin-settings-json")).toHaveCount(0);
  const testNow = main.getByRole("button", { name: "Test now" });
  await expect(testNow).toBeDisabled();
  await expect(testNow).toHaveAttribute("title", /Save the upstream first/);
  await expect(main.getByTestId("test-now-hint")).toContainText("Save the upstream first");
  await expect(main.getByTestId("missing-gateways")).toContainText("e2e-gw-b");
});

test("a saved plugin upstream tests through a gateway", async ({ page, request }) => {
  const up = await pluginUpstream(request, "pg-plugin-saved");
  const calls: string[] = [];
  await page.route(`**/api/v1/upstreams/${up.id}/test`, async (route) => {
    calls.push(route.request().method());
    await route.fulfill({ json: { credential: "plugin", probe: probe("http://127.0.0.1:9/v1/models") } });
  });
  await signIn(page, request, `/upstreams/${up.id}`);
  const main = page.locator("#main");
  const testNow = main.getByRole("button", { name: "Test now" });
  await expect(testNow).toBeEnabled();
  await testNow.click();
  await expect(main.getByText("Tested through a connected gateway with the saved auth plugin.")).toBeVisible();
  await expect(main.getByTestId("upstream-test-result")).toContainText("HTTP 200");
  expect(calls).toEqual(["POST"]);
});

test("saved plugin Test now maps gateway errors and an unapplied version", async ({ page, request }) => {
  const up = await pluginUpstream(request, "pg-plugin-errors");
  await page.route(`**/api/v1/upstreams/${up.id}/test`, (route) =>
    route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({ error: { code: "no_gateway_for_plugin", message: "no connected gateway reports hmac_signature" } }),
    }),
  );
  await signIn(page, request, `/upstreams/${up.id}`);
  let main = page.locator("#main");
  await main.getByRole("button", { name: "Test now" }).click();
  await expect(main.getByTestId("upstream-test-error")).toContainText("No connected gateway can run this probe");

  await page.unroute(`**/api/v1/upstreams/${up.id}/test`);
  await page.route(`**/api/v1/upstreams/${up.id}/test`, (route) =>
    route.fulfill({ json: { credential: "plugin", probe: { url: "http://127.0.0.1:9/v1/models", ok: false, status: null, latency_ms: 1, error: "not_found" } } }),
  );
  await page.reload();
  main = page.locator("#main");
  await main.getByRole("button", { name: "Test now" }).click();
  await expect(main.getByTestId("upstream-probe-error")).toContainText("has not applied the saved upstream version yet");
});

test("a plugin upstream makes a model's Test now wait for the save, then probe through a gateway", async ({ page, request }) => {
  const up = await pluginUpstream(request, "pg-plugin-model-up");
  const model = await createModel(request, admin, { name: "pg-plugin-model", model_type: "llm", upstream_id: up.id, upstream_model: "qwen" });

  await signIn(page, request, "/models/new");
  const main = page.locator("#main");
  await main.getByLabel("Name", { exact: true }).fill("pg-plugin-new-model");
  await pick(main, "Type", "LLM");
  await main.getByRole("combobox", { name: "Upstream", exact: true }).click();
  await page.getByRole("option", { name: "pg-plugin-model-up" }).click();
  await main.getByRole("combobox", { name: "Upstream model", exact: true }).fill("qwen");
  await page.getByRole("option", { name: "Use “qwen” (custom)" }).click();
  const testNow = main.getByRole("button", { name: "Test now" });
  await expect(testNow).toBeDisabled();
  await expect(testNow).toHaveAttribute("title", /Save the model first/);
  await expect(main.getByTestId("test-now-hint")).toContainText("Save the model first");

  await page.route(`**/api/v1/models/${model.id}/deployments/default/test`, (route) =>
    route.fulfill({ json: { credential: "plugin", probe: probe("http://127.0.0.1:9/v1/chat/completions"), reply: null } }),
  );
  await page.goto(`/models/${model.id}`);
  const saved = page.locator("#main");
  const savedTest = saved.getByRole("button", { name: "Test now" });
  await expect(savedTest).toBeEnabled();
  await savedTest.click();
  await expect(saved.getByText("Tested through a connected gateway with the saved auth plugin.")).toBeVisible();
  await expect(saved.getByTestId("model-test-result")).toContainText("HTTP 200");
});

test("reasoning effort: switch on, add custom levels, set the default, and store them", async ({ page, request }) => {
  const up = await (await request.post("/api/v1/upstreams", { headers: admin, data: { name: "pg-up3", kind: "vllm", base_url: "http://127.0.0.1:9/v1" } })).json();
  await page.route(`**/api/v1/upstreams/${up.id}/models`, (route) => route.fulfill({ json: { models: [] } }));
  await signIn(page, request, "/models/new");
  const main = page.locator("#main");
  await main.getByLabel("Name", { exact: true }).fill("pg-reasoning");
  await pick(main, "Type", "LLM");
  await main.getByRole("combobox", { name: "Upstream", exact: true }).click();
  await page.getByRole("option", { name: "pg-up3" }).click();
  await main.getByRole("combobox", { name: "Upstream model", exact: true }).fill("qwen");
  await page.getByRole("option", { name: "Use “qwen” (custom)" }).click();

  // Off by default: nothing to configure.
  await expect(main.getByTestId("reasoning-levels")).toHaveCount(0);
  await main.getByLabel("Handle reasoning effort").click();
  await main.getByRole("button", { name: "Add level" }).click();
  await main.getByLabel("Level 1 name").fill("off");
  await main.getByLabel("Level 1 fields").fill('{"chat_template_kwargs": {"enable_thinking": false}}');
  await main.getByRole("button", { name: "Add level" }).click();
  await main.getByLabel("Level 2 name").fill("deep.think");
  await main.getByLabel("Level 2 fields").fill('{"chat_template_kwargs": {"enable_thinking": true}}');
  await main.getByRole("combobox", { name: "Default level" }).click();
  await page.getByRole("option", { name: "deep.think" }).click();
  await main.getByRole("button", { name: "Create" }).click();
  await expect(page).toHaveURL(/\/models$/);
  const stored = ((await (await request.get("/api/v1/models?limit=200", { headers: admin })).json()).items as any[]).find((m) => m.name === "pg-reasoning");
  expect(stored.reasoning).toEqual({
    mode: "mapped",
    fragments: { off: { chat_template_kwargs: { enable_thinking: false } }, "deep.think": { chat_template_kwargs: { enable_thinking: true } } },
    default_level: "deep.think",
  });
});

test("reasoning effort: a level with empty fields and a passthrough without default are stored", async ({ page, request }) => {
  const up = await (await request.post("/api/v1/upstreams", { headers: admin, data: { name: "pg-up4", kind: "vllm", base_url: "http://127.0.0.1:9/v1" } })).json();
  await page.route(`**/api/v1/upstreams/${up.id}/models`, (route) => route.fulfill({ json: { models: [] } }));
  await signIn(page, request, "/models/new");
  const main = page.locator("#main");
  await main.getByLabel("Name", { exact: true }).fill("pg-empty-level");
  await pick(main, "Type", "LLM");
  await main.getByRole("combobox", { name: "Upstream", exact: true }).click();
  await page.getByRole("option", { name: "pg-up4" }).click();
  await main.getByRole("combobox", { name: "Upstream model", exact: true }).fill("qwen");
  await page.getByRole("option", { name: "Use “qwen” (custom)" }).click();
  await main.getByLabel("Handle reasoning effort").click();
  await main.getByRole("button", { name: "Add level" }).click();
  await main.getByLabel("Level 1 name").fill("none");
  await main.getByRole("button", { name: "Add level" }).click();
  await main.getByLabel("Level 2 name").fill("high");
  await main.getByLabel("Level 2 fields").fill('{"chat_template_kwargs": {"enable_thinking": true}}');
  await main.getByRole("button", { name: "Create" }).click();
  await expect(page).toHaveURL(/\/models$/);
  const find = async () => ((await (await request.get("/api/v1/models?limit=200", { headers: admin })).json()).items as any[]).find((m) => m.name === "pg-empty-level");
  const stored = await find();
  expect(stored.reasoning).toEqual({ mode: "mapped", fragments: { none: {}, high: { chat_template_kwargs: { enable_thinking: true } } }, default_level: null });

  // Reload: the empty level is still there with an empty field.
  await page.goto(`/models/${stored.id}`);
  const names = main.getByLabel(/^Level \d+ name$/);
  await expect(names).toHaveCount(2);
  const idx = (await names.evaluateAll((els) => els.map((e) => (e as HTMLInputElement).value))).indexOf("none");
  expect(idx).toBeGreaterThanOrEqual(0);
  await expect(main.getByLabel(`Level ${idx + 1} fields`)).toHaveValue("");

  // Passthrough with no default level is accepted.
  await pick(main, "Mode", "Pass through with a default level");
  await main.getByRole("button", { name: "Save" }).click();
  await expect(page).toHaveURL(/\/models$/);
  expect((await find()).reasoning.mode).toBe("passthrough");
  expect((await find()).reasoning.default_level ?? null).toBeNull();
});

