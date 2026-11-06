import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { existsSync, readdirSync, rmSync } from "node:fs";
import { join } from "node:path";

import { confirmDialog, control, deleteTeams, dialog, ensureOrg, pick, SHOTS, shot } from "./helpers";

/*
 * The screenshot matrix, in one place: every page, every team tab, the two catalog forms, the permission picker,
 * an open Select, an open Combobox, the delete confirmation and the 409, each at desktop and 390 px, light and dark.
 *
 * One E2E_SHOTS run must produce all of it, so this spec writes the whole matrix under its own `s7-` names and
 * asserts the resulting file set: a missing shot fails the run. A normal `pnpm test:e2e` skips the test and
 * writes nothing.
 */

const BREAK_GLASS = "e2e-break-glass-password";
const admin: Record<string, string> = {};

/** The 13 pages whose whole body is the screenshot. Login is shot in a session-less context, the team detail as
 * its five tabs. */
const PAGES: [string, string][] = [
  ["overview", "/"],
  ["orgs", "/orgs"],
  ["teams", "/teams"],
  ["users", "/users"],
  ["routers", "/routers"],
  ["models", "/models"],
  ["aliases", "/aliases"],
  ["upstreams", "/upstreams"],
  ["usage", "/usage"],
  ["audit", "/audit"],
  ["gateways", "/gateways"],
  ["config", "/config"],
  ["settings", "/settings"],
];
const TABS = ["Members", "Service accounts", "Keys", "Usage", "Settings"];
const SPECIALS = ["page-login", "router-form", "upstream-form", "permission-picker", "select-open", "combobox-open", "confirm", "conflict-409"];
/** The usage chart's own states; each is a picture of the card, not of the page. */
const CHARTS = ["usage-chart", "usage-chart-cost", "usage-chart-tooltip", "usage-chart-table"];
const MODES = [
  ["desktop-light", 1280, 900, "light"],
  ["desktop-dark", 1280, 900, "dark"],
  ["phone-light", 390, 844, "light"],
  ["phone-dark", 390, 844, "dark"],
] as const;

const slug = (name: string) => name.toLowerCase().replaceAll(" ", "-");
const row = (page: Page, name: string) => page.getByRole("row").filter({ has: page.getByRole("cell", { name, exact: true }) });
const myFixture = (name: string) => name.startsWith("s7-");

test.beforeEach(async ({ request }) => {
  const response = await request.post("/api/v1/session/break-glass", { data: { password: BREAK_GLASS } });
  expect(response.status()).toBe(204);
  admin["X-Lumewarp-CSRF"] = (await (await request.get("/api/v1/session")).json()).csrf_token;
});

// The catalog fixtures go again, in reference order. The team goes too, unless it has a key: the API keeps keys
// for usage history and then refuses to delete the team at all (see deleteTeams), so s7-shots stays.
test.afterEach(async ({ request }) => {
  for (const path of ["/aliases", "/routers", "/models", "/upstreams"]) {
    const rows = (await (await request.get(`/api/v1${path}?limit=200`, { headers: admin })).json()).items as { id: string; name: string }[];
    for (const r of rows) if (myFixture(r.name)) await request.delete(`/api/v1${path}/${r.id}`, { headers: admin });
  }
  await deleteTeams(request, admin, "s7-");
});

async function post<T = { id: string; name: string }>(request: APIRequestContext, path: string, data: object): Promise<T> {
  const r = await request.post(`/api/v1${path}`, { headers: admin, data });
  expect(r.status(), await r.text()).toBe(201);
  return (await r.json()) as T;
}

