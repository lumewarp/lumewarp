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

/** Answer only the chart's own query (`group_by=day,model`) with crafted rows. */
async function mockChart(page: Page, items: Record<string, unknown>[]) {
  await page.route("**/api/v1/usage?*", async (route) => {
    if (new URL(route.request().url()).searchParams.get("group_by") !== "day,model") return route.continue();
    await route.fulfill({ status: 200, json: { items, next_cursor: null } });
  });
}

const usageRow = (day: string, model: string, tokens: number, cost: number, unpriced = 0) => ({
  date: day,
  model,
  requests: 1,
  prompt_tokens: tokens,
  completion_tokens: 0,
  total_tokens: tokens,
  cost_nano_usd: cost,
  unknown_cost_count: unpriced,
});

/** The y axis labels, from the baseline up. */
const yLabels = async (page: Page) => (await page.getByTestId("usage-chart-yaxis").locator("span").allTextContents()).map((label) => label.trim());

/** The palette slots as the browser resolves them, in slot order. */
async function palette(page: Page) {
  return page.evaluate(() =>
    Array.from({ length: 8 }, (_, index) => {
      const probe = document.createElement("div");
      probe.style.background = `var(--series-${index + 1})`;
      document.body.append(probe);
      const colour = getComputedStyle(probe).backgroundColor;
      probe.remove();
      return colour;
    }),
  );
}

/** name → swatch colour, in legend order. */
async function legendColours(page: Page) {
  const items = await page.getByTestId("usage-chart-legend").locator("[data-series]").evaluateAll((nodes) =>
    nodes.map((node) => [node.getAttribute("data-series"), getComputedStyle(node.querySelector("span")!).backgroundColor] as [string, string]),
  );
  return new Map(items);
}

/** Every drawn segment of a day: its series, resolved fill and drawn box. */
async function barSegments(page: Page, day: string) {
  return page.locator(`[data-testid="usage-chart-bar"][data-bar-day="${day}"]`).evaluateAll((nodes) =>
    nodes.map((node) => {
      const box = node.getBoundingClientRect();
      return { series: node.getAttribute("data-bar-series") ?? "", fill: getComputedStyle(node).fill, height: box.height, top: box.y };
    }),
  );
}

/** Pixels per unit, from the axis it drew: the "0" label's centre and the top tick's. */
async function yScale(page: Page) {
  const labels = await page.getByTestId("usage-chart-yaxis").locator("span").evaluateAll((nodes) =>
    nodes.map((node) => {
      const box = node.getBoundingClientRect();
      return { text: node.textContent ?? "", centre: box.y + box.height / 2 };
    }),
  );
  const max = Number(await page.getByTestId("usage-chart").locator("svg").getAttribute("data-max"));
  expect(max).toBeGreaterThan(0);
  const zero = labels.find((label) => label.text === "0" || label.text === "$0");
  expect(zero, "the axis has a zero label").toBeTruthy();
  const top = labels[labels.length - 1];
  return { max, pxPerUnit: (zero!.centre - top.centre) / max };
}

/**
 * A day's bars are really drawn: one segment per non-zero series, each with its legend's colour, each as tall
 * as the axis says its value is (±2.5 px for the 2 px surface gaps and the rounded end), and together as tall as
 * the day's total.
 */
