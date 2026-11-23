import { expect, test, type Page, type Response } from "@playwright/test";

import { moreFilters, optionNames, pick } from "./helpers";
import { Admin, MEMBER, MEMBER_EMAIL, TEAM_ADMIN, TEAM_ADMIN_EMAIL } from "./selfService";

/*
 * Usage filters on the shared ComboboxField: the free-text mode of Upstream model (typed text is the value,
 * suggestions only complete it) and the Combobox filters around it. serve.py seeds `use-*` / `use/*`.
 */

const BREAK_GLASS = "e2e-break-glass-password";
const usageRequests = (page: Page) => page.waitForRequest((r) => r.url().includes("/api/v1/usage") && r.url().includes("upstream_model="));

test.beforeEach(async ({ page, request }) => {
  const response = await request.post("/api/v1/session/break-glass", { data: { password: BREAK_GLASS } });
  expect(response.status()).toBe(204);
  await page.context().addCookies((await request.storageState()).cookies);
  await expect
    .poll(async () => ((await (await request.get("/api/v1/usage?group_by=model")).json()).items as unknown[]).length)
    .toBeGreaterThan(0);
  await page.goto("/usage");
  await expect(page.getByRole("heading", { name: "Usage" })).toBeVisible();
  // These tests read one row per served model, not the default called names.
  await pick(page, "Group by", "Served model");
  // Upstream model, Alias and Router are under More filters.
  await moreFilters(page);
});

const upstream = (page: Page) => page.getByRole("combobox", { name: "Upstream model", exact: true });

test("upstream model free text: one control for the label, a suggestion fills it, any typed text is the value", async ({ page }) => {
  await expect(upstream(page)).toHaveCount(1);
  await expect(page.getByLabel("Upstream model", { exact: true })).toHaveCount(1);
  await expect(upstream(page)).toHaveValue("");

  // Picking a suggestion writes its text and queries with it.
  await upstream(page).click();
  const [picked] = await Promise.all([usageRequests(page), page.getByRole("option", { name: "Qwen/UseA", exact: true }).click()]);
  expect(new URL(picked.url()).searchParams.get("upstream_model")).toBe("Qwen/UseA");
  await expect(upstream(page)).toHaveValue("Qwen/UseA");
  await expect(upstream(page)).toHaveAttribute("aria-expanded", "false");

  // Typing narrows the suggestions, and a retired name nobody suggests is still the value.
  await upstream(page).fill("Org/Ret");
  await expect(page.getByRole("option")).toHaveCount(0);
  const [typed] = await Promise.all([usageRequests(page), upstream(page).fill("Org/Retired")]);
  expect(new URL(typed.url()).searchParams.get("upstream_model")).toBe("Org/Retired");
  await expect(upstream(page)).toHaveValue("Org/Retired");

  // Clearing goes back to all (the unfiltered query may come from the cache, so look at the rows).
  const cell = (name: string) => page.getByRole("cell", { name, exact: true });
  await expect(cell("use/a")).toHaveCount(0);
  await upstream(page).fill("");
  await expect(cell("use/a")).toBeVisible();
  await expect(cell("use/retired")).toBeVisible();
});

test("upstream model free text: the input keeps what was typed, spaces included; the request is trimmed as before", async ({ page }) => {
  const [req] = await Promise.all([usageRequests(page), upstream(page).fill("  Org/Retired  ")]);
  await expect(upstream(page)).toHaveValue("  Org/Retired  ");
  expect(new URL(req.url()).searchParams.get("upstream_model")).toBe("Org/Retired");
});

