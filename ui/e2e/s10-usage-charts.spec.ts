import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

import { optionNames, pick } from "./helpers";
import { signIn, VIEWER } from "./selfService";

/*
 * The usage chart follows the table's grouping: its series are the first non-day dimension of Group by
 * (`group_by=day,<dimension>`, a single series for Day), drawn as stacked bars over days, a share donut or
 * ranked bars, in tokens, cost, requests or errors. serve.py seeds a trend across UTC days, a model with no
 * price and nine models on one day; the cases that need exact rows mock the chart's own query.
 */

const BREAK_GLASS = "e2e-break-glass-password";
const isoDay = (date: Date) => date.toISOString().slice(0, 10);
const daysAgo = (n: number) => isoDay(new Date(Date.now() - n * 86_400_000));

type ApiRow = { date: string | null; called: string | null; total_tokens: number; cost_nano_usd: number };

const chart = (page: Page) => page.getByTestId("usage-chart");
const tooltip = (page: Page) => page.getByTestId("usage-chart-tooltip");
const legend = (page: Page) => chart(page).getByTestId("usage-chart-legend");
const metric = (page: Page, name: "Tokens" | "Requests" | "Errors") => page.getByTestId("usage-chart-metric").getByRole("button", { name, exact: true });
const lens = (page: Page, name: "Usage" | "Cost") => page.getByTestId("usage-lens").getByRole("button", { name, exact: true });
const view = (page: Page, name: "Over time" | "Share" | "Ranked") => page.getByTestId("usage-chart-view").getByRole("button", { name, exact: true });

/** The same numbers the chart draws, from the API: `day,called` rows for an inclusive UTC day range. */
async function apiRows(request: APIRequestContext, from: string, to: string): Promise<ApiRow[]> {
  const end = isoDay(new Date(Date.parse(`${to}T00:00:00Z`) + 86_400_000));
  const query = new URLSearchParams({ group_by: "day,called", kind: "llm", from: `${from}T00:00:00Z`, to: `${end}T00:00:00Z`, limit: "500" });
  const response = await request.get(`/api/v1/usage?${query}`);
  expect(response.status(), await response.text()).toBe(200);
  return ((await response.json()) as { items: ApiRow[] }).items;
}

async function signInAsAdmin(page: Page, request: APIRequestContext) {
  const response = await request.post("/api/v1/session/break-glass", { data: { password: BREAK_GLASS } });
  expect(response.status()).toBe(204);
  await page.context().addCookies((await request.storageState()).cookies);
}

async function setRange(page: Page, from: string, to: string) {
  await page.getByLabel("From").fill(from);
  await page.getByLabel("To", { exact: true }).fill(to);
  await expect(page.getByLabel("From")).toHaveValue(from);
}

/** Park the pointer over the plot's last day (the right end): a stacked chart's tooltip follows the x position. */
async function hoverLastDay(page: Page) {
  const surface = chart(page).locator("svg.recharts-surface").first();
  await surface.scrollIntoViewIfNeeded();
  const box = (await surface.boundingBox())!;
  await page.mouse.move(2, 2);
  await page.mouse.move(box.x + box.width - 24, box.y + box.height / 2);
}

const row = (day: string, fields: Record<string, unknown>, tokens: number, cost = 0, unpriced = 0, requests = 1) => ({
  date: day,
  requests,
  prompt_tokens: tokens,
  completion_tokens: 0,
  total_tokens: tokens,
  cost_nano_usd: cost,
  unknown_cost_count: unpriced,
  error_count: 0,
  fallback_count: 0,
  retried_count: 0,
  ...fields,
});

/** Answer only the chart's own queries (`group_by` starting with `day`) with crafted rows; record their group_by. */
async function mockChart(page: Page, items: Record<string, unknown>[], seen: string[] = []) {
  await page.route("**/api/v1/usage?*", async (route) => {
    const groupBy = new URL(route.request().url()).searchParams.get("group_by") ?? "";
    if (!groupBy.startsWith("day")) return route.continue();
    seen.push(groupBy);
    await route.fulfill({ status: 200, json: { items, next_cursor: null } });
  });
}

