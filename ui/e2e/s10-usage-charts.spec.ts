import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

import { signIn, VIEWER } from "./selfService";

/*
 * The usage chart: real rows from `GET /usage?group_by=day,model`, on Overview and on the Usage page, in tokens
 * or cost. serve.py seeds a trend across UTC days (with an empty day), a model with no price, a partly priced
 * model, and nine models on one day so the tail has to fold into "Other".
 */

const BREAK_GLASS = "e2e-break-glass-password";
const isoDay = (date: Date) => date.toISOString().slice(0, 10);
const daysAgo = (n: number) => isoDay(new Date(Date.now() - n * 86_400_000));

type ApiRow = { date: string | null; model: string | null; total_tokens: number; cost_nano_usd: number; unknown_cost_count: number };

const chart = (page: Page) => page.getByTestId("usage-chart");
const daySlots = (page: Page) => page.getByTestId("usage-day");
const tooltip = (page: Page) => page.getByTestId("usage-chart-tooltip");
const metric = (page: Page, name: "Tokens" | "Cost") => page.getByTestId("usage-chart-metric").getByRole("button", { name, exact: true });
const slot = (page: Page, day: string) => page.locator(`[data-day="${day}"]`);

/** The same numbers the chart draws, from the API: `day,model` rows for an inclusive UTC day range. */
async function apiRows(request: APIRequestContext, from: string, to: string): Promise<ApiRow[]> {
  const end = isoDay(new Date(Date.parse(`${to}T00:00:00Z`) + 86_400_000));
  const query = new URLSearchParams({ group_by: "day,model", from: `${from}T00:00:00Z`, to: `${end}T00:00:00Z`, limit: "500" });
  const response = await request.get(`/api/v1/usage?${query}`);
  expect(response.status(), await response.text()).toBe(200);
  return ((await response.json()) as { items: ApiRow[] }).items;
}

const sumBy = (rows: ApiRow[], key: keyof ApiRow) => rows.reduce((sum, row) => sum + Number(row[key] ?? 0), 0);

async function signInAsAdmin(page: Page, request: APIRequestContext) {
  const response = await request.post("/api/v1/session/break-glass", { data: { password: BREAK_GLASS } });
  expect(response.status()).toBe(204);
  await page.context().addCookies((await request.storageState()).cookies);
}

async function setRange(page: Page, from: string, to: string) {
  await page.getByLabel("From").fill(from);
  await page.getByLabel("To (inclusive)").fill(to);
  await expect(page.getByLabel("From")).toHaveValue(from);
}

/** Hover a slot with the pointer parked elsewhere first: a re-render under a still pointer fires no enter. */
async function hoverSlot(page: Page, day: string) {
  await page.mouse.move(2, 2);
  await slot(page, day).hover();
}

test("usage: the chart draws the API's rows, and its table and the page's table agree with it", async ({ page, request }) => {
  await signInAsAdmin(page, request);
  await page.goto("/usage");
  // One day: the chart's table row and the page's model rows then hold the same numbers.
  const today = isoDay(new Date());
  await setRange(page, today, today);
  const rows = await apiRows(request, today, today);
  expect(rows.length).toBeGreaterThan(1);
  const tokens = new Map(rows.map((row) => [row.model ?? "—", row.total_tokens]));
  const total = sumBy(rows, "total_tokens");

  await hoverSlot(page, today);
  await expect(tooltip(page)).toBeVisible();
  for (const model of tokens.keys()) await expect(tooltip(page)).toContainText(model);
  await expect(tooltip(page)).toContainText(`${total.toLocaleString()} tokens`);

  // The chart's own table: one row per day, one column per model, the same numbers.
  await page.getByTestId("usage-chart-table-toggle").click();
  const table = page.getByTestId("usage-chart-table");
  await expect(table).toBeVisible();
  const headers = await table.getByRole("columnheader").allTextContents();
  const cells = await table.getByRole("row").filter({ hasText: today }).getByRole("cell").allTextContents();
  for (const [model, value] of tokens) {
    const column = headers.indexOf(model);
    expect(column, `${model} is a column`).toBeGreaterThan(0);
    expect(cells[column]).toBe(value.toLocaleString());
  }
  expect(cells[headers.indexOf("Total")]).toBe(total.toLocaleString());

  // The page's table, grouped by model, reports the same totals for the same day.
  for (const [model, value] of tokens) {
    const row = page.getByRole("row").filter({ has: page.getByRole("cell", { name: model, exact: true }) });
    await expect(row).toBeVisible();
    const modelCells = await row.getByRole("cell").allTextContents();
    expect(modelCells[5], `${model} total tokens in the table`).toBe(value.toLocaleString());
  }
});