test("screenshots: the whole matrix, desktop and 390 px, light and dark", async ({ page, browser, request }, info) => {
  test.skip(!SHOTS, "set E2E_SHOTS to a directory to save screenshots");
  info.setTimeout(900_000);
  // Start from a clean s7-* set: a stale file from an earlier run would hide a missing shot from this one.
  if (existsSync(SHOTS!)) for (const name of readdirSync(SHOTS!)) if (name.startsWith("s7-") && name.endsWith(".png")) rmSync(join(SHOTS!, name));
  const team = await post(request, "/teams", { name: "s7-shots", org_id: await ensureOrg(request, admin) });
  await post(request, `/teams/${team.id}/members`, { email: "viewer@e2e.test", role: "member" });
  const account = await post(request, `/teams/${team.id}/service-accounts`, { name: "s7-agent", description: "Screenshots" });
  await post(request, `/teams/${team.id}/keys`, { name: "s7-key", service_account_id: account.id });
  const upstream = await post(request, "/upstreams", { name: "s7-up", kind: "vllm", base_url: "http://127.0.0.1:9/v1" });
  const model = await post(request, "/models", { name: "s7/m", upstream_id: upstream.id, upstream_model: "m" });
  // The alias refers to s7/m, so deleting the model is the 409 while deleting the alias only asks.
  await post(request, "/aliases", { name: "s7-alias", target: model.id });

  const cookies = (await request.storageState()).cookies;
  await page.context().addCookies(cookies);
  await page.goto("/");

  const noOverflow = async (where: string) => expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), `${where} overflows`).toBe(true);
  // Focus and scroll-into-view can leave a long form halfway down: a form shot starts at its top.
  const formTop = async () => {
    const body = dialog(page).locator("div.overflow-y-auto").first();
    if (await body.count()) await body.evaluate((el) => (el.scrollTop = 0));
  };
  const chart = page.getByTestId("usage-chart");
  const chartShot = async (name: string) => {
    if (SHOTS) await chart.screenshot({ path: join(SHOTS, `${name}.png`) });
  };
  const theme = async (name: "light" | "dark") => {
    // The theme is read from localStorage at start-up: write it, then reload.
    await page.evaluate((t) => localStorage.setItem("lumewarp.theme", t), name);
    await page.reload();
    await expect(page.locator("html")).toHaveAttribute("data-theme", name);
  };

  for (const [label, width, height, mode] of MODES) {
    // Login is the one page without a session: a fresh, themed context per mode.
    const anonymous = await browser.newContext({ viewport: { width, height } });
    await anonymous.addInitScript((t) => localStorage.setItem("lumewarp.theme", t), mode);
    const login = await anonymous.newPage();
    await login.goto("/login");
    await expect(login.getByLabel("Break-glass password")).toBeVisible();
    await expect(login.locator("html")).toHaveAttribute("data-theme", mode);
    await shot(login, `s7-${label}-page-login`, { settle: 300 });
    await anonymous.close();

    await page.setViewportSize({ width, height });
    await theme(mode);

    for (const [name, path] of PAGES) {
      await page.goto(path);
      await expect(page.locator("html")).toHaveAttribute("data-theme", mode);
      await expect(page.getByRole("heading", { level: 1 }).first()).toBeVisible();
      await noOverflow(`${label} ${name}`);
      await shot(page, `s7-${label}-page-${name}`, { settle: 300 });
    }

    // The usage chart: its three metrics states and its data table.
    await page.goto("/usage");
    await expect(chart).toBeVisible();
    await expect(page.getByTestId("usage-day")).toHaveCount(30);
    await chartShot(`s7-${label}-usage-chart`);
    const metricButton = (name: string) => page.getByTestId("usage-chart-metric").getByRole("button", { name, exact: true });
    await metricButton("Cost").click();
    await page.waitForTimeout(300);
    await chartShot(`s7-${label}-usage-chart-cost`);
    await metricButton("Tokens").click();
    // Hover after scrolling the card into view: scrolling afterwards would move the pointer off the bar.
    await chart.scrollIntoViewIfNeeded();
    await page.getByTestId("usage-day").last().hover();
    await page.waitForTimeout(250);
    await chartShot(`s7-${label}-usage-chart-tooltip`);
    await page.getByLabel("From").fill(new Date(Date.now() - 6 * 86_400_000).toISOString().slice(0, 10));
    await page.mouse.move(2, 2);
    await page.getByTestId("usage-chart-table-toggle").click();
    await page.waitForTimeout(250);
    await chartShot(`s7-${label}-usage-chart-table`);

    await page.goto(`/teams/${team.id}`);
    for (const tab of TABS) {
      await page.getByRole("tab", { name: tab, exact: true }).click();
      await expect(page.getByRole("tab", { name: tab, exact: true })).toHaveAttribute("aria-selected", "true");
      await noOverflow(`${label} team ${tab}`);
      await shot(page, `s7-${label}-team-${slug(tab)}`, { settle: 300 });
    }

    // The router form: a wide drawer with the schema-driven strategy settings.
    await page.goto("/routers");
    await page.getByRole("button", { name: "New router" }).click();
    await pick(dialog(page), "Strategy", "E2E tuned");
    await dialog(page).getByLabel("Name").fill("s7-shot");
    await formTop();
    await shot(page, `s7-${label}-router-form`, { settle: 450, fullPage: false });
    await page.keyboard.press("Escape");
    await expect(dialog(page)).toHaveCount(0);

    // The upstream form (a drawer) and the Select it opens.
    await page.goto("/upstreams");
    await page.getByRole("button", { name: "New upstream" }).click();
    await formTop();
    await shot(page, `s7-${label}-upstream-form`, { settle: 450, fullPage: false });
    await (await control(dialog(page), "Kind")).click();
    await shot(page, `s7-${label}-select-open`, { settle: 450, fullPage: false });
    await page.keyboard.press("Escape");
    await expect(page.getByRole("listbox")).toHaveCount(0);
    await page.keyboard.press("Escape");
    await expect(dialog(page)).toHaveCount(0);

    // The permission picker: the team form with "Selected" chosen and its options loaded.
    await page.goto("/teams");
    await page.getByRole("button", { name: "New team" }).click();
    await dialog(page).getByLabel("Name").fill("s7-shot");
    await dialog(page).getByRole("radio", { name: "Selected" }).click();
    await expect(dialog(page).getByTestId("permission-options").getByRole("group", { name: "Models" })).toBeVisible();
    await dialog(page).getByTestId("permission-picker").scrollIntoViewIfNeeded();
    await shot(page, `s7-${label}-permission-picker`, { settle: 450, fullPage: false });
    await page.keyboard.press("Escape");
    await expect(dialog(page)).toHaveCount(0);

    // The Combobox: the model form's Upstream.
    await page.goto("/models");
    await page.getByRole("button", { name: "New model" }).click();
    await (await control(dialog(page), "Upstream")).click();
    await shot(page, `s7-${label}-combobox-open`, { settle: 450, fullPage: false });
    await page.keyboard.press("Escape");
    await expect(page.getByRole("option")).toHaveCount(0);
    await page.keyboard.press("Escape");
    await expect(dialog(page)).toHaveCount(0);

    // The delete confirmation (an alias nothing refers to), and the 409 (a model an alias still points at).
    await page.goto("/aliases");
    await row(page, "s7-alias").getByRole("button", { name: "Delete" }).click();
    await expect(confirmDialog(page)).toBeVisible();
    await shot(page, `s7-${label}-confirm`, { settle: 450, fullPage: false });
    await page.keyboard.press("Escape");
    await expect(confirmDialog(page)).toHaveCount(0);

    await page.goto("/models");
    await page.getByLabel("Search models").fill("s7/m");
    await row(page, "s7/m").getByRole("button", { name: "Delete" }).click();
    await confirmDialog(page).getByRole("button", { name: "Delete" }).click();
    await expect(confirmDialog(page).getByTestId("delete-blocked")).toBeVisible();
    await shot(page, `s7-${label}-conflict-409`, { settle: 450, fullPage: false });
    await confirmDialog(page).getByRole("button", { name: "Got it" }).click();
    await expect(confirmDialog(page)).toHaveCount(0);
  }

  // The matrix, exactly. 15 pages: the 13 whole pages here, the team detail (its five tabs), and login. Plus the
  // two catalog forms, the picker, both open dropdowns, the confirmation and the 409, and the usage chart in each
  // of its four states, each in four modes.
  const expected = MODES.flatMap(([label]) => [...PAGES.map(([name]) => `s7-${label}-page-${name}`), ...TABS.map((tab) => `s7-${label}-team-${slug(tab)}`), ...SPECIALS.map((name) => `s7-${label}-${name}`), ...CHARTS.map((name) => `s7-${label}-${name}`)]).map((n) => `${n}.png`);
  expect(expected).toHaveLength(120);
  expect(new Set(expected).size).toBe(expected.length);
  const found = readdirSync(SHOTS!).filter((name) => name.startsWith("s7-") && name.endsWith(".png"));
  expect(found.sort()).toEqual(expected.sort());
});