async function expectBarGeometry(page: Page, day: string, values: Map<string, number>) {
  const scale = await yScale(page);
  const segments = await barSegments(page, day);
  const expected = [...values].filter(([, value]) => value > 0);
  expect(segments.map((segment) => segment.series).sort(), `segments on ${day}`).toEqual(expected.map(([name]) => name).sort());
  const legend = await legendColours(page);
  for (const segment of segments) {
    expect(segment.fill, `${segment.series} is painted with its legend colour`).toBe(legend.get(segment.series));
    const drawn = (values.get(segment.series) ?? 0) * scale.pxPerUnit;
    expect(drawn, `${segment.series} has a value`).toBeGreaterThan(0);
    expect(Math.abs(segment.height - drawn), `${segment.series} height`).toBeLessThanOrEqual(2.5);
  }
  const total = expected.reduce((sum, [, value]) => sum + value, 0);
  const stacked = segments.reduce((sum, segment) => sum + segment.height, 0) + 2 * Math.max(0, segments.length - 1);
  expect(Math.abs(stacked - total * scale.pxPerUnit), `${day} stack height`).toBeLessThanOrEqual(2.5);
}

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
  const costs = new Map(rows.map((row) => [row.model ?? "—", row.cost_nano_usd]));
  const total = sumBy(rows, "total_tokens");

  await hoverSlot(page, today);
  await expect(tooltip(page)).toBeVisible();
  for (const model of tokens.keys()) await expect(tooltip(page)).toContainText(model);
  await expect(tooltip(page)).toContainText(`${total.toLocaleString()} tokens`);

  // The bars are really drawn: segments, colours and heights that follow the axis; the empty day has none.
  await expectBarGeometry(page, today, tokens);
  await expectBarGeometry(page, daysAgo(3), new Map());
  for (const segment of await barSegments(page, today)) {
    const swatch = tooltip(page).locator("li", { hasText: segment.series }).first().getByTestId("usage-chart-tooltip-swatch");
    expect(await swatch.evaluate((node) => getComputedStyle(node).backgroundColor), `${segment.series} tooltip swatch`).toBe(segment.fill);
  }
  // And the same is true of cost, which is a different scale.
  await metric(page, "Cost").click();
  await expectBarGeometry(page, today, costs);
  await expectBarGeometry(page, daysAgo(3), new Map());
  await metric(page, "Tokens").click();

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
  // Nine series do not fit in the plot's height: the biggest are listed and the rest are counted, with the
  // total still over all nine.
  await hoverSlot(page, day);
  await expect(tooltip(page).getByTestId("usage-chart-tooltip-name")).toHaveCount(6);
  await expect(tooltip(page).getByTestId("usage-chart-tooltip-more")).toHaveText("3 more in the data table");
  await expect(tooltip(page)).toContainText("fold/m1");
  await expect(tooltip(page)).toContainText(`${sumBy(await apiRows(request, day, day), "total_tokens").toLocaleString()} tokens`);
  // Everything the tooltip left out is in the data table.
  await page.getByTestId("usage-chart-table-toggle").click();
  for (const name of ["fold/m1", "fold/m7", "fold/m8", "Other"]) {
    await expect(page.getByTestId("usage-chart-table").getByRole("columnheader", { name, exact: true })).toBeVisible();
  }
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

  // Every row carries the colour of its bar, and Other carries the neutral.
  const legend = await legendColours(page);
  const rows = await tooltip(page)
    .locator("li")
    .evaluateAll((items) =>
      items
        .filter((item) => item.querySelector('[data-testid="usage-chart-tooltip-swatch"]'))
        .map((item) => [
          item.querySelector('[data-testid="usage-chart-tooltip-name"]')!.textContent,
          getComputedStyle(item.querySelector('[data-testid="usage-chart-tooltip-swatch"]')!).backgroundColor,
        ] as [string, string]),
    );
  expect(rows.length).toBeGreaterThan(1);
  for (const [name, colour] of rows) expect([name, colour]).toEqual([name, legend.get(name)]);
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

test("y axis: the labels stay pinned while a phone scrolls the plot", async ({ page, request, browser }) => {
  await signInAsAdmin(page, request);
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  await context.addCookies((await request.storageState()).cookies);
  const phone = await context.newPage();
  try {
    await phone.goto("/usage");
    await expect(daySlots(phone)).toHaveCount(30);
    const axis = phone.getByTestId("usage-chart-yaxis");
    await expect(axis).toBeVisible();
    // The axis is not inside the scroller, and the plot has scrolled to its recent end.
    expect(await axis.evaluate((node) => node.closest(".overflow-x-auto") === null)).toBe(true);
    const scroller = phone.getByTestId("usage-chart").locator("div.overflow-x-auto");
    expect(await scroller.evaluate((node) => node.scrollLeft)).toBeGreaterThan(0);
    // Every label is inside the card's visible box, not pushed off the left edge by the scroll.
    const card = await phone.getByTestId("usage-chart").boundingBox();
    for (const label of await axis.locator("span").all()) {
      const box = await label.boundingBox();
      expect(box, "a y label is rendered").not.toBeNull();
      expect(box!.x).toBeGreaterThanOrEqual(card!.x);
      expect(box!.x + box!.width).toBeLessThanOrEqual(card!.x + card!.width);
    }
  } finally {
    await context.close();
  }
});