test("upstream model free text: arrows move through suggestions, Enter picks the highlighted one or keeps the typed text, Esc closes", async ({ page }) => {
  const box = upstream(page);
  await box.click();
  const options = page.getByRole("option");
  // The suggestions come from the models list: the page also loads its usage, so wait for them.
  await expect(options.first()).toBeVisible();
  const count = await options.count();
  expect(count).toBeGreaterThan(1);
  await box.press("ArrowDown");
  await expect(options.first()).toHaveAttribute("aria-selected", "true");
  const first = (await options.first().textContent())!;
  await box.press("Enter");
  await expect(box).toHaveValue(first);
  await expect(box).toHaveAttribute("aria-expanded", "false");
  await expect(box).toBeFocused();

  // Enter with nothing highlighted never replaces the typed text with a suggestion.
  await box.fill("Qwen");
  await expect(options.first()).toBeVisible();
  await box.press("Enter");
  await expect(box).toHaveValue("Qwen");
  await expect(box).toHaveAttribute("aria-expanded", "false");

  await box.press("ArrowDown");
  await expect(box).toHaveAttribute("aria-expanded", "true");
  await box.press("Escape");
  await expect(box).toHaveAttribute("aria-expanded", "false");
  await expect(box).toHaveValue("Qwen");
});

for (const [where, width] of [["desktop", 1280], ["390 px", 390]] as const) {
  test(`upstream model free text (${where}): the highlighted suggestion of a long list stays in view and Enter takes it`, async ({ page }) => {
    await page.setViewportSize({ width, height: 700 });
    // Only the suggestions are long: answered here so the shared catalog stays as the other tests expect it.
    await page.route(/\/api\/v1\/models(\?|$)/, async (route) => {
      const response = await route.fetch();
      const body = (await response.json()) as { items: Record<string, unknown>[] };
      // Suggestions come from the deployments of every model.
      const base = body.items.find((item) => Array.isArray(item.deployments) && item.deployments.length > 0) ?? body.items[0];
      const deployment = ((base.deployments as Record<string, unknown>[] | undefined) ?? [{}])[0];
      const extra = Array.from({ length: 65 }, (_, i) => ({ ...base, id: `long-${i}`, deployments: [{ ...deployment, upstream_model: `Long/${String(i).padStart(2, "0")}` }] }));
      await route.fulfill({ response, json: { ...body, items: [...body.items, ...extra] } });
    });
    await page.reload();
    await moreFilters(page);
    const box = upstream(page);
    await box.fill("Long/");
    const options = page.getByRole("option");
    await expect(options).toHaveCount(65);
    const inView = async (name: string) => {
      const list = await page.getByRole("listbox", { name: "Suggestions" }).boundingBox();
      const row = await page.getByRole("option", { name, exact: true }).boundingBox();
      return row!.y >= list!.y - 1 && row!.y + row!.height <= list!.y + list!.height + 1;
    };
    // From nothing highlighted, ArrowUp goes to the last entry, far below the first screen.
    await box.press("ArrowUp");
    await expect(page.getByRole("option", { name: "Long/64", exact: true })).toHaveAttribute("aria-selected", "true");
    await expect.poll(() => inView("Long/64")).toBe(true);
    // It wraps round to the first, and walking down keeps the highlight in view.
    await box.press("ArrowDown");
    await expect(options.first()).toHaveAttribute("aria-selected", "true");
    await expect.poll(() => inView("Long/00")).toBe(true);
    for (let i = 0; i < 20; i++) await box.press("ArrowDown");
    await expect(page.getByRole("option", { name: "Long/20", exact: true })).toHaveAttribute("aria-selected", "true");
    await expect.poll(() => inView("Long/20")).toBe(true);
    await expect.poll(() => box.getAttribute("aria-activedescendant")).toBe(await page.getByRole("option", { name: "Long/20", exact: true }).getAttribute("id"));
    await box.press("Enter");
    await expect(box).toHaveValue("Long/20");
  });
}

test("usage filters: Team, Model, Alias and Router are searchable comboboxes that keep their 'all' row", async ({ page }) => {
  for (const [label, all] of [["Team", "All teams"], ["Model", "All models"], ["Alias", "Any"], ["Router", "Any"]] as const) {
    if (label === "Alias" || label === "Router") await moreFilters(page);
    await expect(page.getByRole("combobox", { name: label, exact: true })).toHaveText(all);
    expect((await optionNames(page, label))[0]).toBe(all);
  }
  const [req] = await Promise.all([page.waitForRequest((r) => r.url().includes("/api/v1/usage") && r.url().includes("model=use%2Fa")), pick(page, "Model", "use/a")]);
  expect(new URL(req.url()).searchParams.get("model")).toBe("use/a");
  await pick(page, "Model", "All models");
  await expect(page.getByRole("combobox", { name: "Model", exact: true })).toHaveText("All models");
});

