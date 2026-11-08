import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

import { pick, shot } from "./helpers";

/*
 * The A2A agents pages: an agent's outbound auth and credential, its middleware, and the card the edit page
 * previews. The plugins come from the gateways serve.py seeds; what a save stored is checked through the API.
 * A card a gateway fetched cannot be produced over HTTP, so that one test mocks the list response.
 */

const BREAK_GLASS = "e2e-break-glass-password";
const admin: Record<string, string> = {};

test.beforeEach(async ({ request }) => {
  const response = await request.post("/api/v1/session/break-glass", { data: { password: BREAK_GLASS } });
  expect(response.status()).toBe(204);
  admin["X-Lumewarp-CSRF"] = (await (await request.get("/api/v1/session")).json()).csrf_token;
});

test.afterEach(async ({ request }) => {
  const listed = await request.get("/api/v1/a2a-agents?limit=200", { headers: admin });
  expect(listed.status(), await listed.text()).toBe(200);
  const items = ((await listed.json()) as { items: { id: string; name: string }[] }).items;
  for (const row of items) {
    if (!row.name.startsWith("a2a-")) continue;
    const response = await request.delete(`/api/v1/a2a-agents/${row.id}`, { headers: admin });
    expect(response.status(), `delete ${row.name}: ${await response.text()}`).toBe(204);
  }
});

async function signIn(page: Page, request: APIRequestContext) {
  await page.context().addCookies((await request.storageState()).cookies);
}

const CARD = {
  name: "Echo agent",
  description: "Echoes the message back.",
  version: "1.2.3",
  defaultInputModes: ["text/plain"],
  defaultOutputModes: ["application/json"],
  capabilities: { streaming: true, pushNotifications: false },
  skills: [
    { id: "echo", name: "Echo", description: "Returns the input.", tags: ["demo"], inputModes: ["text/plain"] },
  ],
};

const agentRow = (over: Record<string, unknown>) => ({
  id: "00000000-0000-4000-8000-000000000777",
  name: "a2a-mocked",
  url: "https://agent.test/a2a",
  card_url: "",
  auth: { plugin: "none", params: {}, timeout_ms: 0 },
  secret_ref: null,
  secret_set: false,
  middleware: null,
  timeout_ms: 0,
  card_json: "",
  last_card_json: "",
  last_card_error: "",
  last_card_at: null,
  config_invalid: false,
  config_problems: [],
  created_at: "2026-11-08T09:00:00Z",
  updated_at: "2026-11-08T10:00:00Z",
  ...over,
});

test("create an agent with a stored token and a pasted card, and the list shows it", async ({ page, request }) => {
  await signIn(page, request);
  await page.goto("/a2a-agents/new");
  const main = page.locator("#main");
  await main.getByLabel("Name", { exact: true }).fill("a2a-create");
  await main.getByLabel("Agent URL").fill("https://agent.test/a2a");
  await main.getByLabel("Request timeout (ms)").fill("45000");
  await pick(page.getByRole("group", { name: "Upstream auth", exact: true }), "Plugin", "static_bearer");
  await main.getByLabel("Token").fill("s3cret-token");
  await main.getByLabel("Pasted Card JSON").fill(JSON.stringify(CARD, null, 2));

  const preview = main.getByTestId("card-preview");
  await expect(preview).toContainText("Echo agent");
  await expect(preview).toContainText("1.2.3");
  await expect(preview).toContainText("Echoes the message back.");
  await expect(preview).toContainText("Returns the input.");
  await expect(preview).toContainText("text/plain → application/json");
  await expect(preview).toContainText("streaming yes");
  await expect(preview).toContainText("pushNotifications no");
  await expect(main.getByTestId("client-path")).toHaveText("/a2a/a2a-create");
  await expect(main.getByText("of 65,536 bytes")).toBeVisible();
  await shot(page, "a2a-agent-new");

  await main.getByRole("button", { name: "Create" }).click();
  await expect(page).toHaveURL(/\/a2a-agents$/);
  const row = page.getByRole("row").filter({ hasText: "a2a-create" });
  await expect(row).toContainText("https://agent.test/a2a");
  await expect(row).toContainText("static_bearer");
  await expect(row).toContainText("Pasted");
  await shot(page, "a2a-agents-list");

  const listed = (await (await request.get("/api/v1/a2a-agents?limit=200", { headers: admin })).json()) as {
    items: { name: string; auth: unknown; secret_ref: string | null; secret_set: boolean; timeout_ms: number; middleware: unknown; card_json: string }[];
  };
  const stored = listed.items.find((item) => item.name === "a2a-create");
  expect(stored?.auth).toEqual({ plugin: "static_bearer", params: {}, timeout_ms: 0 });
  expect([stored?.secret_ref, stored?.secret_set, stored?.timeout_ms, stored?.middleware]).toEqual([null, true, 45000, null]);
  expect(JSON.parse(stored!.card_json)).toEqual(CARD);
});