test("palette: the validated reference slots, with their own light and dark values", async ({ page, request }) => {
  await signInAsAdmin(page, request);
  await page.goto("/usage");
  await expect(daySlots(page).first()).toBeVisible();
  const light = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"];
  const dark = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"];
  const resolve = async (hexes: string[]) =>
    page.evaluate((list) => list.map((hex) => {
      const probe = document.createElement("div");
      probe.style.background = hex;
      document.body.append(probe);
      const colour = getComputedStyle(probe).backgroundColor;
      probe.remove();
      return colour;
    }), hexes);
  // The legend paints with those slots, in slot order.
  const names = ["trend/a", "trend/b", "trend/c", "trend/partial", "trend/unpriced", "use/a", "use/b", "use/c"];
  const lightSlots = await palette(page);
  expect(lightSlots).toEqual(await resolve(light));
  const colours = await legendColours(page);
  names.forEach((name, index) => expect([name, colours.get(name)]).toEqual([name, lightSlots[index]]));

  await page.evaluate(() => localStorage.setItem("lumewarp.theme", "dark"));
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(daySlots(page).first()).toBeVisible();
  const darkSlots = await palette(page);
  expect(darkSlots).toEqual(await resolve(dark));
  const darkColours = await legendColours(page);
  names.forEach((name, index) => expect([name, darkColours.get(name)]).toEqual([name, darkSlots[index]]));
  await page.evaluate(() => localStorage.setItem("lumewarp.theme", "light"));
});

test("slots: the drawn models take the palette in order, and a filter keeps a survivor's colour", async ({ page, request }) => {
  await signInAsAdmin(page, request);
  await page.goto("/usage");
  const day = daysAgo(40);
  await setRange(page, day, day);
  const legend = chart(page).getByTestId("usage-chart-legend");
  await expect(legend.locator("[data-series]")).toHaveCount(9);
  const assigned = await legendColours(page);
  const slots = await palette(page);
  const named = (await legend.locator("[data-series]").evaluateAll((nodes) => nodes.map((node) => node.getAttribute("data-series") ?? ""))).filter((name) => name !== "Other");
  expect(named).toEqual(["fold/m1", "fold/m2", "fold/m3", "fold/m4", "fold/m5", "fold/m6", "fold/m7", "fold/m8"]);
  named.forEach((name, index) => expect([name, assigned.get(name)]).toEqual([name, slots[index]]));
  // Other is the neutral, not a slot.
  expect(assigned.get("Other")).not.toBe(slots[0]);

  // A narrower range that keeps some of them keeps their colours: the survivors are not repainted.
  await setRange(page, daysAgo(39), daysAgo(39));
  await expect(legend.locator("[data-series]")).toHaveCount(3);
  const survivors = await legendColours(page);
  for (const name of ["fold/m1", "fold/m3"]) expect([name, survivors.get(name)]).toEqual([name, assigned.get(name)]);
});

test("ranking: tokens decide the named eight, so the toggle names the same models with the same colours", async ({ page, request }) => {
  await signInAsAdmin(page, request);
  await page.goto("/usage");
  const day = daysAgo(50);
  await setRange(page, day, day);
  const legend = chart(page).getByTestId("usage-chart-legend");
  await expect(legend.locator("[data-series]")).toHaveCount(9);
  const tokens = await legendColours(page);
  const other = page.locator('[data-series="Other"]');
  await expect(other).toHaveAttribute("title", "rank/m10, rank/m9");

  await metric(page, "Cost").click();
  await expect(page.getByTestId("usage-chart-unpriced")).toHaveCount(0);
  const cost = await legendColours(page);
  expect([...cost.keys()].sort()).toEqual([...tokens.keys()].sort());
  for (const [name, colour] of cost) expect(tokens.get(name), name).toBe(colour);
  // The tail stays folded in both modes: m10 outranks everything on cost but not on tokens.
  await expect(other).toHaveAttribute("title", "rank/m10, rank/m9");
});

