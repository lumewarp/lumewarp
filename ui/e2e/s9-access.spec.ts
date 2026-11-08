import { expect, test } from "@playwright/test";

import { confirmDialog, dialog } from "./helpers";
import {
  Admin,
  MEMBER_EMAIL,
  memberKey,
  OUTSIDER,
  OUTSIDER_EMAIL,
  rowOf,
  SHOTS_EMAIL,
  SHOTS_TOKEN,
  signIn,
  TEAM_ADMIN,
  TEAM_ADMIN_EMAIL,
  VIEWER,
  VIEWER_EMAIL,
} from "./selfService";

/*
 * Access checks that depend on `/users/me`: a guarded route revalidates on entry instead of trusting
 * the five-minute cache, a refused read revalidates once, and the team route is guarded per team.
 */

let admin: Admin;

test.beforeEach(async ({ request }) => {
  admin = await Admin.login(request);
});

test("a cached allow is revalidated and replaced when the role is taken away", async ({ page }) => {
  const team = await admin.ensureTeam("s9-allow");
  // The outsider has no other admin role, so demoting them really takes /teams away. An
  // installation admin does this: a team admin cannot demote another admin.
  await admin.putMember(team.id, OUTSIDER_EMAIL, "admin");
  await admin.putMember(team.id, MEMBER_EMAIL, "admin");
  await signIn(page, OUTSIDER, "/my-keys");
  await expect(page.getByRole("link", { name: "Teams", exact: true })).toBeVisible();

  // The role changes while the SPA is open; the cached me still says team admin.
  await admin.putMember(team.id, OUTSIDER_EMAIL, "member");
  await page.route("**/api/v1/users/me", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    await route.continue();
  });
  await page.getByRole("link", { name: "Teams", exact: true }).click();
  // The cached allow mounts the page first; the background check then takes it away.
  await expect(page.getByRole("heading", { name: "Teams", level: 1 })).toBeVisible();
  await expect(page.getByTestId("no-access")).toBeVisible();
  await expect(page.getByRole("heading", { name: "You do not have access to this page" })).toBeVisible();
});

test("a cached deny waits for the check, then mounts when the role is granted", async ({ page }) => {
  const team = await admin.ensureTeam("s9-grant");
  await admin.putMember(team.id, SHOTS_EMAIL);
  await signIn(page, SHOTS_TOKEN, "/my-keys");
  await expect(page.getByRole("link", { name: "Teams", exact: true })).toHaveCount(0);

  // The role changes while the SPA is open; the cached me still denies /teams.
  await admin.putMember(team.id, SHOTS_EMAIL, "admin");
  await page.route("**/api/v1/users/me", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    await route.continue();
  });
  await page.evaluate(() => {
    history.pushState({}, "", "/teams");
    window.dispatchEvent(new PopStateEvent("popstate", { state: {} }));
  });
  await expect(page.getByText("Checking access…")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Teams", level: 1 })).toBeVisible();
});

test("a 403 from a permission-dependent read revalidates /users/me", async ({ page }) => {
  let meRequests = 0;
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/v1/users/me") meRequests++;
  });
  await signIn(page, TEAM_ADMIN, "/usage");
  await expect.poll(() => meRequests).toBe(1);
  await page.route("**/api/v1/usage?*", (route) =>
    route.fulfill({
      status: 403,
      contentType: "application/json",
      body: JSON.stringify({ error: { code: "forbidden", message: "not allowed" } }),
    }),
  );
  await page.getByLabel("From").fill("2026-01-01");
  await expect.poll(() => meRequests, { timeout: 15_000 }).toBeGreaterThan(1);
});