test("overview: real usage for the last 14 UTC days, and no sample data anywhere", async ({ page, request }) => {
  await signInAsAdmin(page, request);
  await page.goto("/");
  await expect(chart(page)).toBeVisible();
  await expect(daySlots(page)).toHaveCount(14);
  const days = await chart(page).getByTestId("usage-day").evaluateAll((nodes) => nodes.map((node) => node.getAttribute("data-day")));
  expect(days).toEqual(Array.from({ length: 14 }, (_, i) => daysAgo(13 - i)));
  await expect(page.getByText(/sample/i)).toHaveCount(0);

  // What the card shows is the API's answer for the same window.
  const today = isoDay(new Date());
  const rows = await apiRows(request, daysAgo(13), today);
  expect(rows.length).toBeGreaterThan(0);
  await hoverSlot(page, today);
  await expect(tooltip(page)).toBeVisible();
  await expect(tooltip(page)).toContainText(`${sumBy(rows.filter((row) => row.date === today), "total_tokens").toLocaleString()} tokens`);

  // And it links to the page that explains it.
  await chart(page).getByRole("link", { name: "Open Usage" }).click();
  await expect(page).toHaveURL(/\/usage$/);
  await expect(page.getByRole("heading", { name: "Usage", level: 1 })).toBeVisible();
});

test("overview: a failed usage query stays in its card and retries without taking the page down", async ({ page, request }) => {
  await signInAsAdmin(page, request);
  await page.route("**/api/v1/usage?*", (route) => route.fulfill({ status: 500, json: { error: { code: "internal", message: "usage is unavailable" } } }));
  await page.goto("/");
  await expect(chart(page).getByText("usage is unavailable")).toBeVisible();
  // The rest of the page is unaffected: the counters are still there, and the page's own error slot stays empty.
  await expect(page.getByText("Active keys", { exact: true })).toBeVisible();
  await expect(page.getByText("Gateways connected", { exact: true })).toBeVisible();
  await expect(page.getByTestId("state-error")).toHaveCount(0);

  await page.unroute("**/api/v1/usage?*");
  await chart(page).getByRole("button", { name: "Try again" }).click();
  await expect(chart(page).getByTestId("usage-day")).toHaveCount(14);
  await expect(chart(page).getByText("usage is unavailable")).toHaveCount(0);
});

test("empty states: a quiet window says so, on both pages", async ({ page, request }) => {
  await signInAsAdmin(page, request);
  await page.route("**/api/v1/usage?*", (route) => route.fulfill({ status: 200, json: { items: [], next_cursor: null } }));
  await page.goto("/");
  await expect(chart(page).getByTestId("usage-chart-empty")).toHaveText("No usage in the last 14 days.");
  await page.unroute("**/api/v1/usage?*");

  await page.goto("/usage");
  await setRange(page, "2020-01-01", "2020-01-01");
  await expect(chart(page).getByTestId("usage-chart-empty")).toHaveText("No usage in this range.");
  await expect(chart(page).getByText("No usage in the last 14 days.")).toHaveCount(0);
});

test("toggle: switches tokens to cost, survives a reload, and works when storage throws", async ({ page, request, browser }) => {
  await signInAsAdmin(page, request);
  await page.goto("/usage");
  const today = isoDay(new Date());
  await expect(metric(page, "Tokens")).toHaveAttribute("aria-pressed", "true");
  await hoverSlot(page, today);
  await expect(tooltip(page)).toContainText("tokens");

  await metric(page, "Cost").click();
  await expect(metric(page, "Cost")).toHaveAttribute("aria-pressed", "true");
  await expect(metric(page, "Tokens")).toHaveAttribute("aria-pressed", "false");
  await hoverSlot(page, today);
  await expect(tooltip(page)).toContainText("$");
  await expect(tooltip(page)).not.toContainText("tokens");
  expect(await page.evaluate(() => localStorage.getItem("lumewarp.usage-metric"))).toBe("cost");

  await page.reload();
  await expect(metric(page, "Cost")).toHaveAttribute("aria-pressed", "true");

  // storage blocked: the preference cannot be read or written, and the toggle still works in memory.
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
  await metric(fresh, "Cost").click();
  await expect(metric(fresh, "Cost")).toHaveAttribute("aria-pressed", "true");
  await expect(chart(fresh).getByTestId("usage-day")).toHaveCount(30);
  expect(errors).toEqual([]);
  await context.close();
});

test("cost: a model with no price is reported and never drawn as zero", async ({ page, request }) => {
  await signInAsAdmin(page, request);
  await page.goto("/usage");
  const legend = chart(page).getByTestId("usage-chart-legend");

  await expect(legend.locator('[data-series="trend/unpriced"]')).toHaveCount(1);
  await metric(page, "Cost").click();
  await expect(chart(page).getByTestId("usage-chart-unpriced")).toContainText("1 model has no price and is not included in cost (trend/unpriced)");
  await expect(chart(page).getByTestId("usage-chart-unpriced")).toContainText("1 request has no price, so the bars are a lower bound");
  await expect(legend.locator('[data-series="trend/unpriced"]')).toHaveCount(0);
  const today = isoDay(new Date());
  await hoverSlot(page, today);
  await expect(tooltip(page)).toContainText("use/a");
  await expect(tooltip(page)).not.toContainText("trend/unpriced");
  // Costs are still the priced part of the day, not zero.
  await expect(tooltip(page)).toContainText("$");

  await metric(page, "Tokens").click();
  await expect(legend.locator('[data-series="trend/unpriced"]')).toHaveCount(1);
  await expect(chart(page).getByTestId("usage-chart-unpriced")).toHaveCount(0);
});

