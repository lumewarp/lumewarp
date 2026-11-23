import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

/*
 * The insight cards draw a thin area line of their metric over the range, from the page's own
 * `group_by=day` query. p95 and fallback rate need one row per day, so they are drawn only for one zone.
 */

const BREAK_GLASS = "e2e-break-glass-password";
const isoDay = (date: Date) => date.toISOString().slice(0, 10);
const daysAgo = (n: number) => isoDay(new Date(Date.now() - n * 86_400_000));

async function signInAsAdmin(page: Page, request: APIRequestContext) {
  const response = await request.post("/api/v1/session/break-glass", { data: { password: BREAK_GLASS } });
  expect(response.status()).toBe(204);
  await page.context().addCookies((await request.storageState()).cookies);
}

const row = (day: string, zone: string, requests: number) => ({
  zone,
  date: day,
  requests,
  prompt_tokens: requests * 10,
  completion_tokens: requests * 10,
  total_tokens: requests * 20,
  cost_nano_usd: requests * 1_000_000,
  unknown_cost_count: 0,
  error_count: 1,
  fallback_count: 0,
  retried_count: 0,
  audio_ms: 0,
  total_duration_ms: 0,
  p95_duration_ms: 300,
});

/** Answer the per-day trend query (`group_by=day` exactly) with these rows. */
async function mockTrend(page: Page, items: Record<string, unknown>[]) {
  await page.route("**/api/v1/usage?*", async (route) => {
    if (new URL(route.request().url()).searchParams.get("group_by") !== "day") return route.continue();
    await route.fulfill({ status: 200, json: { items, next_cursor: null } });
  });
}

const sparkline = (page: Page, tile: string) => page.getByTestId(`insight-${tile}`).locator("[data-testid=insight-sparkline] .recharts-area");

test.use({ timezoneId: "UTC" });

test("insight cards draw a sparkline of the range", async ({ page, request }) => {
  await signInAsAdmin(page, request);
  await mockTrend(page, [row(daysAgo(2), "dev", 10), row(daysAgo(1), "dev", 30), row(daysAgo(0), "dev", 20)]);
  await page.goto("/usage");
  await expect(page.getByTestId("insight-requests")).toBeVisible();
  await expect(sparkline(page, "requests")).toHaveCount(1);
  await expect(sparkline(page, "tokens")).toHaveCount(1);
  await expect(sparkline(page, "errors")).toHaveCount(1);
  await expect(page.getByTestId("insight-requests").getByTestId("insight-sparkline")).toHaveAttribute("aria-hidden", "true");
  // One zone in view: p95 has one row per day, so it is drawn too.
  await expect(sparkline(page, "p95")).toHaveCount(1);
});

test("with several zones p95 has no sparkline but requests does", async ({ page, request }) => {
  await signInAsAdmin(page, request);
  const today = daysAgo(0);
  await mockTrend(page, [row(today, "dev", 10), row(today, "prod", 20)]);
  await page.goto("/usage");
  await page.getByTestId("usage-filters").getByRole("combobox", { name: "Zone", exact: true }).click();
  await page.getByRole("option", { name: "All zones", exact: true }).click();
  await expect(page.getByTestId("insight-p95")).toBeVisible();
  await expect(sparkline(page, "requests")).toHaveCount(1);
  await expect(page.getByTestId("insight-p95").getByTestId("insight-sparkline")).toHaveCount(0);
  await expect(page.getByTestId("insight-fallback").getByTestId("insight-sparkline")).toHaveCount(0);
});