test("team detail: an own team is readable and its back link is My keys for a plain member", async ({ page }) => {
  const own = await admin.ensureTeam("s9-own");
  await admin.putMember(own.id, OUTSIDER_EMAIL);
  const forbidden: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (/\/members$|\/service-accounts(\/|$)|\/models$|\/upstreams$|\/routers$|\/plugins$|\/audit$/.test(path)) forbidden.push(path);
  });
  await signIn(page, OUTSIDER, `/teams/${own.id}`);
  await expect(page.getByRole("heading", { name: own.name, level: 1 })).toBeVisible();
  // The team-admin sections are not mounted at all for a plain member.
  await expect(page.getByRole("tab", { name: "Members", exact: true })).toHaveCount(0);
  await expect(page.getByRole("tab", { name: "Service accounts", exact: true })).toHaveCount(0);
  for (const name of ["Keys", "Usage", "Settings"]) {
    await page.getByRole("tab", { name, exact: true }).click();
    await expect(page.getByRole("tabpanel")).toBeVisible();
  }
  // Settings are read-only and the team's own policy is not sent to a plain member.
  await expect(page.getByRole("button", { name: "Edit team" })).toHaveCount(0);
  await expect(page.getByRole("tabpanel").getByText("Policy", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("tabpanel").getByText("Keys per member", { exact: true })).toBeVisible();
  await page.getByRole("main").getByRole("link", { name: "My keys", exact: true }).click();
  await expect(page).toHaveURL(/\/my-keys$/);
  expect(forbidden).toEqual([]);
});

test("members: a team admin cannot hand out or take away the admin role", async ({ page }) => {
  const team = await admin.ensureTeam("s9-roles");
  await admin.putMember(team.id, TEAM_ADMIN_EMAIL, "admin");
  await admin.putMember(team.id, MEMBER_EMAIL, "admin");
  await admin.putMember(team.id, VIEWER_EMAIL);
  await signIn(page, TEAM_ADMIN, `/teams/${team.id}`);
  await page.getByRole("tab", { name: "Members", exact: true }).click();

  // The Add member dialog offers no role at all; the server would refuse an admin anyway.
  await page.getByRole("button", { name: "Add member" }).click();
  await expect(dialog(page).getByRole("combobox", { name: "Role", exact: true })).toHaveCount(0);
  await expect(dialog(page).getByText(/only an org admin can assign a team admin/i)).toBeVisible();
  await dialog(page).getByRole("button", { name: "Cancel" }).click();

  // An admin row cannot be removed by a team admin; a plain member row can.
  await expect(rowOf(page, MEMBER_EMAIL).getByRole("button", { name: "Delete", exact: true })).toBeDisabled();
  await expect(rowOf(page, VIEWER_EMAIL).getByRole("button", { name: "Delete", exact: true })).toBeEnabled();
});

test("usage: an admin of one team never requests a member team's directory", async ({ page }) => {
  const adminTeam = await admin.ensureTeam("s9-mixed-admin");
  const memberTeam = await admin.ensureTeam("s9-mixed-member");
  await admin.putMember(adminTeam.id, TEAM_ADMIN_EMAIL, "admin");
  await admin.putMember(memberTeam.id, TEAM_ADMIN_EMAIL, "member");
  const requests: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (/\/members$|\/service-accounts(\/|$)/.test(path)) requests.push(path);
  });
  await signIn(page, TEAM_ADMIN, "/usage");
  await expect(page.getByRole("heading", { name: "Usage", level: 1 })).toBeVisible();
  // The directory for the administered team is loaded; the member-only team is never asked.
  await expect.poll(() => requests.includes(`/api/v1/teams/${adminTeam.id}/members`)).toBe(true);
  expect(requests).not.toContain(`/api/v1/teams/${memberTeam.id}/members`);
  expect(requests).not.toContain(`/api/v1/teams/${memberTeam.id}/service-accounts`);
  await expect(page.getByTestId("state-error")).toHaveCount(0);
});