test("axis: exact labels for tiny token peaks, tiny costs and no usage at all", async ({ page, request }) => {
  await signInAsAdmin(page, request);
  await page.goto("/usage");
  const today = isoDay(new Date());
  // A different range each time: the same query key would answer from the cache.
  let step = 1;
  const setPeak = async (items: Record<string, unknown>[]) => {
    await page.unroute("**/api/v1/usage?*").catch(() => undefined);
    await mockChart(page, items);
    await setRange(page, daysAgo(step++), today);
  };

  // Two tokens: whole-number steps, no 0, 1, 1, 2, 2.
  await setPeak([usageRow(today, "tiny/a", 2, 0)]);
  await expect.poll(() => yLabels(page)).toEqual(["0", "1", "2"]);
  // One token.
  await setPeak([usageRow(today, "tiny/a", 1, 0)]);
  await expect.poll(() => yLabels(page)).toEqual(["0", "1"]);

  // $0.00002: distinct labels, not four printings of the same value.
  await setPeak([usageRow(today, "tiny/a", 1, 20_000)]);
  await metric(page, "Cost").click();
  await expect.poll(() => yLabels(page)).toEqual(["$0", "$5.0e-6", "$1.0e-5", "$1.5e-5", "$2.0e-5"]);
  // Nothing clips: every label sits inside the card, however wide its text.
  const card = (await chart(page).boundingBox())!;
  for (const label of await page.getByTestId("usage-chart-yaxis").locator("span").all()) {
    const box = (await label.boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(card.x);
    expect(box.x + box.width).toBeLessThanOrEqual(card.x + card.width);
  }
  // One nano-USD, the smallest cost there is.
  await setPeak([usageRow(today, "tiny/a", 1, 1)]);
  await expect.poll(() => yLabels(page)).toEqual(["$0", "$1.0e-9"]);

  // No usage at all: one baseline label, no invented scale.
  await setPeak([usageRow(today, "tiny/a", 0, 0)]);
  await expect.poll(() => yLabels(page)).toEqual(["$0"]);
  await metric(page, "Tokens").click();
  await expect.poll(() => yLabels(page)).toEqual(["0"]);
  // Every model unpriced in cost is the same empty scale, with the note that says why.
  await metric(page, "Cost").click();
  await setPeak([usageRow(today, "tiny/a", 900, 0, 1)]);
  await metric(page, "Cost").click();
  await expect(page.getByTestId("usage-chart-unpriced")).toContainText("1 model has no price");
  await expect.poll(() => yLabels(page)).toEqual(["$0"]);
});

test("dates: clearing a field is not a crash, and an impossible range is the API's refusal", async ({ page, request }) => {
  await signInAsAdmin(page, request);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/usage");
  await expect(daySlots(page).first()).toBeVisible();

  await page.getByLabel("From").fill("");
  await page.getByLabel("To (inclusive)").fill("");
  await expect(chart(page).getByTestId("usage-chart-empty")).toHaveText("Choose a date range.");
  expect(errors).toEqual([]);
  await expect(page.getByRole("heading", { name: "Usage", level: 1 })).toBeVisible();

  // A range that runs backwards is refused by the API and shown, not swallowed.
  await setRange(page, isoDay(new Date()), daysAgo(5));
  await expect(chart(page).getByTestId("usage-chart-error")).toContainText("must be after from");
  // A range past the API's 400 days: the refusal, and no 740,000 day slots built on the way.
  await setRange(page, "0002-01-01", isoDay(new Date()));
  await expect(chart(page).getByTestId("usage-chart-error")).toContainText("400 days");
  await expect(daySlots(page)).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("tooltip: a long model name is shown whole, not truncated", async ({ page, request }) => {
  await signInAsAdmin(page, request);
  await page.goto("/usage");
  const day = daysAgo(8);
  const name = "trend/a-very-long-model-name-for-the-tooltip";
  await setRange(page, day, day);
  await hoverSlot(page, day);
  const label = tooltip(page).getByTestId("usage-chart-tooltip-name");
  await expect(label).toHaveText(name);
  const box = await label.evaluate((node) => ({ scroll: node.scrollWidth, client: node.clientWidth }));
  expect(box.scroll).toBeLessThanOrEqual(box.client + 1);
});

test("tooltip: beside the slot, on the far side of the halving line", async ({ page, request }) => {
  await signInAsAdmin(page, request);
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/usage");
  await expect(daySlots(page)).toHaveCount(30);
  const card = (await chart(page).boundingBox())!;

  // Right half: the tooltip ends before the slot starts.
  const last = page.getByTestId("usage-day").last();
  await hoverSlot(page, isoDay(new Date()));
  const tip = (await tooltip(page).boundingBox())!;
  const slotBox = (await last.boundingBox())!;
  expect(tip.x + tip.width).toBeLessThanOrEqual(slotBox.x);
  expect(tip.x).toBeGreaterThanOrEqual(card.x);
  expect(tip.x + tip.width).toBeLessThanOrEqual(card.x + card.width);

  // Left half: it starts after the slot ends.
  await page.mouse.move(2, 2);
  await page.getByLabel("From").fill(daysAgo(9));
  await expect(daySlots(page)).toHaveCount(10);
  const first = page.getByTestId("usage-day").first();
  await hoverSlot(page, daysAgo(9));
  const tip2 = (await tooltip(page).boundingBox())!;
  const firstBox = (await first.boundingBox())!;
  expect(tip2.x).toBeGreaterThanOrEqual(firstBox.x + firstBox.width);
  expect(tip2.x + tip2.width).toBeLessThanOrEqual(card.x + card.width);
});

test("subtitle: the date range never breaks across lines on a phone", async ({ page, request, browser }) => {
  await signInAsAdmin(page, request);
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  await context.addCookies((await request.storageState()).cookies);
  const phone = await context.newPage();
  try {
    await phone.goto("/usage");
    const range = phone.getByTestId("usage-chart-range");
    await expect(range).toBeVisible();
    // A wrapped range would be two lines; text-sm is 20px, so one line is well under 26.
    const box = (await range.boundingBox())!;
    expect(box.height).toBeLessThan(26);
    expect(box.width).toBeGreaterThan(120);
    expect(await phone.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  } finally {
    await context.close();
  }
});

test("colours: a model drawn in two consecutive renders keeps its fill across pages, ranges and the toggle", async ({ page, request }) => {
  await signInAsAdmin(page, request);
  // Overview draws a and b; Usage draws b and c; widening Usage brings a back.
  const answer = async (route: import("@playwright/test").Route, names: string[]) =>
    route.fulfill({ status: 200, json: { items: names.map((name) => usageRow(isoDay(new Date()), name, 100, 1000)), next_cursor: null } });
  await page.route("**/api/v1/usage?*", (route) =>
    new URL(route.request().url()).searchParams.get("group_by") === "day,model" ? answer(route, page.url().endsWith("/usage") ? ["b", "c"] : ["a", "b"]) : route.continue(),
  );
  const legendCount = (count: number) => expect(chart(page).getByTestId("usage-chart-legend").locator("[data-series]")).toHaveCount(count);
  await page.goto("/");
  await legendCount(2);
  const overview = await legendColours(page);
  expect([...overview.keys()]).toEqual(["a", "b"]);

  await chart(page).getByRole("link", { name: "Open Usage" }).click();
  await expect(page).toHaveURL(/\/usage$/);
  await legendCount(2);
  const usage = await legendColours(page);
  expect([...usage.keys()]).toEqual(["b", "c"]);
  // b was drawn on both pages: same fill. c is new.
  expect(usage.get("b")).toBe(overview.get("b"));
  expect(usage.get("c")).not.toBe(usage.get("b"));

  // Widen the set: a comes back as a returning model, and no survivor is repainted.
  await metric(page, "Cost").click();
  await expect(metric(page, "Cost")).toHaveAttribute("aria-pressed", "true");
  await metric(page, "Tokens").click();
  await legendCount(2);
  const beforeWiden = await legendColours(page);
  await page.route("**/api/v1/usage?*", async (route) => {
    if (new URL(route.request().url()).searchParams.get("group_by") !== "day,model") return route.continue();
    await route.fulfill({ status: 200, json: { items: ["a", "b", "c"].map((name) => usageRow(isoDay(new Date()), name, 100, 1000)), next_cursor: null } });
  });
  await setRange(page, daysAgo(3), isoDay(new Date()));
  await legendCount(3);
  const widened = await legendColours(page);
  expect([...widened.keys()]).toEqual(["a", "b", "c"]);
  for (const name of ["b", "c"]) expect([name, widened.get(name)]).toEqual([name, beforeWiden.get(name)]);

  // Tokens → Cost → Tokens changes no fill at all.
  await metric(page, "Cost").click();
  await expect(metric(page, "Cost")).toHaveAttribute("aria-pressed", "true");
  await legendCount(3);
  for (const [name, colour] of await legendColours(page)) expect([name, colour]).toEqual([name, widened.get(name)]);
  await metric(page, "Tokens").click();
  await legendCount(3);
  for (const [name, colour] of await legendColours(page)) expect([name, colour]).toEqual([name, widened.get(name)]);
});

test("cost: a model priced at zero is drawn as $0, and unknown requests are counted, not mislabelled", async ({ page, request }) => {
  await signInAsAdmin(page, request);
  await page.route("**/api/v1/usage?*", async (route) => {
    if (new URL(route.request().url()).searchParams.get("group_by") !== "day,model") return route.continue();
    const today = isoDay(new Date());
    await route.fulfill({
      status: 200,
      json: {
        items: [
          // One free request, then two requests of which one is priced at zero and one has no price.
          usageRow(today, "free-only", 10, 0),
          { ...usageRow(today, "free-and-unknown", 20, 0, 1), requests: 2 },
          // A priced request with an unknown sibling: cost is known for part of it.
          { ...usageRow(today, "positive-and-unknown", 30, 5_000_000, 1), requests: 2 },
        ],
        next_cursor: null,
      },
    });
  });
  await page.goto("/usage");
  await expect(daySlots(page).last()).toBeVisible();
  await metric(page, "Cost").click();
  const note = chart(page).getByTestId("usage-chart-unpriced");
  // Free is known: nothing is called unpriced, and only the two unknown requests are reported.
  await expect(note).toHaveCount(1);
  await expect(note).not.toContainText("not included in cost");
  await expect(note).toContainText("2 requests have no price, so the bars are a lower bound");

  const today = isoDay(new Date());
  await hoverSlot(page, today);
  await expect(tooltip(page).getByTestId("usage-chart-tooltip-name")).toHaveCount(3);
  const text = await tooltip(page).textContent();
  expect(text).toContain("free-only$0");
  expect(text).toContain("free-and-unknown$0");
  expect(text).toContain("positive-and-unknown$0.005");
  expect(text).not.toContain("No usage");
  expect(text).toContain("Total$0.005");

  // A day with only unpriced traffic says so, rather than claiming there was no usage.
  await page.unroute("**/api/v1/usage?*");
  await mockChart(page, [{ ...usageRow(today, "unpriced-only", 10, 0, 1), requests: 1 }]);
  await setRange(page, daysAgo(2), today);
  await hoverSlot(page, today);
  expect(await tooltip(page).textContent()).toContain("No priced usage");
  await expect(chart(page).getByTestId("usage-chart-unpriced")).toContainText("1 model has no price and is not included in cost (unpriced-only)");
});

test("tooltip: Other's row carries the neutral swatch", async ({ page, request }) => {
  await signInAsAdmin(page, request);
  // Twenty equal models: eight are named and the other twelve add up to the biggest row on the day.
  const today = isoDay(new Date());
  await mockChart(page, Array.from({ length: 20 }, (_, index) => usageRow(today, `m${String(index).padStart(2, "0")}`, 100, 1000)));
  await page.goto("/usage");
  await expect(daySlots(page).last()).toBeVisible();
  await hoverSlot(page, today);
  const row = tooltip(page).locator("li", { has: page.getByTestId("usage-chart-tooltip-name").filter({ hasText: "Other" }) }).first();
  await expect(row).toBeVisible();
  const swatch = await row.getByTestId("usage-chart-tooltip-swatch").evaluate((node) => getComputedStyle(node).backgroundColor);
  const neutral = await page.evaluate(() => {
    const probe = document.createElement("div");
    probe.style.background = "var(--series-other)";
    document.body.append(probe);
    const colour = getComputedStyle(probe).backgroundColor;
    probe.remove();
    return colour;
  });
  expect(swatch).toBe(neutral);
});

test("keys: hover leaves a focused day's tooltip and tab stop alone, a pen takes one path, a scroll drops a tap", async ({ page, request, browser }) => {
  await signInAsAdmin(page, request);
  await page.setViewportSize({ width: 390, height: 844 });
  const today = isoDay(new Date());
  const far = daysAgo(399);
  await mockChart(page, ["a", "b"].map((name) => usageRow(today, name, 100, 1000)));
  await page.goto("/usage");
  await setRange(page, far, today);
  await expect(daySlots(page)).toHaveCount(400);
  const tabStops = () =>
    page.getByTestId("usage-day").evaluateAll((nodes) => nodes.flatMap((node, index) => (node.tabIndex === 0 ? [index] : [])));

  // Keyboard: Tab in, End to the last day (its focus scrolls it into view), then hover in and out over it.
  await metric(page, "Cost").focus();
  await page.keyboard.press("Tab");
  await expect(daySlots(page).first()).toBeFocused();
  await page.keyboard.press("End");
  await expect(daySlots(page).last()).toBeFocused();
  await expect(tooltip(page)).toBeVisible();
  expect(await page.getByTestId("usage-chart").locator("div.overflow-x-auto").evaluate((node) => node.scrollLeft)).toBeGreaterThan(0);
  expect(await tabStops()).toEqual([399]);
  await page.mouse.move(1, 1);
  await daySlots(page).last().hover();
  await page.mouse.move(1, 1);
  await expect(tooltip(page)).toBeVisible();
  await expect(daySlots(page).last()).toBeFocused();
  expect(await tabStops()).toEqual([399]);

  // A pen hovers like a mouse and never toggles: press, then leave and blur, and nothing stays pinned.
  const box = (await daySlots(page).last().boundingBox())!;
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: box.x + box.width / 2, y: box.y + box.height / 2, pointerType: "pen" });
  await expect(tooltip(page)).toBeVisible();
  await cdp.send("Input.dispatchMouseEvent", { type: "mousePressed", x: box.x + box.width / 2, y: box.y + box.height / 2, button: "left", clickCount: 1, pointerType: "pen" });
  await cdp.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: box.x + box.width / 2, y: box.y + box.height / 2, button: "left", clickCount: 1, pointerType: "pen" });
  // A press is not a toggle: the row is still shown, so the pen did not take the touch path as well.
  await expect(tooltip(page)).toBeVisible();
  await cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: 1, y: 1, pointerType: "pen" });
  await metric(page, "Tokens").focus();
  await expect(tooltip(page)).toHaveCount(0);

  // Touch: a tap pins a day, and scrolling the plot lets the pin go with the place it was on.
  const touch = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true });
  await touch.addCookies((await request.storageState()).cookies);
  const phone = await touch.newPage();
  try {
    await mockChart(phone, ["a", "b"].map((name) => usageRow(today, name, 100, 1000)));
    await phone.goto("/usage");
    await setRange(phone, far, today);
    await expect(daySlots(phone)).toHaveCount(400);
    await daySlots(phone).last().tap();
    await expect(tooltip(phone)).toBeVisible();
    await phone.getByTestId("usage-chart").locator("div.overflow-x-auto").evaluate((node) => (node.scrollLeft = 0));
    await expect.poll(() => tooltip(phone).count()).toBe(0);
    // The keyboard keeps its day through the scroll its own focus caused.
    await daySlots(phone).last().focus();
    await expect(tooltip(phone)).toBeVisible();
    await phone.mouse.move(1, 1);
    await expect(tooltip(phone)).toBeVisible();
  } finally {
    await touch.close();
  }
});