test("usage: the chart draws the API's rows by called name, and its table and the page's table agree", async ({ page, request }) => {
  await signInAsAdmin(page, request);
  await page.goto("/usage");
  const today = isoDay(new Date());
  await setRange(page, today, today);
  const rows = await apiRows(request, today, today);
  expect(rows.length).toBeGreaterThan(1);
  const tokens = new Map(rows.map((r) => [r.called ?? "—", r.total_tokens]));
  const total = rows.reduce((sum, r) => sum + r.total_tokens, 0);

  await expect(page.getByLabel("Group by")).toHaveText("Called name");
  for (const name of tokens.keys()) await expect(legend(page).locator(`[data-series="${name}"]`)).toHaveCount(1);
  await hoverLastDay(page);
  await expect(tooltip(page)).toBeVisible();
  for (const name of tokens.keys()) await expect(tooltip(page)).toContainText(name);

  // The chart's own table: one row per day, one column per name, the same numbers.
  await page.getByTestId("usage-chart-table-toggle").click();
  const table = page.getByTestId("usage-chart-table");
  await expect(table).toBeVisible();
  const headers = await table.getByRole("columnheader").allTextContents();
  const cells = await table.getByRole("row").filter({ hasText: today }).getByRole("cell").allTextContents();
  for (const [name, value] of tokens) {
    const column = headers.indexOf(name);
    expect(column, `${name} is a column`).toBeGreaterThan(0);
    expect(cells[column]).toBe(value.toLocaleString());
  }
  expect(cells[headers.indexOf("Total")]).toBe(total.toLocaleString());

  // The page's table, grouped by called name, reports the same totals.
  for (const [name, value] of tokens) {
    const tableRow = page.getByRole("row").filter({ has: page.getByRole("cell", { name }) }).last();
    await expect(tableRow).toBeVisible();
    expect(await tableRow.getByRole("cell").allTextContents(), `${name} total tokens in the table`).toContain(value.toLocaleString());
  }
});

test("group by: the chart's query and legend follow the table's grouping", async ({ page, request }) => {
  await signInAsAdmin(page, request);
  const seen: string[] = [];
  const today = isoDay(new Date());
  await mockChart(
    page,
    [row(today, { called: "alias-one", model: "served-a", requested: "typed-x", team_id: null }, 100), row(today, { called: "router-two", model: "served-b", requested: "typed-y" }, 50)],
    seen,
  );
  await page.goto("/usage");
  await expect(legend(page).locator('[data-series="alias-one"]')).toHaveCount(1);
  expect(seen).toContain("day,called");

  const pickGroup = (name: string) => pick(page, "Group by", name);
  await pickGroup("Served model");
  await expect(legend(page).locator('[data-series="served-a"]')).toHaveCount(1);
  await expect(legend(page).locator('[data-series="alias-one"]')).toHaveCount(0);
  expect(seen).toContain("day,model");

  // Called × served: the chart's series is the first non-day dimension, the called name.
  await pickGroup("Called × served");
  await expect(legend(page).locator('[data-series="router-two"]')).toHaveCount(1);
  await expect(page.getByLabel("Group by")).toHaveText("Called × served");

  await pickGroup("As typed");
  await expect(legend(page).locator('[data-series="typed-x"]')).toHaveCount(1);
  expect(seen.at(-1)).toBe("day,requested");

  // Day: a single series, no share or ranked views.
  await pickGroup("Day");
  expect(seen.at(-1)).toBe("day");
  await expect(legend(page).locator("[data-series]")).toHaveCount(1);
  await expect(page.getByTestId("usage-chart-view")).toHaveCount(0);
});

test("chart type: stacked bars, a share donut and ranked bars over the same series", async ({ page, request }) => {
  await signInAsAdmin(page, request);
  const today = isoDay(new Date());
  await mockChart(page, [row(today, { called: "big" }, 900), row(today, { called: "small" }, 100)]);
  await page.goto("/usage");
  await expect(chart(page).getByTestId("usage-chart-plot")).toHaveAttribute("data-view", "stacked");
  await view(page, "Share").click();
  await expect(chart(page).getByTestId("usage-chart-plot")).toHaveAttribute("data-view", "share");
  await expect(chart(page).locator(".recharts-pie-sector").first()).toBeVisible();
  await expect(legend(page).locator("[data-series]")).toHaveCount(2);
  await view(page, "Ranked").click();
  await expect(chart(page).locator(".recharts-bar-rectangle").first()).toBeVisible();
  await expect(chart(page).locator(".recharts-bar-rectangle")).toHaveCount(2);
  await view(page, "Over time").click();
  await expect(chart(page).getByTestId("usage-chart-plot")).toHaveAttribute("data-view", "stacked");
});