test("team settings: a team admin edits name and key limits but not the quota", async ({ page }) => {
  const team = await admin.ensureTeam("s9-settings", { max_keys_per_member: 2 });
  await admin.patch<{ id: string }>(`/teams/${team.id}`, { rpm_limit: 10, budget_nano_usd: 5000 });
  await admin.putMember(team.id, TEAM_ADMIN_EMAIL, "admin");
  await signIn(page, TEAM_ADMIN, `/teams/${team.id}`);
  await page.getByRole("tab", { name: "Settings", exact: true }).click();
  // The quota is visible read-only, with no edit control of its own.
  await expect(page.getByRole("tabpanel")).toContainText("10 rpm");
  await page.getByRole("button", { name: "Edit team" }).click();
  const d = dialog(page);
  await expect(d.getByLabel("Requests per minute", { exact: true })).toHaveCount(0);
  await expect(d.getByLabel("Monthly budget (USD)")).toHaveCount(0);
  await expect(d.getByLabel("Max keys per member")).toBeVisible();
  const renamed = `s9-settings-${Date.now()}`;
  await d.getByLabel("Name").fill(renamed);
  await d.getByLabel("Max keys per member").fill("3");
  const patched = page.waitForRequest((request) => request.method() === "PATCH" && new URL(request.url()).pathname === `/api/v1/teams/${team.id}`);
  await d.getByRole("button", { name: "Save" }).click();
  const body = (await patched).postDataJSON() as Record<string, unknown>;
  expect(body).not.toHaveProperty("rpm_limit");
  expect(body).not.toHaveProperty("model_access");
  expect(body).not.toHaveProperty("budget_nano_usd");
  expect(body).toMatchObject({ name: renamed, max_keys_per_member: 3 });
  await expect(d).toHaveCount(0);
  await expect(page.getByRole("heading", { name: renamed, level: 1 })).toBeVisible();
});

test("team detail: a foreign team shows no-access and mounts no panels", async ({ page }) => {
  const foreign = await admin.ensureTeam("s9-foreign");
  await admin.putMember(foreign.id, "member@e2e.test");
  const teamRequests: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === `/api/v1/teams/${foreign.id}`) teamRequests.push(request.url());
  });
  await signIn(page, OUTSIDER, `/teams/${foreign.id}`);
  await expect(page.getByTestId("no-access")).toBeVisible();
  await expect(page.getByRole("heading", { name: "You do not have access to this page" })).toBeVisible();
  await expect(page.getByTestId("state-error")).toHaveCount(0);
  await expect(page.getByRole("tab")).toHaveCount(0);
  expect(teamRequests).toHaveLength(0);
});

test("team detail: an installation admin reads every team", async ({ page, request }) => {
  const foreign = await admin.ensureTeam("s9-foreign");
  await page.context().addCookies((await request.storageState()).cookies);
  await page.goto(`/teams/${foreign.id}`);
  await expect(page.getByRole("heading", { name: foreign.name, level: 1 })).toBeVisible();
  await expect(page.getByRole("link", { name: "All teams", exact: true })).toBeVisible();
});

test("team detail: a team admin's back link is All teams", async ({ page }) => {
  const team = await admin.ensureTeam("s9-own");
  await admin.putMember(team.id, TEAM_ADMIN_EMAIL, "admin");
  await signIn(page, TEAM_ADMIN, `/teams/${team.id}`);
  await expect(page.getByRole("heading", { name: team.name, level: 1 })).toBeVisible();
  await expect(page.getByRole("link", { name: "All teams", exact: true })).toBeVisible();
});

test("the team key panel still rotates and revokes another member's key", async ({ page, browser }) => {
  const team = await admin.ensureTeam("s9-panel");
  await admin.putMember(team.id, TEAM_ADMIN_EMAIL, "admin");
  await admin.putMember(team.id, VIEWER_EMAIL);
  const created = await memberKey(browser, VIEWER, team.id, "s9-other-key");
  await signIn(page, TEAM_ADMIN, `/teams/${team.id}`);
  await page.getByRole("tab", { name: "Keys" }).click();
  const row = rowOf(page, "s9-other-key");
  await expect(row).toBeVisible();
  await expect(row.locator("code")).toHaveText(`${created.key.display_prefix}…`);
  await row.getByRole("button", { name: "Rotate" }).click();
  const secret = page.getByRole("dialog", { name: "API key rotated" });
  await expect(secret.getByTestId("secret-value")).toHaveText(/^lw-/);
  await secret.getByRole("button", { name: /saved it/ }).click();
  await expect(page.getByTestId("secret-value")).toHaveCount(0);
  await row.getByRole("button", { name: "Revoke" }).click();
  await confirmDialog(page).getByRole("button", { name: "Revoke" }).click();
  await expect(row.getByText("Revoked", { exact: true })).toBeVisible();
});