/*
 * Roles. Only an installation admin reads routing detail (served model, upstream, alias, router): everyone else gets
 * the called name, whole-team totals and the three leaderboards, and the page never asks for what the API refuses.
 */
const ROUTING_GROUPS = /(^|,)(model|upstream_model|alias|router)(,|$)/;
const ROUTING_FILTERS = ["model", "upstream_model", "alias", "router", "model_id", "alias_id", "router_id", "alias_missing", "router_missing"];

/** Every usage, leaderboard and request response of the page, so a 403 or a routing parameter fails the test. */
function watchApi(page: Page) {
  const seen: { path: string; status: number; params: URLSearchParams }[] = [];
  page.on("response", (response: Response) => {
    const url = new URL(response.url());
    if (/^\/api\/v1\/(usage|requests)/.test(url.pathname)) seen.push({ path: url.pathname, status: response.status(), params: url.searchParams });
  });
  return seen;
}

async function roleTeam(request: import("@playwright/test").APIRequestContext) {
  const admin = await Admin.login(request);
  const team = await admin.ensureTeam("usage-roles");
  await admin.putMember(team.id, MEMBER_EMAIL, "member");
  await admin.putMember(team.id, TEAM_ADMIN_EMAIL, "admin");
  return team;
}

async function asRole(browser: import("@playwright/test").Browser, token: string, path: string) {
  const context = await browser.newContext();
  const page = await context.newPage();
  const seen = watchApi(page);
  await context.addCookies([{ name: "lumewarp_session", value: token, url: test.info().project.use.baseURL! }]);
  await page.goto(path);
  return { context, page, seen };
}

function expectNoRouting(seen: ReturnType<typeof watchApi>) {
  expect(seen.length).toBeGreaterThan(0);
  for (const call of seen) {
    expect(call.status, `${call.path}?${call.params}`).toBeLessThan(400);
    expect(call.params.get("group_by") ?? "", call.params.toString()).not.toMatch(ROUTING_GROUPS);
    for (const name of ROUTING_FILTERS) expect(call.params.has(name), `${name} in ${call.params}`).toBe(false);
  }
}

for (const [role, token] of [["member", MEMBER], ["team admin", TEAM_ADMIN]] as const) {
  test(`usage as a ${role}: called names, team totals and three leaderboards, no routing and no 403`, async ({ browser, request }) => {
    await roleTeam(request);
    const { context, page, seen } = await asRole(browser, token, "/usage");
    try {
      await expect(page.getByRole("heading", { name: "Usage", level: 1 })).toBeVisible();
      await expect(page.getByTestId("leaderboards")).toBeVisible();
      for (const board of ["called", "key", "owner"]) await expect(page.getByTestId(`leaderboard-${board}`)).toBeVisible();
      await expect(page.getByTestId("leaderboard-called").getByText("Called names", { exact: true })).toBeVisible();
      await expect(page.getByTestId("leaderboard-key").getByText("Keys", { exact: true })).toBeVisible();
      await expect(page.getByTestId("leaderboard-owner").getByText("Users", { exact: true })).toBeVisible();
      await expect(page.getByTestId("state-error")).toHaveCount(0);

      // No routing control: no Model filter, and Group by has the called name and no served model.
      await expect(page.getByRole("combobox", { name: "Model", exact: true })).toHaveCount(0);
      // Owner is offered once the administered teams have loaded; wait for that before reading the options.
      if (role === "team admin") await expect.poll(async () => (await optionNames(page, "Group by")).includes("Owner")).toBe(true);
      const groups = await optionNames(page, "Group by");
      expect(groups).toEqual(expect.arrayContaining(["Called name", "As typed", "Status", "Day", "Team", "API key"]));
      for (const routing of ["Served model", "Called × served", "Upstream model", "Alias", "Router"]) expect(groups).not.toContain(routing);
      expect(groups.includes("Owner")).toBe(role === "team admin");
      await moreFilters(page);
      for (const routing of ["Upstream model", "Alias", "Router"]) await expect(page.getByRole("combobox", { name: routing, exact: true })).toHaveCount(0);

      expect(seen.some((call) => call.path === "/api/v1/usage/leaderboard")).toBe(true);
      expectNoRouting(seen);
    } finally {
      await context.close();
    }
  });

  test(`requests as a ${role}: no routing columns, filters or detail steps`, async ({ browser, request }) => {
    await roleTeam(request);
    const { context, page, seen } = await asRole(browser, token, "/requests");
    try {
      await expect(page.getByRole("heading", { name: "Requests", level: 1 })).toBeVisible();
      await expect(page.getByRole("combobox", { name: "Resolved", exact: true })).toHaveCount(0);
      await moreFilters(page);
      for (const routing of ["Alias", "Router"]) await expect(page.getByRole("combobox", { name: routing, exact: true })).toHaveCount(0);
      expectNoRouting(seen);
    } finally {
      await context.close();
    }
  });
}