test("an edit that does not touch the auth keeps the sealed token; a different plugin replaces it", async ({ page, request }) => {
  const created = await request.post("/api/v1/a2a-agents", {
    headers: admin,
    data: { name: "a2a-sealed", url: "https://agent.test/a2a", auth: { plugin: "static_bearer", secret: "keep-me" } },
  });
  expect(created.status(), await created.text()).toBe(201);
  const agent = (await created.json()) as { id: string };
  await signIn(page, request);

  await page.goto(`/a2a-agents/${agent.id}`);
  const main = page.locator("#main");
  await expect(main.getByLabel("Name", { exact: true })).toHaveValue("a2a-sealed");
  await main.getByLabel("Name", { exact: true }).fill("a2a-renamed");
  await main.getByRole("button", { name: "Save" }).click();
  await expect(page).toHaveURL(/\/a2a-agents$/);
  const renamed = (await (await request.get(`/api/v1/a2a-agents/${agent.id}`, { headers: admin })).json()) as {
    name: string;
    auth: { plugin: string };
    secret_set: boolean;
  };
  expect([renamed.name, renamed.auth.plugin, renamed.secret_set]).toEqual(["a2a-renamed", "static_bearer", true]);

  // Another plugin replaces the whole auth: passthrough takes no credential, so the sealed token goes away.
  await page.goto(`/a2a-agents/${agent.id}`);
  await pick(page.getByRole("group", { name: "Upstream auth", exact: true }), "Plugin", "passthrough");
  await main.getByRole("button", { name: "Save" }).click();
  await expect(page).toHaveURL(/\/a2a-agents$/);
  const passed = (await (await request.get(`/api/v1/a2a-agents/${agent.id}`, { headers: admin })).json()) as {
    auth: { plugin: string };
    secret_set: boolean;
  };
  expect([passed.auth.plugin, passed.secret_set]).toEqual(["passthrough", false]);

  // Back to static_bearer: the stored token cannot be resent, so a save without a new one is refused.
  await page.goto(`/a2a-agents/${agent.id}`);
  await pick(page.getByRole("group", { name: "Upstream auth", exact: true }), "Plugin", "static_bearer");
  await main.getByRole("button", { name: "Save" }).click();
  await expect(main.getByText("Enter the token")).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`/a2a-agents/${agent.id}$`));
});

test("a fetched card wins over the pasted one, with its time and error", async ({ page, request }) => {
  const fetched = {
    name: "Fetched agent",
    description: "Reported by a gateway.",
    version: "9.9.9",
    defaultInputModes: ["application/json"],
    defaultOutputModes: ["text/plain"],
    capabilities: { streaming: true },
    skills: [{ id: "s", name: "Fetched skill", outputModes: ["image/png"] }],
  };
  const row = agentRow({
    name: "a2a-fetched",
    card_json: JSON.stringify({ name: "Pasted agent", version: "1.0.0" }),
    last_card_json: JSON.stringify(fetched),
    last_card_error: "gateway: connection refused",
    last_card_at: "2026-11-08T10:00:00Z",
  });
  await page.route(/\/api\/v1\/a2a-agents\?/, (route) => route.fulfill({ json: { items: [row], next_cursor: null } }));
  await signIn(page, request);

  await page.goto(`/a2a-agents/${row.id}`);
  const preview = page.getByTestId("card-preview");
  await expect(preview).toContainText("Fetched agent");
  await expect(preview).toContainText("9.9.9");
  await expect(preview).toContainText("Fetched skill");
  await expect(preview).toContainText("image/png");
  await expect(preview).not.toContainText("Pasted agent");
  await expect(preview).toContainText("gateway: connection refused");
  await expect(preview).toContainText("Fetched by a gateway at");
  await expect(preview).toContainText("Fetch failed");
  await shot(page, "a2a-agent-fetched-card");

  await page.goto("/a2a-agents");
  await expect(page.getByRole("row").filter({ hasText: "a2a-fetched" })).toContainText("Fetch failed");
});