test("tooltip: nine long names fit the plot, count the rest, and all of them are in the table", async ({ page, request, browser }) => {
  await signInAsAdmin(page, request);
  const long = (index: number) => `model-${index}-` + "a-very-long-model-name-".repeat(7);
  const today = isoDay(new Date());
  const items = Array.from({ length: 8 }, (_, index) => usageRow(today, long(index), 100 + index, 1000));
  const touch = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true });
  await touch.addCookies((await request.storageState()).cookies);
  const phone = await touch.newPage();
  try {
    await mockChart(phone, items);
    await phone.goto("/usage");
    await expect(daySlots(phone)).toHaveCount(30);
    await daySlots(phone).last().tap();
    const tip = tooltip(phone);
    await expect(tip).toBeVisible();
    const tipBox = (await tip.boundingBox())!;
    // A name is clamped to two lines, so measure the row: the name's own box is clipped, not resized.
    const rows = await tip.locator("li").evaluateAll((nodes) =>
      nodes
        .filter((node) => node.querySelector('[data-testid="usage-chart-tooltip-name"]'))
        .map((node) => {
          const box = node.getBoundingClientRect();
          return { text: node.textContent ?? "", top: box.y, bottom: box.y + box.height, height: box.height };
        }),
    );
    expect(rows.length).toBeGreaterThan(0);
    expect(rows.length).toBeLessThan(8);
    for (const row of rows) {
      expect(row.top, "no row starts above the tooltip").toBeGreaterThanOrEqual(tipBox.y);
      expect(row.bottom, "no row ends below the tooltip").toBeLessThanOrEqual(tipBox.y + tipBox.height);
      expect(row.height, "a name is at most two lines").toBeLessThanOrEqual(46);
    }
    await expect(tip.getByTestId("usage-chart-tooltip-more")).toHaveText(`${8 - rows.length} more in the data table`);
    // The tooltip stays inside the plot, so the table's toggle is still there for a finger.
    const toggle = phone.getByTestId("usage-chart-table-toggle");
    expect((await toggle.boundingBox())!.y).toBeGreaterThanOrEqual(tipBox.y + tipBox.height);
    await toggle.tap();
    const table = phone.getByTestId("usage-chart-table");
    await expect(table).toBeVisible();
    for (let index = 0; index < 8; index++) await expect(table.getByRole("columnheader", { name: long(index), exact: true })).toBeVisible();
  } finally {
    await touch.close();
  }
});