test("leaderboards: rank, bar, spend, the 'you' badge and muted anonymous rows", async ({ browser, request }) => {
  await roleTeam(request);
  const row = (rank: number, label: string, tokens: number, extra: object = {}) => ({
    rank, label, requested: null, team_id: null, key: null, owner_kind: null, owner_id: null, is_me: false,
    requests: 1, prompt_tokens: tokens, completion_tokens: 0, total_tokens: tokens, cost_nano_usd: tokens * 10, unknown_cost_count: 0, ...extra,
  });
  const context = await browser.newContext();
  const page = await context.newPage();
  try {
    await page.route("**/api/v1/usage/leaderboard?*", (route) => {
      const by = new URL(route.request().url()).searchParams.get("by");
      const items =
        by === "called"
          ? [row(1, "shared-chat", 900, { called: "shared-chat" }), row(2, "embed", 100, { called: "embed" })]
          : by === "key"
            ? [row(1, "ci-key", 500, { key: { id: "k1", name: "ci-key", display_prefix: "lw-abc", team_id: "t", team_name: "usage-roles", lifecycle: "active" } }), row(2, "Key #2", 50)]
            : [row(1, "E2E Member", 700, { owner_kind: "user", owner_id: "u1", is_me: true }), row(2, "Member #2", 20)];
      return route.fulfill({ status: 200, json: { items, next_cursor: null } });
    });
    await context.addCookies([{ name: "lumewarp_session", value: MEMBER, url: test.info().project.use.baseURL! }]);
    await page.goto("/usage");
    const board = (by: string) => page.getByTestId(`leaderboard-${by}`);
    await expect(board("called").getByTestId("leaderboard-row")).toHaveCount(2);
    await expect(board("called").getByTestId("leaderboard-row").first()).toContainText("shared-chat");
    await expect(board("key").getByTestId("key-pill").first()).toContainText("ci-key");
    await expect(board("key").getByText("Key #2")).toHaveClass(/text-muted-foreground/);
    await expect(board("owner").getByTestId("leaderboard-row").first()).toContainText("you");
    await expect(board("owner").getByText("Member #2")).toHaveClass(/text-muted-foreground/);
    // The Usage lens ranks by tokens and shows no money; the Cost lens ranks by spend.
    await expect(board("owner").getByTestId("leaderboard-row").first()).toContainText("700");
    await expect(board("owner").getByTestId("leaderboard-row").first()).not.toContainText("$");
    await page.getByTestId("usage-lens").getByRole("button", { name: "Cost", exact: true }).click();
    await expect(board("owner").getByTestId("leaderboard-row").first()).toContainText("$");
    await expect(board("called").getByTestId("leaderboard-row").first()).toContainText("shared-chat");
    // Three cards side by side on a wide screen, stacked on a phone.
    const tops = async () => Promise.all(["called", "key", "owner"].map(async (by) => (await board(by).boundingBox())!.y));
    const wide = await tops();
    expect(new Set(wide.map(Math.round)).size).toBe(1);
    await page.setViewportSize({ width: 390, height: 800 });
    const narrow = await tops();
    expect(narrow[0]).toBeLessThan(narrow[1]);
    expect(narrow[1]).toBeLessThan(narrow[2]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  } finally {
    await context.close();
  }
});

test("usage as an installation admin keeps the routing page and its leaderboards", async ({ page }) => {
  // The suite's beforeEach signed this page in with the break-glass session.
  await expect(page.getByRole("combobox", { name: "Model", exact: true })).toBeVisible();
  const groups = await optionNames(page, "Group by");
  expect(groups).toEqual(expect.arrayContaining(["Called name", "Served model", "Called × served", "Upstream model", "Alias", "Router", "As typed"]));
  await expect(page.getByRole("columnheader", { name: "Served model", exact: true })).toBeVisible();
  for (const board of ["called", "key", "owner"]) await expect(page.getByTestId(`leaderboard-${board}`)).toBeVisible();
  await expect(page.getByTestId("leaderboard-called").getByTestId("leaderboard-row").first()).toBeVisible();
});

test("a team admin's tables carry no routing column, and a request with null routing fields opens", async ({ browser, request }) => {
  await roleTeam(request);
  const context = await browser.newContext();
  const page = await context.newPage();
  const usage = { date: null, requested: "shared-chat", called: "shared-chat", called_kind: "model", called_names: ["shared-chat"], fallback_count: 0, retried_count: 0, model: null, team_id: null, owner_kind: null, owner_id: null, key_id: null, key: null, endpoint: null, model_type: null, requests: 3, prompt_tokens: 10, completion_tokens: 20, total_tokens: 30, audio_ms: 0, cost_nano_usd: 300, unknown_cost_count: 0, error_count: 0, total_duration_ms: 0 };
  const logged = {
    request_id: "req-null-1", ts: new Date().toISOString(), kind: "llm", endpoint: "/v1/chat/completions", requested: "shared-chat",
    model: null, model_id: null, alias: null, alias_id: null, router: null, router_id: null, fallback: null, attempts: null, upstream: null, upstream_model: null,
    status: 200, outcome: "completed", stream: false, duration_ms: 12, prompt_tokens: 1, completion_tokens: 2, cost_nano_usd: 10,
    team_id: "00000000-0000-0000-0000-000000000001", key_id: null, key: null, owner_kind: "user", owner_id: "00000000-0000-0000-0000-000000000002", playground_user_id: null,
  };
  try {
    await page.route("**/api/v1/usage?*", (route) => route.fulfill({ status: 200, json: { items: [usage], next_cursor: null } }));
    await page.route("**/api/v1/requests?*", (route) => route.fulfill({ status: 200, json: { items: [logged], next_cursor: null } }));
    await context.addCookies([{ name: "lumewarp_session", value: TEAM_ADMIN, url: test.info().project.use.baseURL! }]);
    await page.goto("/usage");
    await expect(page.getByRole("columnheader", { name: "Called name", exact: true })).toBeVisible();
    await expect(page.getByRole("columnheader", { name: /^(Served model|Model|Upstream model|Via alias|Via router|Served by)$/ })).toHaveCount(0);
    await expect(page.getByRole("link", { name: "View" }).first()).not.toHaveAttribute("href", /(model|alias|router)=/);

    await page.goto("/requests");
    await expect(page.getByRole("columnheader")).toHaveText(["Date", "Status", "Requested", "Key", "Tokens", "Cost"]);
    await page.getByTestId("request-requested").getByText("shared-chat", { exact: true }).click();
    const timeline = page.getByTestId("request-detail").getByTestId("request-timeline");
    await expect(timeline.locator("li")).toHaveText([/Received\s*shared-chat/, /Response\s*HTTP\s*200/]);
    await expect(page.getByTestId("state-error")).toHaveCount(0);
  } finally {
    await context.close();
  }
});
