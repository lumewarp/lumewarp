import { expect, test, type Page } from "@playwright/test";

import { expectKeyPill, pick } from "./helpers";

/*
 * The request log against the real control plane: serve.py's seed_usage puts traffic through a direct
 * model, an alias, a router, and an alias to a router in the last 24 hours. Pagination and the
 * Playground attribution are mocked, because the fixture is smaller than one page.
 */

const BREAK_GLASS = "e2e-break-glass-password";

const chain = (page: Page) => page.getByTestId("request-chain");
const moreFilters = (page: Page) => page.getByRole("button", { name: /More filters/ }).click();

test.beforeEach(async ({ page, request }) => {
  const response = await request.post("/api/v1/session/break-glass", { data: { password: BREAK_GLASS } });
  expect(response.status()).toBe(204);
  await page.context().addCookies((await request.storageState()).cookies);
  await expect
    .poll(async () => ((await (await request.get("/api/v1/requests?limit=5")).json()).items as unknown[]).length)
    .toBeGreaterThan(0);
});

test("lists requests newest first, renders the name chain, and defaults to LLM for 24 hours", async ({ page }) => {
  const firstRequest = page.waitForRequest((request) => new URL(request.url()).pathname === "/api/v1/requests");
  await page.goto("/requests");
  await expect(page.getByRole("heading", { name: "Requests", level: 1 })).toBeVisible();
  const request = await firstRequest;
  const query = new URL(request.url()).searchParams;
  expect(query.get("kind")).toBe("llm");
  expect(query.get("from")).toBeNull();
  expect(query.get("to")).toBeNull();
  await expect(page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Requests", exact: true })).toBeVisible();

  for (const header of ["Date", "Requested", "Resolved", "Status", "Key", "Tokens", "Cost"]) {
    await expect(page.getByRole("columnheader", { name: header, exact: true })).toBeVisible();
  }

  // Resolved shows the serving model and nothing on hover; the route it took is in the detail sheet.
  await expect(chain(page).filter({ hasText: "use/a" }).first()).toBeVisible();
  await expect(page.locator("[data-testid=request-chain][title], [data-testid=request-requested][title]")).toHaveCount(0);

  await page.getByTestId("request-requested").getByText("use-alias", { exact: true }).first().click();
  const detail = page.getByTestId("request-detail");
  await expect(detail.getByRole("heading", { name: "use-alias" })).toBeVisible();
  const timeline = detail.getByTestId("request-timeline");
  await expect(timeline.locator("li")).toHaveText([/Received\s*use-alias/, /Alias\s*use-alias/, /Router\s*use-router/, /Model\s*use\/[ab]/, /Upstream\s*use-up.*Qwen\/UseA/, /Response\s*HTTP\s*200/]);
  await expect(detail).toContainText("Request id");
  await page.keyboard.press("Escape");
  await expect(detail).toBeHidden();

  await page.getByTestId("request-requested").getByText("use-direct", { exact: true }).first().click();
  await expect(timeline.locator("li[data-step=alias]")).toContainText("use-direct");
  await expect(timeline.locator("li[data-step=router]")).toHaveCount(0);
  await expect(timeline.locator("li[data-step=model]")).toContainText("use/c");
  await page.keyboard.press("Escape");

  // The table has no copy button; the request id is copied from the detail sheet. Status is a coloured badge.
  await expect(page.getByRole("table").getByRole("button", { name: "Copy request id" })).toHaveCount(0);
  await expect(page.getByTestId("request-status").first()).toContainText("200");
});

test("filters send the names, status, outcome, kind and request id to the API", async ({ page, request }) => {
  await page.goto("/requests");
  await expect(page.getByRole("heading", { name: "Requests", level: 1 })).toBeVisible();
  await moreFilters(page);
  await expect(page.getByRole("combobox", { name: "Kind", exact: true })).toHaveText("LLM");

  const [kindRequest] = await Promise.all([
    page.waitForRequest((r) => new URL(r.url()).searchParams.get("kind") === "mcp"),
    pick(page, "Kind", "MCP"),
  ]);
  expect(new URL(kindRequest.url()).searchParams.get("kind")).toBe("mcp");
  await pick(page, "Kind", "LLM");
  await page.keyboard.press("Escape");

  const [modelResponse] = await Promise.all([
    page.waitForResponse((r) => new URL(r.url()).pathname === "/api/v1/requests" && new URL(r.url()).searchParams.get("model") === "use/a"),
    page.getByRole("combobox", { name: "Resolved", exact: true }).fill("use/a"),
  ]);
  expect(modelResponse.ok()).toBe(true);
  await expect(chain(page).filter({ hasText: "use/b" })).toHaveCount(0);

  await page.getByRole("combobox", { name: "Resolved", exact: true }).fill("");
  await moreFilters(page);
  const [aliasResponse] = await Promise.all([
    page.waitForResponse((r) => new URL(r.url()).pathname === "/api/v1/requests" && new URL(r.url()).searchParams.get("alias") === "use-direct"),
    page.getByRole("combobox", { name: "Alias", exact: true }).fill("use-direct"),
  ]);
  expect(aliasResponse.ok()).toBe(true);
  await expect(page.getByTestId("request-requested").filter({ hasText: "use-direct" }).first()).toBeVisible();

  const [outcomeRequest] = await Promise.all([
    page.waitForRequest((r) => new URL(r.url()).searchParams.get("outcome") === "completed"),
    pick(page, "Outcome", "Completed"),
  ]);
  expect(new URL(outcomeRequest.url()).searchParams.get("outcome")).toBe("completed");

  const [statusRequest] = await Promise.all([
    page.waitForRequest((r) => new URL(r.url()).searchParams.get("status") === "200"),
    page.getByLabel("Status").fill("200"),
  ]);
  expect(new URL(statusRequest.url()).searchParams.get("status")).toBe("200");

  // The id filter is the last one; the earlier name filters would otherwise narrow the same row away.
  await page.getByRole("combobox", { name: "Alias", exact: true }).fill("");
  const listed = (await (await request.get("/api/v1/requests?kind=llm&model=use%2Fa&limit=1")).json()) as { items: { request_id: string }[] };
  const id = listed.items[0]!.request_id;
  const [idResponse] = await Promise.all([
    page.waitForResponse((r) => new URL(r.url()).pathname === "/api/v1/requests" && new URL(r.url()).searchParams.get("request_id") === id),
    page.getByLabel("Request id", { exact: true }).fill(id),
  ]);
  expect(idResponse.ok()).toBe(true);
  await expect(chain(page)).toHaveCount(1);
  await expect(chain(page)).toContainText("use/a");
});

test("follows next_cursor and shows the Playground attribution", async ({ page }) => {
  let seq = 0;
  const KEY_ID = "11111111-1111-4111-8111-111111111111";
  const uid = () => `00000000-0000-4000-8000-${String(++seq).padStart(12, "0")}`;
  const row = (requested: string, playgroundUserId: string | null = null) => ({
    request_id: uid(),
    ts: new Date().toISOString(),
    kind: "llm",
    endpoint: "chat_completions",
    requested,
    alias: "",
    alias_id: null,
    router: "",
    router_id: null,
    fallback: false,
    model: requested,
    model_id: null,
    upstream: "mock-up",
    upstream_model: "mock/model",
    attempts: 1,
    status: 200,
    outcome: "completed",
    stream: false,
    prompt_tokens: 10,
    completion_tokens: 5,
    audio_ms: 0,
    cost_nano_usd: 1000,
    usage_state: "reported",
    duration_ms: 12,
    key_id: KEY_ID,
    key: {
      id: KEY_ID,
      name: "ci-runner",
      display_prefix: "lw-3fa9c2",
      team_id: uid(),
      team_name: "Platform",
      org_id: null,
      org_name: null,
      owner_kind: "user",
      user_id: null,
      service_account_id: null,
      service_account_name: null,
      lifecycle: "active",
    },
    team_id: uid(),
    owner_kind: "user",
    owner_id: uid(),
    playground_user_id: playgroundUserId,
  });
  const first = row("first-page");
  const second = row("second-page", uid());
  await page.route("**/api/v1/requests**", async (route) => {
    const cursor = new URL(route.request().url()).searchParams.get("cursor");
    await route.fulfill({ json: cursor === "page-2" ? { items: [second], next_cursor: null } : { items: [first], next_cursor: "page-2" } });
  });

  await page.goto("/requests");
  await expect(chain(page).filter({ hasText: "first-page" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "15 (10+5)", exact: true })).toBeVisible();
  await expectKeyPill(page, page.getByRole("table"), "ci-runner", ["Platform", "lw-3fa9c2…", KEY_ID]);
  const nextPage = page.waitForRequest((request) => new URL(request.url()).searchParams.get("cursor") === "page-2");
  await page.getByRole("button", { name: "Next page" }).click();
  await nextPage;
  await expect(chain(page).filter({ hasText: "second-page" })).toBeVisible();
  // The table carries no Playground badge; a row click opens the sheet, which names who sent it from the Playground.
  await expect(page.getByRole("table")).not.toContainText("Playground");
  await page.getByRole("cell", { name: "15 (10+5)", exact: true }).click();
  await expect(page.getByTestId("request-detail-playground")).toContainText("Sent by");
  await expect(page.getByTestId("request-detail")).toContainText("15 (10 in + 5 out)");
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("request-detail")).toBeHidden();
  await page.getByRole("button", { name: "Previous page" }).click();
  await expect(chain(page).filter({ hasText: "first-page" })).toBeVisible();
});

test("Requests stays usable at 390 px and in dark mode", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto("/requests");
  await expect(page.getByRole("heading", { name: "Requests", level: 1 })).toBeVisible();
  await expect(chain(page).first()).toBeVisible();
  await expect(page.locator("html")).toHaveClass(/dark/);
  await expect(page.getByRole("columnheader", { name: "Key", exact: true })).toBeHidden();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test("Usage rows open Requests with the same names and range", async ({ page }) => {
  await page.goto("/usage");
  await expect(page.getByRole("heading", { name: "Usage", level: 1 })).toBeVisible();
  const view = page.getByRole("link", { name: "View", exact: true }).first();
  await expect(view).toBeVisible();
  const href = (await view.getAttribute("href")) ?? "";
  expect(href).toContain("/requests?");
  expect(href).toContain("from=");
  expect(href).toContain("to=");

  await view.click();
  await expect(page.getByRole("heading", { name: "Requests", level: 1 })).toBeVisible();
  await expect(page.getByLabel("From")).toHaveValue(/^\d{4}-\d{2}-\d{2}$/);
  await expect(page.getByLabel("To", { exact: true })).toHaveValue(/^\d{4}-\d{2}-\d{2}$/);
});