test("series: eight hues, and the tail folds into Other with its names in the tooltip", async ({ page, request }) => {
  await signInAsAdmin(page, request);
  await page.goto("/usage");
  const day = daysAgo(40);
  await setRange(page, day, day);
  const legend = chart(page).getByTestId("usage-chart-legend");
  await expect(legend.locator("[data-series]")).toHaveCount(9);
  const names = await legend.locator("[data-series]").evaluateAll((items) => items.map((item) => item.getAttribute("data-series")));
  expect(names).toContain("Other");
  expect(names.filter((name) => name !== "Other")).toHaveLength(8);
  // Other names the models it stands for, and the tooltip lists them.
  await expect(legend.locator('[data-series="Other"]')).toHaveAttribute("title", "fold/m9");
  await hoverSlot(page, day);
  await expect(tooltip(page)).toContainText("Other");
  await expect(tooltip(page)).toContainText("fold/m9");
  await expect(tooltip(page)).toContainText(`${sumBy(await apiRows(request, day, day), "total_tokens").toLocaleString()} tokens`);
});

test("x axis: a day without usage keeps its slot", async ({ page, request }) => {
  await signInAsAdmin(page, request);
  await page.goto("/usage");
  await setRange(page, daysAgo(6), daysAgo(0));
  await expect(daySlots(page)).toHaveCount(7);
  const quiet = slot(page, daysAgo(3));
  await expect(quiet).toHaveCount(1);
  await hoverSlot(page, daysAgo(3));
  await expect(tooltip(page)).toContainText(`${daysAgo(3)}`);
  await expect(tooltip(page)).toContainText("No usage");
  await hoverSlot(page, daysAgo(1));
  await expect(tooltip(page)).toContainText("trend/a");
});

test("tooltip: by hover and by keyboard focus, one per day", async ({ page, request }) => {
  await signInAsAdmin(page, request);
  await page.goto("/usage");
  const today = isoDay(new Date());
  await expect(tooltip(page)).toHaveCount(0);
  await hoverSlot(page, today);
  await expect(tooltip(page)).toBeVisible();
  await expect(tooltip(page)).toContainText(`${today}`);
  await expect(tooltip(page)).toContainText("UTC");
  // Leaving the chart closes it.
  await chart(page).getByText("All times UTC").hover();
  await expect(tooltip(page)).toHaveCount(0);

  // Tab reaches the chart from the toggle; arrows and End walk the days.
  await metric(page, "Cost").focus();
  await page.keyboard.press("Tab");
  await expect(daySlots(page).first()).toBeFocused();
  await expect(tooltip(page)).toBeVisible();
  await expect(tooltip(page)).toContainText(daysAgo(29));
  await page.keyboard.press("ArrowRight");
  await expect(daySlots(page).nth(1)).toBeFocused();
  await expect(tooltip(page)).toContainText(daysAgo(28));
  await page.keyboard.press("End");
  await expect(daySlots(page).last()).toBeFocused();
  await expect(tooltip(page)).toContainText(today);
  await page.keyboard.press("Home");
  await expect(daySlots(page).first()).toBeFocused();
});

test("phone: the card scrolls, the page does not, and a tap opens the tooltip", async ({ page, request, browser }) => {
  await signInAsAdmin(page, request);
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true });
  await context.addCookies((await request.storageState()).cookies);
  const phone = await context.newPage();
  try {
    for (const path of ["/", "/usage"]) {
      await phone.goto(path);
      await expect(phone.getByTestId("usage-chart")).toBeVisible();
      await expect(daySlots(phone).first()).toBeVisible();
      expect(await phone.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), `${path} overflows`).toBe(true);
    }
    // 30 days are wider than a phone: they scroll inside the card, starting at the recent end.
    const scroller = phone.getByTestId("usage-chart").locator("div.overflow-x-auto");
    expect(await scroller.evaluate((element) => element.scrollWidth > element.clientWidth)).toBe(true);
    expect(await scroller.evaluate((element) => element.scrollLeft > 0)).toBe(true);
    const today = isoDay(new Date());
    await slot(phone, today).tap();
    await expect(tooltip(phone)).toBeVisible();
    await expect(tooltip(phone)).toContainText(today);
  } finally {
    await context.close();
  }
});

test("a plain member's Usage page still works, and is empty rather than broken", async ({ browser }) => {
  const context = await browser.newContext();
  const member = await context.newPage();
  try {
    await signIn(member, VIEWER, "/usage");
    await expect(member.getByRole("heading", { name: "Usage", level: 1 })).toBeVisible();
    await expect(chart(member).getByTestId("usage-chart-empty")).toBeVisible();
    await expect(member.getByTestId("state-error")).toHaveCount(0);
    await expect(member.getByText("No usage in this period.")).toBeVisible();
    await metric(member, "Cost").click();
    await expect(metric(member, "Cost")).toHaveAttribute("aria-pressed", "true");
  } finally {
    await context.close();
  }
});
