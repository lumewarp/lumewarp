import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

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

test("Test now on a new upstream sends the typed key and saves nothing", async ({ page, request }) => {
  await signIn(page, request, "/upstreams/new");
  const sent: any[] = [];
  await page.route("**/api/v1/upstreams/test", async (route) => {
    sent.push(route.request().postDataJSON());
    await route.fulfill({ json: { credential: "entered", health: probe("http://up.test/health"), models: probe("http://up.test/v1/models") } });
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
  expect(sent).toEqual([{ base_url: "http://up.test/v1", health_check_url: null, api_key: "sk-typed-123" }]);
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
  expect([stored.upstream_model, stored.max_input_tokens, stored.max_output_tokens]).toEqual(["my-finetune", 131072, 8192]);
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
  await main.getByRole("combobox", { name: "Upstream", exact: true }).click();
  await page.getByRole("option", { name: "pg-up2" }).click();
  const picker = main.getByRole("combobox", { name: "Upstream model", exact: true });
  await picker.fill("qwen");
  await page.getByRole("option", { name: "Use “qwen” (custom)" }).click();
  await main.getByLabel("JSON object").fill('{"top_k": 20}');
  await main.getByLabel("Input length (tokens)").fill("131072");
  await expect(main.getByText("128k tokens.")).toBeVisible();
  await main.getByRole("button", { name: "Test now" }).click();
  await expect(main.getByTestId("model-test-result")).toContainText("HTTP 200");
  await expect(main.getByTestId("model-test-result")).toContainText("2");
  expect(sent).toEqual([{ upstream_id: up.id, upstream_model: "qwen", default_body: { top_k: 20 } }]);

  await main.getByLabel("Check periodically").click();
  await main.getByLabel("Interval (ms)").fill("30000");
  await main.getByLabel("Timeout (ms)").fill("5000");
  await main.getByRole("button", { name: "Create" }).click();
  await expect(page).toHaveURL(/\/models$/);
  const row = page.getByRole("row", { name: /pg-checked/ });
  await expect(row).toContainText("Unknown");
  await expect(row).toContainText("128k / —");
  const stored = ((await (await request.get("/api/v1/models?limit=200", { headers: admin })).json()).items as any[]).find((m) => m.name === "pg-checked");
  expect(stored.chat_check).toEqual({ interval_ms: 30000, timeout_ms: 5000, unhealthy_threshold: null, healthy_threshold: null });
});

test("reasoning effort: switch on, add custom levels, set the default, and store them", async ({ page, request }) => {
  const up = await (await request.post("/api/v1/upstreams", { headers: admin, data: { name: "pg-up3", kind: "vllm", base_url: "http://127.0.0.1:9/v1" } })).json();
  await page.route(`**/api/v1/upstreams/${up.id}/models`, (route) => route.fulfill({ json: { models: [] } }));
  await signIn(page, request, "/models/new");
  const main = page.locator("#main");
  await main.getByLabel("Name", { exact: true }).fill("pg-reasoning");
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