test("metric: tokens, requests and errors on the Usage lens; the choice survives a reload and storage that throws", async ({ page, request, browser }) => {
  await signInAsAdmin(page, request);
  await page.goto("/usage");
  await expect(metric(page, "Tokens")).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByTestId("usage-chart-metric").getByRole("button")).toHaveText(["Tokens", "Requests", "Errors"]);
  await hoverLastDay(page);
  await expect(tooltip(page)).toBeVisible();
  for (const name of ["Requests", "Errors"] as const) {
    await metric(page, name).click();
    await expect(metric(page, name)).toHaveAttribute("aria-pressed", "true");
    await expect(metric(page, "Tokens")).toHaveAttribute("aria-pressed", "false");
  }
  await page.reload();
  await expect(metric(page, "Errors")).toHaveAttribute("aria-pressed", "true");

  // storage blocked: the preference cannot be read or written, and the toggles still work in memory.
  const context = await browser.newContext();
  await context.addCookies((await request.storageState()).cookies);
  await context.addInitScript(() => {
    const block = () => {
      throw new DOMException("denied", "SecurityError");
    };
    Storage.prototype.getItem = block;
    Storage.prototype.setItem = block;
  });
  const fresh = await context.newPage();
  const errors: string[] = [];
  fresh.on("pageerror", (error) => errors.push(error.message));
  await fresh.goto("/usage");
  await expect(metric(fresh, "Tokens")).toHaveAttribute("aria-pressed", "true");
  await expect(lens(fresh, "Usage")).toHaveAttribute("aria-pressed", "true");
  await lens(fresh, "Cost").click();
  await expect(lens(fresh, "Cost")).toHaveAttribute("aria-pressed", "true");
  await expect(fresh.getByTestId("usage-chart")).toHaveAttribute("data-metric", "cost");
  expect(errors).toEqual([]);
  await context.close();
});

test("lens: Cost reshapes tiles, chart, leaderboards and table, and survives a reload", async ({ page, request }) => {
  await signInAsAdmin(page, request);
  await page.goto("/usage");
  const tiles = page.getByTestId("usage-insights");
  await expect(lens(page, "Usage")).toHaveAttribute("aria-pressed", "true");
  for (const id of ["requests", "tokens", "errors", "p95", "fallback"]) await expect(tiles.getByTestId(`insight-${id}`)).toBeVisible();
  await expect(tiles.getByTestId("insight-spend")).toHaveCount(0);
  await expect(page.getByRole("columnheader", { name: "Prompt", exact: true })).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "Cost", exact: true })).toHaveCount(0);
  await expect(page.getByTestId("leaderboard-called").getByTestId("leaderboard-row").first()).not.toContainText("$");

  await lens(page, "Cost").click();
  await expect(lens(page, "Cost")).toHaveAttribute("aria-pressed", "true");
  for (const id of ["spend", "avg-cost", "cost-per-mtok", "unpriced"]) await expect(tiles.getByTestId(`insight-${id}`)).toBeVisible();
  for (const id of ["requests", "tokens", "errors", "p95", "fallback", "status"]) await expect(tiles.getByTestId(`insight-${id}`)).toHaveCount(0);
  await expect(page.getByTestId("usage-chart")).toHaveAttribute("data-metric", "cost");
  await expect(page.getByTestId("usage-chart-metric")).toHaveCount(0);
  await expect(page.getByTestId("usage-chart-view")).toBeVisible();
  const headers = page.getByRole("columnheader");
  for (const name of ["Requests", "Cost", "Share of cost", "Cost / request", "Unpriced", "Log"]) await expect(headers.filter({ hasText: new RegExp(`^${name.replace("/", "\\/")}$`) })).toHaveCount(1);
  for (const name of ["Prompt", "Completion", "Total tokens", "p95"]) await expect(headers.filter({ hasText: new RegExp(`^${name}$`) })).toHaveCount(0);
  await expect(page.getByTestId("leaderboard-called").getByTestId("leaderboard-row").first()).toContainText("$");

  expect(await page.evaluate(() => localStorage.getItem("lumewarp.usage-lens"))).toBe("cost");
  await page.reload();
  await expect(lens(page, "Cost")).toHaveAttribute("aria-pressed", "true");
  await expect(tiles.getByTestId("insight-spend")).toBeVisible();
  await lens(page, "Usage").click();
  await expect(metric(page, "Tokens")).toHaveAttribute("aria-pressed", "true");
});

