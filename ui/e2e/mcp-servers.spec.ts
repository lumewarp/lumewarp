import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

import { confirmDialog, dialog, pick, shot } from "./helpers";

/*
 * The MCP screens: the list with its definition counts, the server form (name, the auth plugin and its
 * settings, a write-only token, the plugin timeout only for a WASM plugin, "Save anyway"), the definition
 * review queue with a diff of a pending hash against the approved one, and Discover now, whose token lives
 * in the dialog alone.
 *
 * The control plane in serve.py reports two connected gateways: both have the built-ins and the
 * `strip_artifacts` middleware, only `e2e-gw` has the `hmac_signature` auth plugin, so a form that names it
 * needs "Save anyway". Definition reports arrive over the gRPC stream, which a spec cannot drive, so the
 * review queue and the list's counts are answered by routes here.
 */

const BREAK_GLASS = "e2e-break-glass-password";
const admin: Record<string, string> = {};

test.beforeEach(async ({ request }) => {
  const response = await request.post("/api/v1/session/break-glass", { data: { password: BREAK_GLASS } });
  expect(response.status()).toBe(204);
  admin["X-Lumewarp-CSRF"] = (await (await request.get("/api/v1/session")).json()).csrf_token;
});

test.afterEach(async ({ request }) => {
  const listed = await request.get("/api/v1/mcp-servers?limit=200", { headers: admin });
  for (const row of ((await listed.json()).items ?? []) as { id: string; name: string }[]) {
    if (row.name.startsWith("ms-")) await request.delete(`/api/v1/mcp-servers/${row.id}`, { headers: admin });
  }
});

async function signIn(page: Page, request: APIRequestContext, path: string) {
  await page.context().addCookies((await request.storageState()).cookies);
  await page.goto(path);
}

type Server = {
  id: string;
  name: string;
  review_mode: string;
  secret_ref: string | null;
  secret_set: boolean;
  auth: { plugin: string; params: Record<string, unknown>; timeout_ms: number };
};

async function createServer(request: APIRequestContext, data: Record<string, unknown>): Promise<Server> {
  const response = await request.post("/api/v1/mcp-servers", { headers: admin, data });
  expect(response.status(), await response.text()).toBe(201);
  return (await response.json()) as Server;
}

async function serverNamed(request: APIRequestContext, name: string): Promise<Server> {
  const listed = await request.get("/api/v1/mcp-servers?limit=200", { headers: admin });
  const found = ((await listed.json()).items as Server[]).find((row) => row.name === name);
  expect(found, `server ${name} exists`).toBeTruthy();
  return found!;
}

async function serverById(request: APIRequestContext, id: string): Promise<Server> {
  const response = await request.get(`/api/v1/mcp-servers/${id}`, { headers: admin });
  expect(response.status(), await response.text()).toBe(200);
  return (await response.json()) as Server;
}

