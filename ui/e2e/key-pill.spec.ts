import { expect, test } from "@playwright/test";

import { expectKeyPill } from "./helpers";

/* Usage grouped by key and the audit log render a key as a pill; a deleted key is a muted "Deleted key" pill. */

const BREAK_GLASS = "e2e-break-glass-password";
const KEY_ID = "22222222-2222-4222-8222-222222222222";
const GONE_ID = "33333333-3333-4333-8333-333333333333";
const key = {
  id: KEY_ID,
  name: "batch-runner",
  display_prefix: "lw-9a8b7c",
  team_id: "44444444-4444-4444-8444-444444444444",
  team_name: "Search",
  org_id: null,
  org_name: null,
  owner_kind: "user",
  user_id: null,
  service_account_id: null,
  service_account_name: null,
  lifecycle: "active",
};

test.beforeEach(async ({ page, request }) => {
  const response = await request.post("/api/v1/session/break-glass", { data: { password: BREAK_GLASS } });
  expect(response.status()).toBe(204);
  await page.context().addCookies((await request.storageState()).cookies);
});

test("usage grouped by key shows key pills, and a deleted key says so", async ({ page }) => {
  const usage = (key_id: string, k: typeof key | null) => ({
    key_id, key: k, requests: 3, prompt_tokens: 10, completion_tokens: 5, total_tokens: 15, cost_nano_usd: 1000,
  });
  await page.route("**/api/v1/usage?**", async (route) => {
    const q = new URL(route.request().url()).searchParams;
    if (q.get("group_by") === "key") await route.fulfill({ json: { items: [usage(KEY_ID, key), usage(GONE_ID, null)], next_cursor: null } });
    else await route.fallback();
  });
  await page.goto("/usage");
  await expect(page.getByRole("heading", { name: "Usage", level: 1 })).toBeVisible();
  await page.getByRole("combobox", { name: "Group by" }).first().click();
  await page.getByRole("option", { name: /API key/ }).click();
  await expect(page.getByRole("listbox")).toHaveCount(0);
  await page.getByRole("heading", { name: "Usage", level: 1 }).click();
  await expect(page.getByRole("combobox", { name: "Group by" }).first()).toContainText("API key");
  await expectKeyPill(page, page.getByRole("table").first(), "batch-runner", ["Search", "lw-9a8b7c…", KEY_ID]);
  await expectKeyPill(page, page.getByRole("table").first(), "Deleted key", [GONE_ID]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test("audit shows a key resource as a pill and other resources as before", async ({ page }) => {
  const entry = (id: number, resource_type: string, resource_id: string, k: typeof key | null) => ({
    id, ts: new Date().toISOString(), team_id: null, org_id: null, actor_type: "user", actor_id: GONE_ID,
    action: "key.create", resource_type, resource_id, details: {}, key: k,
  });
  await page.route("**/api/v1/audit?**", (route) =>
    route.fulfill({ json: { items: [entry(2, "key", KEY_ID, key), entry(1, "team", GONE_ID, null)], next_cursor: null } }),
  );
  await page.goto("/audit");
  await expectKeyPill(page, page.getByRole("table"), "batch-runner", ["Search", "lw-9a8b7c…", KEY_ID]);
  await expect(page.getByRole("cell", { name: /^team 33333333/ })).toBeVisible();
});