test("a plugin a gateway lacks needs Save anyway, and its settings and timeout are saved", async ({ page, request }) => {
  await signIn(page, request);
  await page.goto("/a2a-agents/new");
  const main = page.locator("#main");
  await main.getByLabel("Name", { exact: true }).fill("a2a-force");
  await main.getByLabel("Agent URL").fill("https://agent.test/a2a");
  const auth = page.getByRole("group", { name: "Upstream auth", exact: true });
  await pick(auth, "Plugin", "HMAC signature");
  await auth.getByLabel("Header").fill("x-signature");
  await auth.getByLabel("Timeout (ms)").fill("500");

  // Gateway B does not have it: the config it would get is rejected, so the form asks for the override.
  await expect(main.getByTestId("missing-gateways")).toContainText("e2e-gw-b");
  await main.getByRole("button", { name: "Create" }).click();
  await expect(main.getByText("Tick “Save anyway” to save while those gateways lack the plugin", { exact: true })).toBeVisible();
  await expect(page).toHaveURL(/\/a2a-agents\/new$/);
  await shot(page, "a2a-agent-force");

  await main.getByLabel("Save anyway").click();
  await main.getByRole("button", { name: "Create" }).click();
  await expect(page).toHaveURL(/\/a2a-agents$/);
  const listed = (await (await request.get("/api/v1/a2a-agents?limit=200", { headers: admin })).json()) as {
    items: {
      name: string;
      auth: { plugin: string; params: unknown; timeout_ms: number };
      secret_ref: string | null;
      secret_set: boolean;
      middleware: unknown;
    }[];
  };
  const stored = listed.items.find((item) => item.name === "a2a-force");
  expect([stored?.auth.plugin, stored?.auth.params, stored?.auth.timeout_ms]).toEqual(["hmac_signature", { header: "x-signature" }, 500]);
  expect([stored?.secret_ref, stored?.secret_set, stored?.middleware]).toEqual([null, false, null]);
});

test("middleware is saved with its settings, and turning it off sends null", async ({ page, request }) => {
  await signIn(page, request);
  await page.goto("/a2a-agents/new");
  const main = page.locator("#main");
  await main.getByLabel("Name", { exact: true }).fill("a2a-mw");
  await main.getByLabel("Agent URL").fill("https://agent.test/a2a");
  await main.getByRole("switch", { name: "Apply middleware" }).click();
  const middleware = page.getByRole("group", { name: "A2A middleware", exact: true });
  await pick(middleware, "Plugin", "Strip artifacts");
  await middleware.getByLabel("MIME type").fill("image/*");
  await middleware.getByLabel("Timeout (ms)").fill("900");
  await main.getByRole("button", { name: "Create" }).click();
  await expect(page).toHaveURL(/\/a2a-agents$/);

  const listed = async () =>
    ((await (await request.get("/api/v1/a2a-agents?limit=200", { headers: admin })).json()) as {
      items: { id: string; name: string; middleware: unknown }[];
    }).items;
  const created = (await listed()).find((agent) => agent.name === "a2a-mw");
  expect(created?.middleware).toEqual({ plugin: "strip_artifacts", params: { mime: "image/*" }, timeout_ms: 900 });
  await expect(page.getByRole("row").filter({ hasText: "a2a-mw" })).toContainText("strip_artifacts");

  await page.goto(`/a2a-agents/${created!.id}`);
  await expect(page.getByRole("group", { name: "A2A middleware", exact: true })).toContainText("Strip artifacts");
  await page.getByRole("switch", { name: "Apply middleware" }).click();
  await expect(page.getByRole("group", { name: "A2A middleware", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page).toHaveURL(/\/a2a-agents$/);
  const off = (await listed()).find((agent) => agent.name === "a2a-mw");
  expect(off?.middleware).toBeNull();
});
