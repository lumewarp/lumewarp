import { expect, test, type Page } from "@playwright/test";

import { dialog } from "./helpers";
import { Admin, signIn, VIEWER, VIEWER_EMAIL } from "./selfService";

/*
 * Keys, limits, usage and the Playground per zone. A second zone is declared by serve.py; the viewer is a plain
 * member of the teams below, the break-glass admin edits limits. The zone stays (it holds a key by then).
 */

const ZONE = "zk-b";
const LABEL = "Zone B";

let admin: Admin;

test.beforeEach(async ({ request }) => {
  admin = await Admin.login(request);
  // zk-b is declared by serve.py; give it the rules this spec needs (idempotent).
  await admin.patch(`/zones/${ZONE}`, {
    name: LABEL,
    color: "violet",
    key_min_creator_role: "member",
    key_max_lifetime_seconds: 7 * 86_400,
    key_expiry_required: false,
    key_max_per_member: null,
    service_account_keys_allowed: true,
    playground_allowed: false,
    playground_gateway_url: null,
  });
});

/** The header zone of this tab, set before the app loads. */
async function inZone(page: Page, zone: string) {
  await page.addInitScript((name) => sessionStorage.setItem("lumewarp.zone", name), zone);
}

/** The page signed in as the break-glass admin. */
async function adminPage(page: Page) {
  const response = await page.request.post("/api/v1/session/break-glass", { data: { password: "e2e-break-glass-password" } });
  expect(response.status()).toBe(204);
}

test("a key created in zone B names the zone, shows its rules and is listed only there", async ({ page }) => {
  const team = await admin.ensureTeam("zk-keys");
  await admin.putMember(team.id, VIEWER_EMAIL);
  await inZone(page, ZONE);
  await signIn(page, VIEWER, "/my-keys");
  await expect(page.getByTestId("app-header")).toHaveAttribute("data-zone", ZONE);

  await page.getByTestId("team-zk-keys").getByRole("button", { name: "New key" }).click();
  const form = page.getByRole("dialog", { name: `New key in zk-keys (${LABEL})` });
  await expect(form).toBeVisible();
  await expect(form.getByTestId("zone-key-rules")).toContainText(`Keys in ${LABEL} live at most 7 days.`);
  await form.getByLabel("Name", { exact: true }).fill("zk-key-b");
  await form.getByRole("button", { name: "Create key" }).click();
  await expect(page.getByRole("dialog", { name: "API key created" })).toBeVisible();
  await page.getByRole("button", { name: /saved it/ }).click();

  // Listed in zone B, with the zone on its pill tooltip; absent in the default zone.
  const keys = page.getByTestId("my-keys");
  await expect(keys.getByText("zk-key-b")).toBeVisible();
  const mine = await admin.get<{ items: { name: string; zone: string }[] }>(`/teams/${team.id}/keys?limit=200`);
  expect(mine.items.find((key) => key.name === "zk-key-b")?.zone).toBe(ZONE);

  await page.getByTestId("zone-switcher").click();
  await page.getByRole("menuitemradio", { name: /default/i }).first().click();
  await expect(page.getByTestId("app-header")).toHaveAttribute("data-zone", "default");
  await expect(page.getByTestId("my-keys").getByText("zk-key-b")).toHaveCount(0);
});

test("a rule the zone sets blocks creation with the reason", async ({ page, request }) => {
  const team = await admin.ensureTeam("zk-role");
  await admin.putMember(team.id, VIEWER_EMAIL);
  const patched = await request.patch(`/api/v1/zones/${ZONE}`, { headers: admin.headers, data: { key_min_creator_role: "team_admin" } });
  expect(patched.status(), await patched.text()).toBe(200);
  try {
    await inZone(page, ZONE);
    await signIn(page, VIEWER, "/my-keys");
    await page.getByTestId("team-zk-role").getByRole("button", { name: "New key" }).click();
    const form = page.getByRole("dialog", { name: /New key in zk-role/ });
    await expect(form.getByTestId("key-blocked")).toContainText(`Keys in ${LABEL} can only be created by a team admin or above.`);
    await expect(form.getByRole("button", { name: "Create key" })).toBeDisabled();
  } finally {
    await request.patch(`/api/v1/zones/${ZONE}`, { headers: admin.headers, data: { key_min_creator_role: "member" } });
  }
});

test("team limits are edited per zone: a value set in the default zone does not appear in zone B", async ({ page }) => {
  const team = await admin.ensureTeam("zk-limits");
  await adminPage(page);

  await page.goto(`/teams/${team.id}`);
  await page.getByRole("tab", { name: "Settings" }).click();
  await page.getByRole("button", { name: "Edit team" }).click();
  const form = dialog(page);
  await expect(form.getByTestId("limits-heading")).toContainText("Limits in default");
  await form.getByLabel("Requests per minute", { exact: true }).fill("11");
  await form.getByRole("button", { name: "Save" }).click();
  await expect(form).toHaveCount(0);

  const after = await admin.get<{ limits_by_zone: Record<string, { rpm_limit?: number | null }> }>(`/teams/${team.id}`);
  expect(after.limits_by_zone.default?.rpm_limit).toBe(11);
  expect(after.limits_by_zone[ZONE]?.rpm_limit ?? null).toBeNull();

  // Zone B shows its own (empty) limits, and its heading names it.
  await page.getByTestId("zone-switcher").click();
  await page.getByRole("menuitemradio", { name: new RegExp(LABEL) }).click();
  await page.getByRole("button", { name: "Edit team" }).click();
  await expect(dialog(page).getByTestId("limits-heading")).toContainText(`Limits in ${LABEL}`);
  await expect(dialog(page).getByLabel("Requests per minute", { exact: true })).toHaveValue("");
});

test("usage asks for the header zone, and All zones drops the filter and groups rows by zone", async ({ page }) => {
  await adminPage(page);
  await inZone(page, ZONE);
  const zones: (string | null)[] = [];
  const groups: (string | null)[] = [];
  await page.route("**/api/v1/usage?*", (route) => {
    const url = new URL(route.request().url());
    if (!url.pathname.endsWith("/leaderboard")) {
      zones.push(url.searchParams.get("zone"));
      groups.push(url.searchParams.get("group_by"));
    }
    return route.continue();
  });
  await page.goto("/usage");
  await expect(page.getByRole("heading", { name: "Usage", level: 1 })).toBeVisible();
  await expect.poll(() => zones.at(-1)).toBe(ZONE);

  const filters = page.getByTestId("usage-filters");
  await filters.getByRole("combobox", { name: "Zone", exact: true }).click();
  await page.getByRole("option", { name: "All zones" }).click();
  await expect.poll(() => zones.at(-1)).toBeNull();
  // Each row then names its zone.
  await expect.poll(() => groups.some((group) => group?.startsWith("zone,"))).toBe(true);
});

test("the Playground says that zone B does not allow it", async ({ page }) => {
  await inZone(page, ZONE);
  await signIn(page, VIEWER, "/playground");
  await expect(page.getByTestId("playground-zone-unavailable")).toContainText(`${LABEL} does not allow the Playground.`);
});