test("lens: the old chart-metric preference 'cost' starts the Cost lens", async ({ page, request }) => {
  await signInAsAdmin(page, request);
  await page.addInitScript(() => {
    if (!localStorage.getItem("lumewarp.usage-lens")) localStorage.setItem("lumewarp.usage-metric", "cost");
  });
  await page.goto("/usage");
  await expect(lens(page, "Cost")).toHaveAttribute("aria-pressed", "true");
});

test("cost lens: the unpriced note lives here and a model with no price is never drawn as zero", async ({ page, request }) => {
  await signInAsAdmin(page, request);
  const today = isoDay(new Date());
  await mockChart(page, [
    row(today, { called: "free-only" }, 10, 0),
    row(today, { called: "free-and-unknown" }, 20, 0, 1, 2),
    row(today, { called: "unpriced-only" }, 10, 0, 1),
  ]);
  await page.goto("/usage");
  await expect(legend(page).locator('[data-series="unpriced-only"]')).toHaveCount(1);
  await expect(page.getByText(/had no price configured/)).toHaveCount(0);
  await lens(page, "Cost").click();
  const note = chart(page).getByTestId("usage-chart-unpriced");
  await expect(note).toContainText("1 model has no price and is not included in cost (unpriced-only)");
  await expect(note).toContainText("1 request has no price, so the bars are a lower bound");
  await expect(legend(page).locator('[data-series="unpriced-only"]')).toHaveCount(0);
  await expect(legend(page).locator('[data-series="free-only"]')).toHaveCount(1);
  await lens(page, "Usage").click();
  await expect(legend(page).locator('[data-series="unpriced-only"]')).toHaveCount(1);
  await expect(note).toHaveCount(0);
});

test("series: eight hues, and the tail folds into Other with its names listed", async ({ page, request }) => {
  await signInAsAdmin(page, request);
  const today = isoDay(new Date());
  await mockChart(page, Array.from({ length: 10 }, (_, i) => row(today, { called: `fold/m${i + 1}` }, 1000 - i * 10)));
  await page.goto("/usage");
  await expect(legend(page).locator("[data-series]")).toHaveCount(9);
  await expect(legend(page).locator('[data-series="Other"]')).toHaveAttribute("title", "fold/m9, fold/m10");
  await page.getByTestId("usage-chart-table-toggle").click();
  for (const name of ["fold/m1", "fold/m8", "Other"]) {
    await expect(page.getByTestId("usage-chart-table").getByRole("columnheader", { name, exact: true })).toBeVisible();
  }
  await expect(page.getByTestId("usage-chart-table").getByRole("columnheader", { name: "fold/m9", exact: true })).toHaveCount(0);
  await hoverLastDay(page);
  await expect(tooltip(page).getByTestId("usage-chart-tooltip-more")).toContainText("fold/m9, fold/m10");
});

test("empty states, cleared dates and an impossible range", async ({ page, request }) => {
  await signInAsAdmin(page, request);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/usage");
  await setRange(page, "2020-01-01", "2020-01-01");
  await expect(chart(page).getByTestId("usage-chart-empty")).toHaveText("No usage in this range.");

  await page.getByLabel("From").fill("");
  await page.getByLabel("To", { exact: true }).fill("");
  await expect(chart(page).getByTestId("usage-chart-empty")).toHaveText("Choose a date range.");
  await setRange(page, isoDay(new Date()), daysAgo(5));
  await expect(chart(page).getByTestId("usage-chart-error")).toContainText("must be after from");
  await setRange(page, "0002-01-01", isoDay(new Date()));
  await expect(chart(page).getByTestId("usage-chart-error")).toContainText("400 days");
  expect(errors).toEqual([]);
});

