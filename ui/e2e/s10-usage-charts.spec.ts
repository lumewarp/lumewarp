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

  // Every row carries the colour of its bar, and Other carries the neutral.
  const legend = await legendColours(page);
  const rows = await tooltip(page).locator("li").evaluateAll((items) =>
    items.map((item) => [
      item.querySelector('[data-testid="usage-chart-tooltip-name"]')!.textContent,
      getComputedStyle(item.querySelector('[data-testid="usage-chart-tooltip-swatch"]')!).backgroundColor,
    ] as [string, string]),
  );
  expect(rows.length).toBeGreaterThan(1);
  for (const [name, colour] of rows) expect([name, colour]).toEqual([name, legend.get(name)]);
  const other = rows.find(([name]) => name === "Other");
  expect(other).toBeTruthy();
  const neutral = await page.evaluate(() => {
    const probe = document.createElement("div");
    probe.style.background = "var(--series-other)";
    document.body.append(probe);
    const colour = getComputedStyle(probe).backgroundColor;
    probe.remove();
    return colour;
  });
  expect(other![1]).toBe(neutral);
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
