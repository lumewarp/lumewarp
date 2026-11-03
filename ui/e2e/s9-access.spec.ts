import { expect, test } from "@playwright/test";

import { confirmDialog } from "./helpers";
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
  // The outsider has no other admin role, so demoting them really takes /teams away.
  await admin.putMember(team.id, OUTSIDER_EMAIL, "admin");
  // A second admin so the demotion below is allowed (a team must keep at least one admin).
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
  await expect.poll(() => meRequests).toBeGreaterThan(1);
});

test("team detail: an own team is readable and its back link is My keys for a plain member", async ({ page }) => {
  const own = await admin.ensureTeam("s9-own");
  await admin.putMember(own.id, OUTSIDER_EMAIL);
  await signIn(page, OUTSIDER, `/teams/${own.id}`);
  await expect(page.getByRole("heading", { name: own.name, level: 1 })).toBeVisible();
  await page.getByRole("main").getByRole("link", { name: "My keys", exact: true }).click();
  await expect(page).toHaveURL(/\/my-keys$/);
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