test("insights: tiles with the change against the previous range, and the status split", async ({ page, request }) => {
  await signInAsAdmin(page, request);
  await page.route("**/api/v1/usage?*", async (route) => {
    const params = new URL(route.request().url()).searchParams;
    const groupBy = params.get("group_by") ?? "";
    if (groupBy === ",") {
      expect(params.get("compare")).toBe("previous");
      const totals = { requests: 200, total_tokens: 2000, cost_nano_usd: 2_000_000_000, error_count: 20, fallback_count: 10, p95_duration_ms: 900 };
      return route.fulfill({
        status: 200,
        json: { items: [{ ...totals, prompt_tokens: 0, completion_tokens: 0, unknown_cost_count: 0, audio_ms: 0, total_duration_ms: 0, retried_count: 0 }], next_cursor: null, previous: { ...totals, requests: 100, error_count: 1, p95_duration_ms: 600, prompt_tokens: 0, completion_tokens: 0, audio_ms: 0, unknown_cost_count: 0, total_duration_ms: 0, retried_count: 0, p50_duration_ms: null, p99_duration_ms: null } },
      });
    }
    if (groupBy === "status_class") {
      const base = { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, cost_nano_usd: 0, unknown_cost_count: 0, audio_ms: 0, error_count: 0, fallback_count: 0, retried_count: 0, total_duration_ms: 0 };
      return route.fulfill({ status: 200, json: { items: [{ ...base, status_class: "2xx", requests: 180 }, { ...base, status_class: "5xx", requests: 20 }], next_cursor: null } });
    }
    return route.continue();
  });
  await page.goto("/usage");
  const insights = page.getByTestId("usage-insights");
  await expect(insights.getByTestId("insight-requests")).toContainText("200");
  await expect(insights.getByTestId("insight-requests").getByTestId("insight-delta")).toContainText("100%");
  await expect(insights.getByTestId("insight-requests").getByTestId("insight-delta")).toHaveAttribute("data-direction", "up");
  await expect(insights.getByTestId("insight-errors")).toContainText("10%");
  await expect(insights.getByTestId("insight-p95")).toContainText("900 ms");
  await expect(insights.getByTestId("insight-fallback")).toContainText("5%");

  // Cost lens: the previous range has the same spend, so the tile says so.
  await lens(page, "Cost").click();
  await expect(insights.getByTestId("insight-spend")).toContainText("$2.00");
  await expect(insights.getByTestId("insight-spend")).toContainText("no change");
  await expect(insights.getByTestId("insight-avg-cost")).toContainText("$0.01");
  await expect(insights.getByTestId("insight-cost-per-mtok")).toContainText("$1,000");
  await expect(insights.getByTestId("insight-unpriced")).toContainText("0");
});

test("a plain member's Usage page still works, has no fallback tile, and is empty rather than broken", async ({ browser }) => {
  const context = await browser.newContext();
  const member = await context.newPage();
  try {
    await signIn(member, VIEWER, "/usage");
    await expect(member.getByRole("heading", { name: "Usage", level: 1 })).toBeVisible();
    await expect(chart(member).getByTestId("usage-chart-empty")).toBeVisible();
    await expect(member.getByTestId("state-error")).toHaveCount(0);
    await expect(member.getByTestId("insight-requests")).toBeVisible();
    await expect(member.getByTestId("insight-fallback")).toHaveCount(0);
    await expect(member.getByLabel("Group by")).toHaveText("Called name");
    await lens(member, "Cost").click();
    await expect(lens(member, "Cost")).toHaveAttribute("aria-pressed", "true");
    await expect(member.getByTestId("insight-spend")).toBeVisible();
    await expect(member.getByTestId("insight-fallback")).toHaveCount(0);
  } finally {
    await context.close();
  }
});