test("the MCP & A2A group holds the MCP pages, and Plugins lists every kind read-only", async ({ page, request }) => {
  await signIn(page, request, "/mcp-servers");
  await expect(page.getByRole("heading", { name: "MCP servers", level: 1 })).toBeVisible();
  const mcp = page.getByRole("group", { name: "MCP & A2A" });
  await expect(mcp.getByRole("link", { name: "MCP servers", exact: true })).toBeVisible();
  await expect(mcp.getByRole("link", { name: "A2A agents", exact: true })).toBeVisible();
  const dataPlane = page.getByRole("group", { name: "Data plane" });
  await expect(dataPlane.getByRole("link", { name: "Plugins", exact: true })).toBeVisible();

  await dataPlane.getByRole("link", { name: "Plugins", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Plugins", level: 1 })).toBeVisible();
  for (const title of ["Router strategies", "Upstream auth", "A2A middleware"]) {
    await expect(page.getByRole("heading", { name: title, level: 2 })).toBeVisible();
  }
  // Built-in, WASM-only-on-one-gateway, and a WASM middleware, each with its source and coverage.
  await expect(page.getByRole("row", { name: /static_bearer/ })).toContainText("Built-in");
  const hmac = page.getByRole("row", { name: /hmac_signature/ });
  await expect(hmac).toContainText("WASM");
  await expect(hmac).toContainText("missing: e2e-gw-b");
  await expect(hmac).toContainText("Header");
  await expect(page.getByRole("row", { name: /Strip artifacts/ })).toContainText("WASM");
  // Read-only: no upload, no edit, no delete.
  await expect(page.getByRole("button", { name: /upload|edit|delete/i })).toHaveCount(0);
  await shot(page, "plugins");
});

test("a new server takes a static bearer token, write-only, and a built-in plugin has no timeout", async ({ page, request }) => {
  await signIn(page, request, "/mcp-servers/new");
  const main = page.locator("#main");
  await main.getByLabel("Name", { exact: true }).fill("ms__bad");
  await main.getByLabel("URL").fill("https://mcp.example/mcp");
  await main.getByRole("button", { name: "Create" }).click();
  await expect(main.getByText(/two underscores in a row are refused/)).toBeVisible();

  await main.getByLabel("Name", { exact: true }).fill("ms-bearer");
  const auth = main.getByRole("group", { name: "Upstream auth" });
  await pick(auth, "Plugin", "static_bearer");
  await expect(auth.getByLabel("Timeout (ms)")).toHaveCount(0);
  // The credential field belongs to the page, not to the plugin component: only static_bearer takes one.
  await main.getByLabel("Token").fill("s3cret-mcp-token");
  await shot(page, "mcp-server-new");

  await main.getByRole("button", { name: "Create" }).click();
  await expect(page).toHaveURL(/\/mcp-servers$/);
  const row = page.getByRole("row", { name: /ms-bearer/ });
  await expect(row).toContainText("static_bearer");
  await expect(row).toContainText("TOFU");
  await expect(row).toContainText("0 pending");
  await shot(page, "mcp-servers-list");

  const saved = await serverNamed(request, "ms-bearer");
  expect([saved.auth.plugin, saved.secret_set, saved.secret_ref, saved.review_mode]).toEqual(["static_bearer", true, null, "tofu"]);
});

test("a WASM auth plugin takes a timeout, and a gateway that lacks it needs Save anyway", async ({ page, request }) => {
  await signIn(page, request, "/mcp-servers/new");
  const main = page.locator("#main");
  await main.getByLabel("Name", { exact: true }).fill("ms-hmac");
  await main.getByLabel("URL").fill("https://mcp.example/mcp");
  const auth = main.getByRole("group", { name: "Upstream auth" });
  await pick(auth, "Plugin", "HMAC signature");

  const timeout = auth.getByLabel("Timeout (ms)");
  await expect(timeout).toHaveValue("0");
  await expect(timeout).toHaveAccessibleDescription("0 uses the 60 second default.");
  await timeout.fill("1500");
  await auth.getByLabel("Header").fill("x-signature");

  const missing = main.getByTestId("missing-gateways");
  await expect(missing).toContainText("e2e-gw-b");
  await main.getByRole("button", { name: "Create" }).click();
  await expect(missing.getByText(/Tick “Save anyway”/)).toBeVisible();
  await missing.getByLabel("Save anyway").click();
  await main.getByRole("button", { name: "Create" }).click();
  await expect(page).toHaveURL(/\/mcp-servers$/);

  const saved = await serverNamed(request, "ms-hmac");
  expect(saved.auth).toMatchObject({ plugin: "hmac_signature", timeout_ms: 1500, params: { header: "x-signature" } });
});

test("a stored token survives a save that leaves the auth alone, and goes when the plugin changes", async ({ page, request }) => {
  const created = await createServer(request, { name: "ms-keep", url: "https://mcp.example/mcp", auth: { plugin: "static_bearer", secret: "keep-me" } });
  await signIn(page, request, `/mcp-servers/${created.id}`);
  const main = page.locator("#main");
  await expect(main.getByLabel("Token")).toHaveAttribute("placeholder", /Stored/);

  // Only the review mode changes: no `auth` is sent, so the sealed token stays.
  await pick(main, "Review mode", "Manual — approve every definition first");
  await main.getByRole("button", { name: "Save" }).click();
  await expect(page).toHaveURL(/\/mcp-servers$/);
  let saved = await serverById(request, created.id);
  expect([saved.review_mode, saved.auth.plugin, saved.secret_set]).toEqual(["manual", "static_bearer", true]);

  // The plugin changing replaces the whole auth, and the token with it.
  await page.goto(`/mcp-servers/${created.id}`);
  await pick(page.locator("#main"), "Plugin", "none");
  await page.locator("#main").getByRole("button", { name: "Save" }).click();
  await expect(page).toHaveURL(/\/mcp-servers$/);
  saved = await serverById(request, created.id);
  expect([saved.auth.plugin, saved.secret_set]).toEqual(["none", false]);
});

const ISO = "2026-11-05T09:00:00Z";
const definition = (state: string, hash: string, description: string) => ({
  id: `${state === "pending" ? "11111111" : "22222222"}-1111-4111-8111-111111111111`,
  kind: "tool",
  name: "echo",
  hash: hash.repeat(64).slice(0, 64),
  definition_json: JSON.stringify({ name: "echo", description, inputSchema: { type: "object" } }),
  state,
  first_seen_at: ISO,
  last_seen_at: ISO,
  decided_by_user_id: null,
  decided_at: state === "approved" ? ISO : null,
});

test("definitions: pending first, the diff against the approved hash, then approve", async ({ page, request }) => {
  const created = await createServer(request, { name: "ms-review", url: "https://mcp.example/mcp", auth: { plugin: "none" } });
  let approved = false;
  const requested: (string | null)[] = [];
  const posts: string[] = [];
  const rows = () => [definition("approved", "b", "old text"), ...(approved ? [] : [definition("pending", "a", "new text")])];
  await page.route(/\/api\/v1\/mcp-servers\/[^/]+\/definitions(\?.*)?$/, async (route) => {
    const state = new URL(route.request().url()).searchParams.get("state");
    requested.push(state);
    const items = rows()
      .filter((row) => !state || row.state === state)
      .map((row) => ({ ...row, server_id: created.id }));
    await route.fulfill({ json: { items, next_cursor: null } });
  });
  await page.route(/\/api\/v1\/mcp-servers\/[^/]+\/definitions\/[^/]+\/approve$/, async (route) => {
    posts.push(route.request().url());
    approved = true;
    await route.fulfill({ json: { ...definition("approved", "a", "new text"), server_id: created.id } });
  });

  await signIn(page, request, `/mcp-servers/${created.id}`);
  const queue = page.getByTestId("diff-tool-echo");
  await expect(queue).toContainText("against the approved bbbbbbbbbbbb");
  await expect(queue.getByTestId("definition-diff")).toContainText(/-\s+"description": "old text"/);
  await expect(queue.getByTestId("definition-diff")).toContainText(/\+\s+"description": "new text"/);
  expect(requested[0]).toBe("pending");
  const heading = page.getByText("Definitions", { exact: true });
  await expect(heading).toBeVisible();
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  await shot(page, "mcp-definitions", { fullPage: false });

  await page.getByRole("row", { name: /echo/ }).getByRole("button", { name: "Approve" }).click();
  const confirm = confirmDialog(page);
  await expect(confirm).toContainText("Approve “echo” (tool)");
  await confirm.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByText("Nothing is waiting for review.")).toBeVisible();
  expect(posts).toHaveLength(1);
  expect(posts[0]).toContain("/approve");

  // The filter is a state of its own: approved hashes are listed unchanged.
  await pick(page.locator("#main"), "Show", "Approved");
  await expect(page.getByRole("row", { name: /echo/ })).toContainText("bbbbbbbbbbbb");
  expect(requested).toContain("approved");
});

test("Discover now sends a typed token once and shows the definitions, the credential and an error", async ({ page, request }) => {
  const created = await createServer(request, { name: "ms-discover", url: "https://mcp.example/mcp", auth: { plugin: "none" } });
  const bodies: Record<string, unknown>[] = [];
  let refuse = false;
  await page.route("**/api/v1/mcp-servers/discover", async (route) => {
    bodies.push(route.request().postDataJSON() as Record<string, unknown>);
    if (refuse) {
      await route.fulfill({
        status: 422,
        json: { error: { code: "validation_error", message: "server_id: auth plugin 'hmac_signature' cannot run here; this server is observable from traffic only" } },
      });
      return;
    }
    await route.fulfill({
      json: {
        credential: "entered",
        server_id: created.id,
        server: "ms-discover",
        protocol_version: "2025-11-25",
        tools: [{ kind: "tool", name: "echo", hash: "c".repeat(64), definition_json: "{}", state: "approved" }],
        prompts: [{ kind: "prompt", name: "greet", hash: "d".repeat(64), definition_json: "{}", state: "unrecorded" }],
        truncated: false,
        recorded: true,
      },
    });
  });

  await signIn(page, request, `/mcp-servers/${created.id}`);
  await page.getByRole("button", { name: "Discover now" }).click();
  await dialog(page).getByLabel("Token").fill("typed-token");
  await dialog(page).getByRole("button", { name: "Discover", exact: true }).click();

  const result = dialog(page).getByTestId("discover-result");
  await expect(result).toContainText("Used the token typed here");
  await expect(result).toContainText("Protocol 2025-11-25");
  await expect(result).toContainText("recorded against this server");
  await expect(result).toContainText("Tools (1)");
  await expect(result).toContainText("echo");
  await expect(result).toContainText("approved");
  await expect(result).toContainText("greet");
  await expect(result).toContainText("unrecorded");
  expect(bodies[0]).toEqual({ url: "https://mcp.example/mcp", timeout_ms: 10000, server_id: created.id, token: "typed-token" });
  await shot(page, "mcp-discover", { fullPage: false });

  // Reopening starts blank: the token was the dialog's own state.
  await page.keyboard.press("Escape");
  await expect(dialog(page)).toHaveCount(0);
  await page.getByRole("button", { name: "Discover now" }).click();
  await expect(dialog(page).getByLabel("Token")).toHaveValue("");

  // A server behind a WASM auth plugin is refused by the control plane; the answer is shown as it came.
  refuse = true;
  await dialog(page).getByRole("button", { name: "Discover", exact: true }).click();
  await expect(dialog(page).getByTestId("discover-error")).toContainText("cannot run here");
});

test("the list shows a server's approved and pending definition counts", async ({ page, request }) => {
  const stub = {
    id: "33333333-1111-4111-8111-111111111111",
    name: "ms-stub",
    url: "https://mcp.example/mcp",
    auth: { plugin: "none", params: {}, timeout_ms: 0 },
    secret_ref: null,
    secret_set: false,
    timeout_ms: 0,
    review_mode: "tofu",
    definitions: { approved: 2, pending: 1, rejected: 3 },
    config_invalid: false,
    config_problems: [],
    created_at: ISO,
    updated_at: ISO,
  };
  await page.route(/\/api\/v1\/mcp-servers(\?.*)?$/, async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    await route.fulfill({ json: { items: [stub], next_cursor: null } });
  });
  await signIn(page, request, "/mcp-servers");
  const row = page.getByRole("row", { name: /ms-stub/ });
  await expect(row.getByTestId("counts-ms-stub")).toContainText("2");
  await expect(row.getByText("1 pending")).toBeVisible();
  await expect(row).toContainText("3 rejected");
});