test("money: an amount fmtUsd would round to zero keeps its magnitude in the tooltip and the table", async ({ page, request }) => {
  await signInAsAdmin(page, request);
  const today = isoDay(new Date());
  // Two nano-USD: $0.000000002, which the table's currency format prints as $0.00.
  await mockChart(page, [usageRow(today, "two-nano", 1, 2)]);
  await page.goto("/usage");
  await expect(daySlots(page).last()).toBeVisible();
  await metric(page, "Cost").click();
  await hoverSlot(page, today);
  await expect(tooltip(page)).toContainText("$2.0e-9");
  await expect(tooltip(page)).not.toContainText("$0.00");
  await page.getByTestId("usage-chart-table-toggle").click();
  const cells = await page.getByTestId("usage-chart-table").getByRole("row").last().getByRole("cell").allTextContents();
  expect(cells).toContain("$2.0e-9");
  expect(cells).not.toContain("$0.00");
});

test("table toggle: aria-controls names an element in both states", async ({ page, request }) => {
  await signInAsAdmin(page, request);
  await page.goto("/usage");
  const toggle = chart(page).getByTestId("usage-chart-table-toggle");
  const target = () =>
    toggle.evaluate((node) => {
      const controlled = document.getElementById(node.getAttribute("aria-controls") ?? "");
      return controlled ? { found: true, hidden: (controlled as HTMLElement).hidden } : { found: false, hidden: false };
    });
  expect(await target()).toEqual({ found: true, hidden: true });
  await toggle.click();
  expect(await target()).toEqual({ found: true, hidden: false });
  await toggle.click();
  expect(await target()).toEqual({ found: true, hidden: true });
});