/** The dimension headers each Group by option adds, in table order (an "All zones" view puts Zone first). */
const GROUP_HEADERS: Record<string, string[]> = {
  "Called name": ["Called name"],
  "Served model": ["Served model", "Upstream model", "Via alias", "Via router"],
  "Called × served": ["Called name", "Served model", "Upstream model"],
  "As typed": ["As typed"],
  "Upstream model": ["Upstream model", "Served by"],
  Alias: ["Alias", "Served by"],
  Router: ["Router", "Served by"],
  Endpoint: ["Endpoint"],
  "Model type": ["Type"],
  Status: ["Status"],
  Team: ["Team"],
  Owner: ["Owner"],
  "API key": ["API key"],
  Day: ["Date"],
};

for (const which of ["Usage", "Cost"] as const) {
  test(`group by (${which} lens): every option keeps the table's header and cells aligned`, async ({ page, request }) => {
    await signInAsAdmin(page, request);
    const warnings: string[] = [];
    page.on("console", (message) => {
      if (/same key|Encountered two children|duplicate column key/i.test(message.text())) warnings.push(message.text());
    });
    await page.goto("/usage");
    await lens(page, which).click();
    const table = page.locator("table").last();
    const common = which === "Usage" ? ["Requests", "Total tokens"] : ["Requests", "Cost", "Share of cost", "Cost / request", "Unpriced"];
    const options = await optionNames(page, "Group by");
    expect(options).toEqual(Object.keys(GROUP_HEADERS).filter((name) => options.includes(name)));
    for (const option of options) {
      await pick(page, "Group by", option);
      await expect(page.getByRole("combobox", { name: "Group by", exact: true })).toHaveText(option);
      await expect(table.locator("tbody tr").first()).toBeVisible();
      const headers = (await table.locator("thead th").allTextContents()).map((text) => text.trim());
      const dims = GROUP_HEADERS[option];
      const at = headers[0] === "Zone" ? 1 : 0;
      expect(headers.slice(at, at + dims.length), option).toEqual(dims);
      expect(headers.slice(at + dims.length, at + dims.length + common.length), option).toEqual(common);
      expect(headers.at(-1), option).toBe("Log");
      const rows = table.locator("tbody tr");
      for (let i = 0; i < (await rows.count()); i++) expect(await rows.nth(i).locator("td").count(), `${option} row ${i}`).toBe(headers.length);
      // The Log cell is the last one and holds the link, whatever the dimensions before it.
      await expect(rows.first().locator("td").last().getByRole("link", { name: "View" })).toBeVisible();
    }
    expect(warnings).toEqual([]);
  });
}

test("group by: a sort on a column that goes away is dropped and paging restarts", async ({ page, request }) => {
  await signInAsAdmin(page, request);
  // The seed has only a few days of usage: the Day table is padded to 25 rows so it pages.
  await page.route("**/api/v1/usage?*", async (route) => {
    const url = new URL(route.request().url());
    const response = await route.fetch();
    if (url.searchParams.get("group_by") !== "day") return route.fulfill({ response });
    const body = await response.json();
    const sample = body.items[0];
    const items = Array.from({ length: 25 }, (_, i) => ({ ...sample, date: daysAgo(i) }));
    await route.fulfill({ response, json: { ...body, items } });
  });
  await page.goto("/usage");
  await pick(page, "Group by", "Day");
  const table = page.locator("table").last();
  const pager = page.getByTestId("table-pagination").last();
  // The padded rows are in before the sort: the pager reads the 25 of them.
  await expect(pager).toContainText("1–10 of 25");
  await table.getByRole("button", { name: "Date", exact: true }).click();
  await expect(table.locator("thead th").first()).toHaveAttribute("aria-sort", "ascending");
  await pager.getByRole("button", { name: "Next page" }).click();
  await expect(pager).toContainText(/^11–/);
  // Away and back: without the reset the Date sort and page 2 would come back with the column.
  await pick(page, "Group by", "Called name");
  await expect(table.locator("thead th[aria-sort]")).toHaveCount(0);
  await pick(page, "Group by", "Day");
  await expect(table.locator("thead th").first()).toHaveText("Date");
  await expect(table.locator("thead th[aria-sort]")).toHaveCount(0);
  await expect(pager).toContainText(/^1–10 of/);
});
