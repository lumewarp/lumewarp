import { expect, test } from "@playwright/test";

import { confirmDialog, pick, SHOTS, shot } from "./helpers";
import {
  Admin,
  daysFromNow,
  isoDay,
  MEMBER,
  MEMBER_EMAIL,
  memberKey,
  memberRotate,
  memberSession,
  memberUpdate,
  OUTSIDER,
  rowOf,
  routeChange,
  SHOTS_EMAIL,
  SHOTS_TOKEN,
  myKeyRow,
  teamDetails,
  signIn,
  TEAM_ADMIN,
  TEAM_ADMIN_EMAIL,
  VIEWER,
  VIEWER_EMAIL,
  WEEK,
} from "./selfService";

/*
 * Self-service keys as a regular member against the real control plane. serve.py seeds the users and
 * their cookie sessions (a login here is OIDC or break-glass, so a spec cannot sign a member in any
 * other way); every team, membership and key is made through the API in the test that needs it.
 *
 * The viewer is a plain member, `member` is a second plain member of the same team, `teamadmin` is an
 * admin of one team only, and `outsider` is on no team at all.
 */

let admin: Admin;

test.beforeEach(async ({ request }) => {
  admin = await Admin.login(request);
});

test("a regular member lands on My keys and sees only their teams and own keys", async ({ page, browser }) => {
  const mine = await admin.ensureTeam("s8-mine");
  const elsewhere = await admin.ensureTeam("s8-elsewhere");
  await admin.putMember(mine.id, VIEWER_EMAIL);
  await admin.putMember(mine.id, MEMBER_EMAIL);
  await admin.putMember(elsewhere.id, MEMBER_EMAIL);
  await memberKey(browser, VIEWER, mine.id, "s8-viewer-key");
  await memberKey(browser, MEMBER, mine.id, "s8-other-key");
  await memberKey(browser, MEMBER, elsewhere.id, "s8-hidden-key");

  await signIn(page, VIEWER, "/");
  await expect(page).toHaveURL(/\/my-keys$/);
  await expect(page.getByRole("heading", { name: "My keys", level: 1 })).toBeVisible();
  await expect((await myKeyRow(page, "s8-mine", "s8-viewer-key"))).toBeVisible();
  await expect(page.getByTestId("my-keys")).not.toContainText("s8-other-key");
  await expect(page.getByTestId("team-s8-elsewhere")).toHaveCount(0);
  await expect(page.getByText("s8-hidden-key")).toHaveCount(0);
  // The navigation shows the self-service page first and no admin page for a plain member.
  const nav = page.getByRole("navigation", { name: "Main" });
  await expect(nav.getByRole("link").first()).toHaveText("My keys");
  await expect(nav.getByRole("link", { name: "Teams", exact: true })).toHaveCount(0);
  await expect(nav.getByRole("link", { name: "Keys", exact: true })).toHaveCount(0);
  await expect(nav.getByRole("link", { name: "Users", exact: true })).toHaveCount(0);

  // The home entry was replaced: Back does not land on / (and bounce forward again).
  await page.goBack();
  await expect(page).not.toHaveURL(/\/my-keys$/);
  await page.goForward();
  await expect(page).toHaveURL(/\/my-keys$/);
});

test("create with expiry: the secret is shown once and gone after closing", async ({ page }) => {
  const team = await admin.ensureTeam("s8-keys");
  await admin.putMember(team.id, VIEWER_EMAIL);
  await signIn(page, VIEWER);

  const section = page.getByTestId("team-s8-keys");
  await section.getByRole("button", { name: "New key" }).click();
  const form = page.getByRole("dialog", { name: "New key in s8-keys" });
  await form.getByLabel("Name").fill("s8-created");
  await form.getByLabel("Expires").fill(isoDay(daysFromNow(2)));
  await form.getByRole("button", { name: "Create key" }).click();

  const secret = page.getByRole("dialog", { name: "API key created" });
  await expect(secret.getByTestId("secret-value")).toHaveText(/^lw-/);
  const value = await secret.getByTestId("secret-value").textContent();
  await expect(secret.getByText(/will never be shown again/i)).toBeVisible();
  await expect(secret.getByRole("button", { name: "Copy" })).toBeVisible();
  await secret.getByRole("button", { name: /saved it/ }).click();
  await expect(secret).toBeHidden();
  await expect(page.getByTestId("secret-value")).toHaveCount(0);
  expect(await page.content()).not.toContain(value!);
  await expect((await myKeyRow(page, "s8-keys", "s8-created"))).toBeVisible();
});

test("the create dialog refuses an expiry past the team's limit with the API's message", async ({ page }) => {
  const team = await admin.ensureTeam("s8-limit", { max_member_key_lifetime_seconds: WEEK });
  await admin.putMember(team.id, VIEWER_EMAIL);
  await signIn(page, VIEWER);

  const section = await teamDetails(page, "s8-limit");
  await expect(section).toContainText("Longest lifetime: 7 days");
  await section.getByRole("button", { name: "New key" }).click();
  const form = page.getByRole("dialog", { name: "New key in s8-limit" });
  await form.getByLabel("Name").fill("s8-too-long");
  await form.getByLabel("Expires").fill(isoDay(daysFromNow(30)));
  await form.getByRole("button", { name: "Create key" }).click();
  await expect(form.getByText(/member keys may not outlive 604800 seconds/)).toBeVisible();
  await expect(form).toBeVisible();
  await form.getByRole("button", { name: "Cancel" }).click();
});

test.describe("expiry max", () => {
  test.describe("negative UTC offset across a DST change", () => {
    test.use({ timezoneId: "America/Los_Angeles" });
    test("the max is the last day whose end is not after the deadline", async ({ page }) => {
      const team = await admin.ensureTeam("s8-maxday", { max_member_key_lifetime_seconds: WEEK });
      await admin.putMember(team.id, VIEWER_EMAIL);
      const fixed = new Date("2027-03-07T23:30:00-08:00");
      await page.clock.install({ time: fixed });
      await signIn(page, VIEWER);
      await page.getByTestId("team-s8-maxday").getByRole("button", { name: "New key" }).click();
      const form = page.getByRole("dialog", { name: "New key in s8-maxday" });
      const max = await form.getByLabel("Expires").getAttribute("max");
      expect(max).toBeTruthy();
      const bounds = await page.evaluate(
        ({ max, now, lifetime }) => {
          const endOf = (day: string) => new Date(`${day}T23:59:59`).getTime();
          const noon = new Date(`${max}T12:00:00`);
          noon.setDate(noon.getDate() + 1);
          const next = `${noon.getFullYear()}-${String(noon.getMonth() + 1).padStart(2, "0")}-${String(noon.getDate()).padStart(2, "0")}`;
          return { endMax: endOf(max), deadline: new Date(now).getTime() + lifetime * 1000, endNext: endOf(next) };
        },
        { max: max!, now: fixed.toISOString(), lifetime: WEEK },
      );
      expect(bounds.endMax).toBeLessThanOrEqual(bounds.deadline);
      expect(bounds.endNext).toBeGreaterThan(bounds.deadline);
      // The DST change is why the local date is not "six days later" (2027-03-13).
      expect(max).toBe("2027-03-14");
    });
  });

  test.describe("positive UTC offset", () => {
    test.use({ timezoneId: "Asia/Taipei" });
    test("the max is the last day whose end is not after the deadline", async ({ page }) => {
      const team = await admin.ensureTeam("s8-maxday", { max_member_key_lifetime_seconds: WEEK });
      await admin.putMember(team.id, VIEWER_EMAIL);
      const fixed = new Date("2027-06-01T12:00:00+08:00");
      await page.clock.install({ time: fixed });
      await signIn(page, VIEWER);
      await page.getByTestId("team-s8-maxday").getByRole("button", { name: "New key" }).click();
      const form = page.getByRole("dialog", { name: "New key in s8-maxday" });
      const max = await form.getByLabel("Expires").getAttribute("max");
      expect(max).toBeTruthy();
      const bounds = await page.evaluate(
        ({ max, now, lifetime }) => {
          const endOf = (day: string) => new Date(`${day}T23:59:59`).getTime();
          const noon = new Date(`${max}T12:00:00`);
          noon.setDate(noon.getDate() + 1);
          const next = `${noon.getFullYear()}-${String(noon.getMonth() + 1).padStart(2, "0")}-${String(noon.getDate()).padStart(2, "0")}`;
          return { endMax: endOf(max), deadline: new Date(now).getTime() + lifetime * 1000, endNext: endOf(next) };
        },
        { max: max!, now: fixed.toISOString(), lifetime: WEEK },
      );
      expect(bounds.endMax).toBeLessThanOrEqual(bounds.deadline);
      expect(bounds.endNext).toBeGreaterThan(bounds.deadline);
      expect(max).toBe("2027-06-07");
    });
  });
});

test("rotate with a one hour grace, then with no grace", async ({ page, browser }) => {
  const team = await admin.ensureTeam("s8-keys");
  await admin.putMember(team.id, VIEWER_EMAIL);
  const created = await memberKey(browser, VIEWER, team.id, "s8-rotate-key");
  await signIn(page, VIEWER);

  const row = (await myKeyRow(page, "s8-keys", "s8-rotate-key"));
  await expect(row.locator("code")).toHaveText(`${created.key.display_prefix}…`);

  await row.getByRole("button", { name: "Rotate" }).click();
  const first = page.getByRole("dialog", { name: /^Rotate/ });
  await expect(first.getByTestId("rotate-notice")).toHaveText("The old secret stops working right away.");
  await pick(first, "Old secret", "1 hour");
  await expect(first.getByTestId("rotate-notice")).toContainText(/keeps working until/i);
  await first.getByRole("button", { name: "Rotate key" }).click();

  const rotated = page.getByRole("dialog", { name: "API key rotated" });
  await expect(rotated.getByTestId("secret-value")).toHaveText(/^lw-/);
  await expect(rotated.getByTestId("secret-note")).toContainText(/previous secret works until/i);
  await rotated.getByRole("button", { name: /saved it/ }).click();
  await expect(row.getByTestId("grace")).toContainText(/Old secret works until/i);

  // With no grace the old secret stops at once and the row no longer claims a grace.
  await row.getByRole("button", { name: "Rotate" }).click();
  const second = page.getByRole("dialog", { name: /^Rotate/ });
  await expect(second.getByTestId("rotate-notice")).toContainText("The old secret stops working right away.");
  await expect(second.getByTestId("rotate-notice")).toContainText(/already has a previous secret in grace until/i);
  await second.getByRole("button", { name: "Rotate key" }).click();
  const rotatedAgain = page.getByRole("dialog", { name: "API key rotated" });
  await expect(rotatedAgain.getByTestId("secret-note")).toHaveText("The previous secret stopped working immediately.");
  await rotatedAgain.getByRole("button", { name: /saved it/ }).click();
  await expect(row.getByTestId("grace")).toHaveCount(0);
});

test("rotating during a grace ends the older one immediately", async ({ page, browser }) => {
  const team = await admin.ensureTeam("s8-grace2");
  await admin.putMember(team.id, VIEWER_EMAIL);
  await memberKey(browser, VIEWER, team.id, "s8-grace-key");
  await signIn(page, VIEWER);

  const row = (await myKeyRow(page, "s8-grace2", "s8-grace-key"));
  await row.getByRole("button", { name: "Rotate" }).click();
  let dialog = page.getByRole("dialog", { name: /^Rotate/ });
  await pick(dialog, "Old secret", "24 hours");
  await dialog.getByRole("button", { name: "Rotate key" }).click();
  let secret = page.getByRole("dialog", { name: "API key rotated" });
  await secret.getByRole("button", { name: /saved it/ }).click();
  const oldGrace = (await row.getByTestId("grace").textContent()) ?? "";
  expect(oldGrace).toMatch(/Old secret works until /);

  // A second rotation has only one previous hash to keep: the older grace is dropped at once.
  await row.getByRole("button", { name: "Rotate" }).click();
  dialog = page.getByRole("dialog", { name: /^Rotate/ });
  const notice = dialog.getByTestId("rotate-notice");
  await expect(notice).toContainText(/already has a previous secret in grace until/i);
  await expect(notice).toContainText(oldGrace.replace("Old secret works until ", ""));
  await expect(notice).toContainText(/ends that grace immediately/i);
  await expect(notice).toContainText(/older secret stops working right away/i);
  await pick(dialog, "Old secret", "1 hour");
  await dialog.getByRole("button", { name: "Rotate key" }).click();
  secret = page.getByRole("dialog", { name: "API key rotated" });
  await expect(secret.getByTestId("secret-note")).toContainText(/previous secret works until/i);
  await secret.getByRole("button", { name: /saved it/ }).click();
  await expect(row.getByTestId("grace")).not.toHaveText(oldGrace);
});

test("rotating a suspended key does not claim it works", async ({ page, browser }) => {
  const team = await admin.ensureTeam("s8-suspended");
  await admin.putMember(team.id, VIEWER_EMAIL);
  const created = await memberKey(browser, VIEWER, team.id, "s8-suspended-key");
  await memberUpdate(browser, VIEWER, created.key.id, { lifecycle: "suspended" });
  await signIn(page, VIEWER);

  const row = (await myKeyRow(page, "s8-suspended", "s8-suspended-key"));
  await expect(row.getByText("Suspended", { exact: true })).toBeVisible();
  await row.getByRole("button", { name: "Rotate" }).click();
  const dialog = page.getByRole("dialog", { name: /^Rotate/ });
  await expect(dialog.getByTestId("rotate-notice")).toContainText(/suspended/i);
  await expect(dialog.getByTestId("rotate-notice")).toContainText(/does not resume it/i);
  await expect(dialog.getByTestId("rotate-notice")).toContainText(/Use Resume/i);
  await pick(dialog, "Old secret", "1 hour");
  await expect(dialog.getByTestId("rotate-notice")).toContainText(/neither secret can be used while the key is suspended/i);
  await expect(dialog.getByTestId("rotate-notice")).not.toContainText(/keeps working/i);
  await dialog.getByRole("button", { name: "Rotate key" }).click();

  const secret = page.getByRole("dialog", { name: "API key rotated" });
  await expect(secret.getByTestId("secret-note")).toContainText(/will not work until this key is resumed/i);
  await expect(secret.getByTestId("secret-note")).toContainText(/use Resume/i);
  await secret.getByRole("button", { name: /saved it/ }).click();
  await expect(row.getByTestId("grace")).toHaveCount(0);
  await expect(row.getByText("Suspended", { exact: true })).toBeVisible();
});

test("rotating an expired key does not extend it", async ({ page, browser }) => {
  const team = await admin.ensureTeam("s8-expired");
  await admin.putMember(team.id, VIEWER_EMAIL);
  const created = await memberKey(browser, VIEWER, team.id, "s8-expired-key", daysFromNow(2).toISOString());
  await memberUpdate(browser, VIEWER, created.key.id, { expires_at: new Date(Date.now() - 86_400_000).toISOString() });
  await signIn(page, VIEWER);

  const row = (await myKeyRow(page, "s8-expired", "s8-expired-key"));
  await expect(row.getByText("Expired", { exact: true })).toBeVisible();
  await row.getByRole("button", { name: "Rotate" }).click();
  const dialog = page.getByRole("dialog", { name: /^Rotate/ });
  await expect(dialog.getByTestId("rotate-notice")).toContainText(/has expired/i);
  await expect(dialog.getByTestId("rotate-notice")).toContainText(/does not extend its expiry/i);
  await expect(dialog.getByTestId("rotate-notice")).toContainText(/Edit the expiry/i);
  await pick(dialog, "Old secret", "1 hour");
  await expect(dialog.getByTestId("rotate-notice")).toContainText(/already expired with the key/i);
  await expect(dialog.getByTestId("rotate-notice")).not.toContainText(/keeps working/i);
  await dialog.getByRole("button", { name: "Rotate key" }).click();

  const secret = page.getByRole("dialog", { name: "API key rotated" });
  await expect(secret.getByTestId("secret-note")).toContainText(/still expired/i);
  await expect(secret.getByTestId("secret-note")).toContainText(/edit it/i);
  await secret.getByRole("button", { name: /saved it/ }).click();
  await expect(row.getByTestId("grace")).toHaveCount(0);
  await expect(row.getByText("Expired", { exact: true })).toBeVisible();
});

test("edit, suspend and resume an own key", async ({ page, browser }) => {
  const team = await admin.ensureTeam("s8-edit");
  await admin.putMember(team.id, VIEWER_EMAIL);
  const created = await memberKey(browser, VIEWER, team.id, "s8-editable");
  await signIn(page, VIEWER);

  let row = (await myKeyRow(page, "s8-edit", "s8-editable"));
  await row.getByRole("button", { name: "Edit" }).click();
  const edit = page.getByRole("dialog", { name: new RegExp(`Edit “s8-editable” \\(${created.key.display_prefix}`) });
  await edit.getByLabel("Name").fill("s8-renamed");
  await edit.getByLabel("Expires").fill(isoDay(daysFromNow(3)));
  await edit.getByRole("button", { name: "Save" }).click();
  await expect(edit).toBeHidden();
  row = (await myKeyRow(page, "s8-edit", "s8-renamed"));
  await expect(row).toBeVisible();

  await row.getByRole("button", { name: "Suspend" }).click();
  await expect(row.getByText("Suspended", { exact: true })).toBeVisible();
  await row.getByRole("button", { name: "Resume" }).click();
  await expect(row.getByText("Active", { exact: true })).toBeVisible();
});

test("a lowered lifetime is fixed with Edit, then rotate succeeds", async ({ page, browser }) => {
  const team = await admin.ensureTeam("s8-lower", { max_member_key_lifetime_seconds: 30 * 86_400 });
  await admin.putMember(team.id, VIEWER_EMAIL);
  await memberKey(browser, VIEWER, team.id, "s8-lower-key", new Date(Date.now() + 29 * 86_400_000).toISOString());
  await admin.patch(`/teams/${team.id}`, { max_member_key_lifetime_seconds: WEEK });
  await signIn(page, VIEWER);

  const row = (await myKeyRow(page, "s8-lower", "s8-lower-key"));
  await row.getByRole("button", { name: "Rotate" }).click();
  let dialog = page.getByRole("dialog", { name: /^Rotate/ });
  await dialog.getByRole("button", { name: "Rotate key" }).click();
  await expect(dialog.getByTestId("rotate-error")).toContainText(/shorten expires_at first/i);
  await dialog.getByRole("button", { name: "Cancel" }).click();

  // Edit is the way out: shorten the expiry inside the new cap, then rotate.
  await row.getByRole("button", { name: "Edit" }).click();
  const edit = page.getByRole("dialog", { name: /^Edit “s8-lower-key”/ });
  await edit.getByLabel("Expires").fill(isoDay(daysFromNow(2)));
  await edit.getByRole("button", { name: "Save" }).click();
  await expect(edit).toBeHidden();
  await row.getByRole("button", { name: "Rotate" }).click();
  dialog = page.getByRole("dialog", { name: /^Rotate/ });
  await dialog.getByRole("button", { name: "Rotate key" }).click();
  const secret = page.getByRole("dialog", { name: "API key rotated" });
  await expect(secret.getByTestId("secret-value")).toHaveText(/^lw-/);
  await secret.getByRole("button", { name: /saved it/ }).click();
});

test("a removed and re-added member resumes their owner_removed key", async ({ page, browser }) => {
  const team = await admin.ensureTeam("s8-removed");
  await admin.putMember(team.id, VIEWER_EMAIL);
  await memberKey(browser, VIEWER, team.id, "s8-removed-key");
  await admin.removeMember(team.id, VIEWER_EMAIL);
  await admin.putMember(team.id, VIEWER_EMAIL);
  await signIn(page, VIEWER);

  const row = (await myKeyRow(page, "s8-removed", "s8-removed-key"));
  await expect(row.getByText("Suspended", { exact: true })).toBeVisible();
  await expect(row.getByText(/owner removed from team/i)).toBeVisible();
  await row.getByRole("button", { name: "Resume" }).click();
  await expect(row.getByText("Active", { exact: true })).toBeVisible();
});

test("revoke a key after a confirmation", async ({ page, browser }) => {
  const team = await admin.ensureTeam("s8-keys");
  await admin.putMember(team.id, VIEWER_EMAIL);
  await memberKey(browser, VIEWER, team.id, "s8-revoked-key");
  await signIn(page, VIEWER);

  const row = (await myKeyRow(page, "s8-keys", "s8-revoked-key"));
  await row.getByRole("button", { name: "Revoke" }).click();
  await expect(confirmDialog(page).getByText(/stop working immediately/i)).toBeVisible();
  await confirmDialog(page).getByRole("button", { name: "Revoke" }).click();
  await expect(row.getByText("Revoked", { exact: true })).toBeVisible();
  await expect(row.getByRole("button", { name: "Rotate" })).toHaveCount(0);
});

test("a 409 from the per-member cap is shown as text", async ({ page, browser }) => {
  const team = await admin.ensureTeam("s8-cap", { max_keys_per_member: 1 });
  await admin.putMember(team.id, VIEWER_EMAIL);
  await memberKey(browser, VIEWER, team.id, "s8-cap-first");
  await signIn(page, VIEWER);

  const section = await teamDetails(page, "s8-cap");
  await expect(section).toContainText("1 of 1 non-revoked keys");
  await section.getByRole("button", { name: "New key" }).click();
  const form = page.getByRole("dialog", { name: "New key in s8-cap" });
  await form.getByLabel("Name").fill("s8-cap-second");
  await form.getByRole("button", { name: "Create key" }).click();
  await expect(form.getByRole("alert")).toContainText("member key limit reached (1 per member in this team)");
  await expect(form).toBeVisible();
  await form.getByRole("button", { name: "Cancel" }).click();
});

test("phone: two same-name keys stay identifiable, including the rotate and revoke dialogs", async ({ page, browser }) => {
  const team = await admin.ensureTeam("s8-phone");
  await admin.putMember(team.id, VIEWER_EMAIL);
  const firstExpires = daysFromNow(3).toISOString();
  const secondExpires = daysFromNow(5).toISOString();
  const first = await memberKey(browser, VIEWER, team.id, "production", firstExpires);
  const second = await memberKey(browser, VIEWER, team.id, "production", secondExpires);
  await signIn(page, VIEWER);
  await page.setViewportSize({ width: 390, height: 844 });

  await myKeyRow(page, "s8-phone", "production");
  const row = (prefix: string) => page.getByTestId("my-keys").getByRole("row").filter({ hasText: prefix });
  const firstRow = row(first.key.display_prefix);
  const secondRow = row(second.key.display_prefix);
  await expect(firstRow).toBeVisible();
  await expect(secondRow).toBeVisible();
  await expect(firstRow.getByText(first.key.display_prefix, { exact: false })).toBeVisible();
  await expect(secondRow.getByText(second.key.display_prefix, { exact: false })).toBeVisible();
  const firstDay = await page.evaluate((iso) => new Date(iso).toLocaleDateString(), firstExpires);
  const secondDay = await page.evaluate((iso) => new Date(iso).toLocaleDateString(), secondExpires);
  await expect(firstRow.getByTestId("phone-expiry")).toBeVisible();
  await expect(firstRow.getByTestId("phone-expiry")).toContainText(firstDay);
  await expect(secondRow.getByTestId("phone-expiry")).toBeVisible();
  await expect(secondRow.getByTestId("phone-expiry")).toContainText(secondDay);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);

  await secondRow.getByRole("button", { name: "Rotate" }).click();
  const rotate = page.getByRole("dialog", { name: new RegExp(`Rotate .*${second.key.display_prefix}`) });
  await expect(rotate).toBeVisible();
  await expect(rotate.getByRole("heading")).toContainText(second.key.display_prefix);
  await rotate.getByRole("button", { name: "Cancel" }).click();

  await secondRow.getByRole("button", { name: "Revoke" }).click();
  await expect(confirmDialog(page).getByText(second.key.display_prefix, { exact: false })).toBeVisible();
  await confirmDialog(page).getByRole("button", { name: "Cancel" }).click();
});

test("a hidden page opened by URL explains instead of failing", async ({ page }) => {
  const team = await admin.ensureTeam("s8-keys");
  await admin.putMember(team.id, VIEWER_EMAIL);
  await signIn(page, VIEWER);

  await page.goto("/users");
  await expect(page.getByRole("heading", { name: "You do not have access to this page" })).toBeVisible();
  await expect(page.getByTestId("no-access")).toBeVisible();
  await expect(page.getByTestId("state-error")).toHaveCount(0);
  await expect(page.getByRole("table")).toHaveCount(0);
  await page.getByRole("link", { name: "Go to My keys" }).click();
  await expect(page).toHaveURL(/\/my-keys$/);
});

test("an authorization outage shows a temporary state with retry, not 'no team'", async ({ page, browser }) => {
  const team = await admin.ensureTeam("s8-keys");
  await admin.putMember(team.id, VIEWER_EMAIL);
  await signIn(page, VIEWER);

  const { context, csrf } = await memberSession(browser, VIEWER);
  const real = await (await context.request.get("/api/v1/users/me", { headers: { "X-Lumewarp-CSRF": csrf } })).json();
  await context.close();

  let meRequests = 0;
  await page.route("**/api/v1/users/me", async (route) => {
    meRequests++;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ...real, authz_unavailable: true, teams: [] }),
    });
  });
  await page.goto("/my-keys");
  await expect(page.getByTestId("authz-temporary")).toBeVisible();
  await expect(page.getByTestId("authz-unavailable")).toBeVisible();
  await expect(page.getByText(/cannot be verified right now/i)).toBeVisible();
  await expect(page.getByTestId("no-teams")).toHaveCount(0);
  await expect(page.getByText(/Ask an administrator to add you to a team/i)).toHaveCount(0);
  await expect(page.getByTestId("team-s8-keys")).toHaveCount(0);
  expect(meRequests).toBeGreaterThan(0);

  await page.getByRole("button", { name: "Retry" }).click();
  await expect.poll(() => meRequests).toBeGreaterThan(1);
  await page.unroute("**/api/v1/users/me");
  await page.getByRole("button", { name: "Retry" }).click();
  await expect(page.getByTestId("team-s8-keys")).toBeVisible();
});

test("a user on no team sees an empty state, not an empty table", async ({ page }) => {
  await signIn(page, OUTSIDER, "/");
  await expect(page).toHaveURL(/\/my-keys$/);
  await expect(page.getByTestId("no-teams")).toContainText("Ask an administrator to add you to a team");
  await expect(page.getByRole("table")).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Overview", exact: true })).toHaveCount(0);
});

test("the emergency session has its own text, not the ask-an-admin state", async ({ page, request }) => {
  await page.context().addCookies((await request.storageState()).cookies);
  await page.goto("/my-keys");
  await expect(page.getByTestId("break-glass-no-keys")).toBeVisible();
  await expect(page.getByText(/does not own API keys/i)).toBeVisible();
  await expect(page.getByTestId("no-teams")).toHaveCount(0);
  await expect(page.getByText(/Ask an administrator to add you to a team/i)).toHaveCount(0);
});

test("a team admin's own keys rotate and revoke; the team page lists every key", async ({ page, browser }) => {
  const team = await admin.ensureTeam("s8-admin");
  await admin.putMember(team.id, TEAM_ADMIN_EMAIL, "admin");
  await admin.putMember(team.id, VIEWER_EMAIL);
  await memberKey(browser, TEAM_ADMIN, team.id, "s8-admin-key");
  await memberKey(browser, VIEWER, team.id, "s8-admin-other");

  await signIn(page, TEAM_ADMIN, "/");
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  const nav = page.getByRole("navigation", { name: "Main" });
  await expect(nav.getByRole("link", { name: "Keys", exact: true })).toHaveCount(0);
  await page.goto("/my-keys");
  const row = (await myKeyRow(page, "s8-admin", "s8-admin-key"));
  await expect(row).toBeVisible();
  await expect(page.getByTestId("my-keys")).not.toContainText("s8-admin-other");

  await row.getByRole("button", { name: "Rotate" }).click();
  const rotate = page.getByRole("dialog", { name: /^Rotate/ });
  await rotate.getByRole("button", { name: "Rotate key" }).click();
  const secret = page.getByRole("dialog", { name: "API key rotated" });
  await expect(secret.getByTestId("secret-value")).toHaveText(/^lw-/);
  await secret.getByRole("button", { name: /saved it/ }).click();
  await row.getByRole("button", { name: "Revoke" }).click();
  await confirmDialog(page).getByRole("button", { name: "Revoke" }).click();
  await expect(row.getByText("Revoked", { exact: true })).toBeVisible();

  // Other members' keys are managed on the team page, which still lists every key.
  await page.goto(`/teams/${team.id}`);
  await page.getByRole("tab", { name: "Keys" }).click();
  await expect(rowOf(page, "s8-admin-other")).toBeVisible();
  await expect(rowOf(page, "s8-admin-key")).toBeVisible();
});

test("a team admin sets the member key limits from the team settings", async ({ page, request }) => {
  const team = await admin.ensureTeam("s8-limits");
  await admin.putMember(team.id, TEAM_ADMIN_EMAIL, "admin");
  await signIn(page, TEAM_ADMIN, `/teams/${team.id}`);
  await page.getByRole("tab", { name: "Settings" }).click();
  await expect(page.getByText("Keys per member")).toBeVisible();
  await page.getByRole("button", { name: "Edit team" }).click();
  const form = page.getByRole("dialog", { name: "Edit team settings" });
  await form.getByLabel("Max keys per member").fill("3");
  await form.getByLabel("Longest member key lifetime (seconds)").fill("3600");
  await form.getByRole("button", { name: "Save" }).click();
  await expect(form).toBeHidden();
  const saved = await (await request.get(`/api/v1/teams/${team.id}`, { headers: admin.headers })).json();
  expect(saved.max_keys_per_member).toBe(3);
  expect(saved.max_member_key_lifetime_seconds).toBe(3600);
  await expect(page.getByText("1 hour", { exact: true })).toBeVisible();
  await page.goto("/my-keys");
  const limits = await teamDetails(page, "s8-limits");
  await expect(limits).toContainText("0 of 3 non-revoked keys");
  await expect(limits).toContainText("Longest lifetime: 1 hour");
});


test("a one-time secret survives failed access checks, an outage and route changes", async ({ page, browser }) => {
  const team1 = await admin.ensureTeam("s8-survive-1");
  const team2 = await admin.ensureTeam("s8-survive-2");
  await admin.putMember(team1.id, VIEWER_EMAIL);
  await admin.putMember(team2.id, VIEWER_EMAIL);
  const { context } = await memberSession(browser, VIEWER);
  const real = await (await context.request.get("/api/v1/users/me")).json();
  await context.close();
  await signIn(page, VIEWER, "/my-keys");
  await expect(page.getByTestId("team-s8-survive-1")).toBeVisible();
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);

  // The create's invalidation refetches /users/me; fail that background check.
  await page.route("**/api/v1/users/me", (route) =>
    route.fulfill({ status: 403, contentType: "application/json", body: JSON.stringify({ error: { code: "forbidden", message: "temporary" } }) }),
  );
  const section = page.getByTestId("team-s8-survive-1");
  await section.getByRole("button", { name: "New key" }).click();
  const form = page.getByRole("dialog", { name: "New key in s8-survive-1" });
  await form.getByLabel("Name").fill("s8-survivor");
  await form.getByRole("button", { name: "Create key" }).click();
  const secret = page.getByRole("dialog", { name: "API key created" });
  await expect(secret.getByTestId("secret-value")).toHaveText(/^lw-/);
  const value = await secret.getByTestId("secret-value").textContent();
  await expect(page.getByTestId("me-check-failed")).toBeVisible();
  await secret.getByRole("button", { name: "Copy" }).click();
  await expect(secret.getByRole("button", { name: "Copied" })).toBeVisible();

  // A successful response that says authorization is unavailable.
  await page.unroute("**/api/v1/users/me");
  await page.route("**/api/v1/users/me", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ...real, authz_unavailable: true, teams: [] }) }),
  );
  await routeChange(page, `/teams/${team1.id}`);
  await expect(page.getByTestId("authz-blocked")).toBeVisible();
  await expect(secret.getByTestId("secret-value")).toHaveText(value!);

  // Recovery through another guarded entry.
  await page.unroute("**/api/v1/users/me");
  await routeChange(page, `/teams/${team2.id}`);
  // The dialog is modal, so the heading behind it is outside the accessibility tree; a CSS locator sees it.
  await expect(page.locator("main h1").filter({ hasText: team2.name })).toBeVisible();
  await expect(secret.getByTestId("secret-value")).toHaveText(value!);
  await secret.getByRole("button", { name: /saved it/ }).click();
  await expect(secret).toBeHidden();
  expect(await page.content()).not.toContain(value!);
});

test("a one-time secret survives losing the role, while the page becomes no-access", async ({ page }) => {
  const team = await admin.ensureTeam("s8-role");
  await admin.putMember(team.id, SHOTS_EMAIL, "admin");
  await admin.putMember(team.id, MEMBER_EMAIL, "admin");
  await signIn(page, SHOTS_TOKEN, `/teams/${team.id}`);
  await page.getByRole("tab", { name: "Keys" }).click();
  await page.getByRole("button", { name: "New key" }).click();
  const form = page.getByRole("dialog", { name: "New API key" });
  await form.getByLabel("Name").fill("s8-role-key");
  await form.getByRole("button", { name: "Create key" }).click();
  const secret = page.getByRole("dialog", { name: "API key created" });
  await expect(secret.getByTestId("secret-value")).toHaveText(/^lw-/);
  const value = await secret.getByTestId("secret-value").textContent();

  await admin.removeMember(team.id, SHOTS_EMAIL);
  await routeChange(page, "/teams");
  await expect(page.getByTestId("no-access")).toBeVisible();
  await expect(secret.getByTestId("secret-value")).toHaveText(value!);
  await secret.getByRole("button", { name: /saved it/ }).click();
  expect(await page.content()).not.toContain(value!);
});

test("a background 401 keeps the one-time secret above the login page", async ({ page }) => {
  const team = await admin.ensureTeam("s8-401");
  await admin.putMember(team.id, MEMBER_EMAIL);
  await signIn(page, MEMBER, "/my-keys");
  await expect(page.getByTestId("team-s8-401")).toBeVisible();
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);

  // The create's post-success invalidation gets a 401; the login page's discovery is unauthenticated.
  await page.route("**/api/v1/users/me", (route) =>
    route.fulfill({ status: 401, contentType: "application/json", body: JSON.stringify({ error: { code: "unauthenticated", message: "session expired" } }) }),
  );
  await page.route("**/api/v1/session", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ oidc_enabled: false, break_glass_enabled: true, authenticated: false }),
    }),
  );
  await page.getByTestId("team-s8-401").getByRole("button", { name: "New key" }).click();
  const form = page.getByRole("dialog", { name: "New key in s8-401" });
  await form.getByLabel("Name").fill("s8-401-key");
  await form.getByRole("button", { name: "Create key" }).click();
  const secret = page.getByRole("dialog", { name: "API key created" });
  await expect(secret.getByTestId("secret-value")).toHaveText(/^lw-/);
  const value = await secret.getByTestId("secret-value").textContent();

  await expect(page).toHaveURL(/\/login/);
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  // The open modal hides the page from the accessibility tree, so these must count hidden elements too.
  await expect(page.getByRole("heading", { name: "My keys", includeHidden: true })).toHaveCount(0);
  await expect(page.getByRole("navigation", { name: "Main", includeHidden: true })).toHaveCount(0);
  await expect(page.getByTestId("team-s8-401")).toHaveCount(0);
  await expect(secret.getByTestId("secret-value")).toHaveText(value!);
  await secret.getByRole("button", { name: "Copy" }).click();
  await expect(secret.getByRole("button", { name: "Copied" })).toBeVisible();
  await secret.getByRole("button", { name: /saved it/ }).click();
  await expect(page.getByTestId("secret-value")).toHaveCount(0);
  expect(await page.content()).not.toContain(value!);

  // The login form works once the secret is closed: it validates a submit and takes input.
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByText("Password is required")).toBeVisible();
  const password = page.getByLabel("Break-glass password");
  await password.fill("typed");
  await expect(password).toHaveValue("typed");
});

test("the real Sign out button drops a create still in flight", async ({ page }) => {
  const team = await admin.ensureTeam("s8-inflight-signout");
  await admin.putMember(team.id, MEMBER_EMAIL);
  await signIn(page, MEMBER, `/teams/${team.id}`);
  await page.getByRole("tab", { name: "Keys" }).click();

  let releaseResponse!: () => void;
  const hold = new Promise<void>((resolve) => {
    releaseResponse = resolve;
  });
  let responseFetched!: () => void;
  const fetched = new Promise<void>((resolve) => {
    responseFetched = resolve;
  });
  let responseDelivered!: () => void;
  const delivered = new Promise<void>((resolve) => {
    responseDelivered = resolve;
  });
  await page.route(`**/api/v1/teams/${team.id}/keys**`, async (route) => {
    if (route.request().method() !== "POST") {
      await route.continue();
      return;
    }
    const response = await route.fetch();
    responseFetched();
    await hold;
    await route.fulfill({ response });
    responseDelivered();
  });

  await page.getByRole("button", { name: "New key" }).click();
  const form = page.getByRole("dialog", { name: "New API key" });
  await form.getByLabel("Name").fill("s8-inflight-key");
  await form.getByRole("button", { name: "Create key" }).click();
  await fetched;
  await page.keyboard.press("Escape");
  await expect(form).toBeHidden();

  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login/);
  const postResponse = page.waitForResponse(
    (response) => new URL(response.url()).pathname === `/api/v1/teams/${team.id}/keys` && response.request().method() === "POST",
  );
  releaseResponse();
  await postResponse;
  await delivered;
  // Nothing signals "the dropped secret was not shown"; allow a broken build time to show it.
  await page.waitForTimeout(500);
  await expect(page.getByTestId("secret-value")).toHaveCount(0);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByLabel("Break-glass password")).toBeVisible();
});

test("a failed sign-out keeps the page and a later secret still shows", async ({ page }) => {
  const team = await admin.ensureTeam("s8-failed-logout");
  await admin.putMember(team.id, VIEWER_EMAIL);
  await signIn(page, VIEWER, "/my-keys");
  await page.route("**/api/v1/session/logout", (route) =>
    route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({ error: { code: "server_error", message: "logout failed" } }),
    }),
  );
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByText("logout failed")).toBeVisible();
  await expect(page).toHaveURL(/\/my-keys$/);
  await expect(page.getByRole("heading", { name: "My keys" })).toBeVisible();

  await page.getByTestId("team-s8-failed-logout").getByRole("button", { name: "New key" }).click();
  const form = page.getByRole("dialog", { name: "New key in s8-failed-logout" });
  await form.getByLabel("Name").fill("s8-after-failed-logout");
  await form.getByRole("button", { name: "Create key" }).click();
  const secret = page.getByRole("dialog", { name: "API key created" });
  await expect(secret.getByTestId("secret-value")).toHaveText(/^lw-/);
  await secret.getByRole("button", { name: /saved it/ }).click();
});

test("a one-time secret dialog cannot be dismissed by Escape or a click outside", async ({ page }) => {
  const team = await admin.ensureTeam("s8-dismiss");
  await admin.putMember(team.id, VIEWER_EMAIL);
  await signIn(page, VIEWER);
  await page.getByTestId("team-s8-dismiss").getByRole("button", { name: "New key" }).click();
  const form = page.getByRole("dialog", { name: "New key in s8-dismiss" });
  await form.getByLabel("Name").fill("s8-dismiss-key");
  await form.getByRole("button", { name: "Create key" }).click();
  const secret = page.getByRole("dialog", { name: "API key created" });
  const value = await secret.getByTestId("secret-value").textContent();

  await expect(page.getByRole("button", { name: "Close" })).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(secret.getByTestId("secret-value")).toHaveText(value!);
  await page.mouse.click(5, 5);
  await expect(secret.getByTestId("secret-value")).toHaveText(value!);
  await secret.getByRole("button", { name: /saved it/ }).click();
  await expect(page.getByTestId("secret-value")).toHaveCount(0);
  expect(await page.content()).not.toContain(value!);
});

test("two secrets arriving in a row are both shown, the second after the first is closed", async ({ page }) => {
  const team = await admin.ensureTeam("s8-two-secrets");
  await admin.putMember(team.id, VIEWER_EMAIL);
  await signIn(page, VIEWER, `/teams/${team.id}`);
  await page.getByRole("tab", { name: "Keys" }).click();
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);

  let teamSecret = "";
  let releaseTeam!: () => void;
  const teamGate = new Promise<void>((resolve) => {
    releaseTeam = resolve;
  });
  let teamFetched!: () => void;
  const fetchedGate = new Promise<void>((resolve) => {
    teamFetched = resolve;
  });
  let teamDelivered!: () => void;
  const deliveredGate = new Promise<void>((resolve) => {
    teamDelivered = resolve;
  });
  await page.route(`**/api/v1/teams/${team.id}/keys**`, async (route) => {
    if (route.request().method() !== "POST" || (route.request().postDataJSON() as { name?: string }).name !== "s8-team-secret") {
      await route.continue();
      return;
    }
    const response = await route.fetch();
    const body = await response.json();
    teamSecret = body.secret;
    teamFetched();
    await teamGate;
    await route.fulfill({ response, body: JSON.stringify(body) });
    teamDelivered();
  });

  // Hold only the team create; close its busy form while the mutation is in flight.
  await page.getByRole("button", { name: "New key" }).click();
  let form = page.getByRole("dialog", { name: "New API key" });
  await form.getByLabel("Name").fill("s8-team-secret");
  await form.getByRole("button", { name: "Create key" }).click();
  await fetchedGate;
  await page.keyboard.press("Escape");
  await expect(form).toBeHidden();

  // The My keys create is not held: it shows the first secret while the team one is pending.
  const myKeysMe = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/v1/users/me");
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "My keys" }).click();
  await expect(page.getByRole("heading", { name: "My keys" })).toBeVisible();
  await page.getByTestId("team-s8-two-secrets").getByRole("button", { name: "New key" }).click();
  form = page.getByRole("dialog", { name: "New key in s8-two-secrets" });
  await form.getByLabel("Name").fill("s8-my-secret");
  await form.getByRole("button", { name: "Create key" }).click();
  const first = page.getByRole("dialog", { name: "API key created" });
  await expect(first.getByTestId("secret-value")).toHaveText(/^lw-/);
  const firstSecret = await first.getByTestId("secret-value").textContent();
  await myKeysMe;

  expect(teamSecret).toMatch(/^lw-/);
  expect(firstSecret).not.toBe(teamSecret);
  const teamInvalidated = page.waitForRequest((request) => new URL(request.url()).pathname === "/api/v1/users/me");
  releaseTeam();
  await teamInvalidated;
  await deliveredGate;
  // The team secret arrived while the first was open: the first is still the visible one.
  await expect(first.getByTestId("secret-value")).toHaveText(firstSecret!);
  await first.getByRole("button", { name: "Copy" }).click();
  await expect(first.getByRole("button", { name: "Copied" })).toBeVisible();
  await first.getByRole("button", { name: /saved it/ }).click();
  const second = page.getByRole("dialog", { name: "API key created" });
  await expect(second.getByTestId("secret-value")).toHaveText(teamSecret);
  // The copy state belongs to the previous secret and must reset for this one.
  await expect(second.getByRole("button", { name: "Copy" })).toBeVisible();
  await second.getByRole("button", { name: /saved it/ }).click();
  await expect(page.getByTestId("secret-value")).toHaveCount(0);
});

test("a failed background check keeps an open create-key form and its draft", async ({ page }) => {
  const team = await admin.ensureTeam("s8-draft");
  await admin.putMember(team.id, VIEWER_EMAIL);
  const expiry = isoDay(daysFromNow(3));
  await signIn(page, VIEWER, "/my-keys");
  await page.getByTestId("team-s8-draft").getByRole("button", { name: "New key" }).click();
  const form = page.getByRole("dialog", { name: "New key in s8-draft" });
  await form.getByLabel("Name").fill("s8-draft-key");
  await form.getByLabel("Expires").fill(expiry);

  // Stale the keys query (React Query's staleTime is 15 s), then fail its focus refetch; its 403
  // revalidates /users/me, which fails too. Step the page's clock rather than sleeping: a 16 s sleep
  // against a 15 s staleTime left a one-second margin that a host clock step could eat.
  await page.evaluate(() => {
    const now = Date.now;
    Date.now = () => now() + 20_000;
  });
  const forbidden = { status: 403, contentType: "application/json", body: JSON.stringify({ error: { code: "forbidden", message: "temporary" } }) };
  await page.route("**/api/v1/users/me/keys*", (route) => route.fulfill(forbidden));
  await page.route("**/api/v1/users/me", (route) => route.fulfill(forbidden));
  // Refocus: React Query listens on window and only refetches stale queries on a hidden -> visible transition.
  await page.evaluate(() => {
    window.dispatchEvent(new Event("blur"));
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
    window.dispatchEvent(new Event("visibilitychange"));
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
    window.dispatchEvent(new Event("visibilitychange"));
    window.dispatchEvent(new Event("focus"));
  });
  await expect(page.getByTestId("me-check-failed")).toBeVisible();
  await expect(form).toBeVisible();
  await expect(form.getByLabel("Name")).toHaveValue("s8-draft-key");
  await expect(form.getByLabel("Expires")).toHaveValue(expiry);
});

test("a failed guard check keeps the guarded page and an open create-key form", async ({ page }) => {
  const team = await admin.ensureTeam("s8-guard-draft");
  await admin.putMember(team.id, VIEWER_EMAIL);
  await signIn(page, VIEWER, `/teams/${team.id}`);
  await page.getByRole("tab", { name: "Keys" }).click();
  await page.getByRole("button", { name: "New key" }).click();
  const form = page.getByRole("dialog", { name: "New API key" });
  await form.getByLabel("Name").fill("s8-guard-draft");

  // Stale the team keys query the same way as the My keys test above, then fail its focus refetch;
  // its 403 revalidates /users/me, which fails too.
  await page.evaluate(() => {
    const now = Date.now;
    Date.now = () => now() + 20_000;
  });
  const forbidden = { status: 403, contentType: "application/json", body: JSON.stringify({ error: { code: "forbidden", message: "temporary" } }) };
  await page.route(`**/api/v1/teams/${team.id}/keys*`, (route) => route.fulfill(forbidden));
  await page.route("**/api/v1/users/me", (route) => route.fulfill(forbidden));
  await page.evaluate(() => {
    window.dispatchEvent(new Event("blur"));
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
    window.dispatchEvent(new Event("visibilitychange"));
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
    window.dispatchEvent(new Event("visibilitychange"));
    window.dispatchEvent(new Event("focus"));
  });
  await expect(page.getByTestId("me-check-failed")).toBeVisible();
  await expect(form).toBeVisible();
  await expect(form.getByLabel("Name")).toHaveValue("s8-guard-draft");
  await page.keyboard.press("Escape");
  await expect(form).toBeHidden();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByTestId("me-check-failed")).toBeVisible();
  await shot(page, "s8-me-check-failed", { fullPage: true });
});

test("a key that is both suspended and expired names both conditions", async ({ page, browser }) => {
  const team = await admin.ensureTeam("s8-cross");
  await admin.putMember(team.id, VIEWER_EMAIL);
  const created = await memberKey(browser, VIEWER, team.id, "s8-cross-key", daysFromNow(2).toISOString());
  await memberUpdate(browser, VIEWER, created.key.id, { lifecycle: "suspended" });
  await memberUpdate(browser, VIEWER, created.key.id, { expires_at: new Date(Date.now() - 86_400_000).toISOString() });
  await signIn(page, VIEWER);

  const row = (await myKeyRow(page, "s8-cross", "s8-cross-key"));
  await row.getByRole("button", { name: "Rotate" }).click();
  const dialog = page.getByRole("dialog", { name: /^Rotate/ });
  const notice = dialog.getByTestId("rotate-notice");
  await expect(notice).toContainText(/suspended/i);
  await expect(notice).toContainText(/expired/i);
  await expect(notice).toContainText(/Use Resume/i);
  await expect(notice).toContainText(/Edit the expiry/i);
  await dialog.getByRole("button", { name: "Rotate key" }).click();
  const secret = page.getByRole("dialog", { name: "API key rotated" });
  await expect(secret.getByTestId("secret-note")).toContainText(/resumed/i);
  await expect(secret.getByTestId("secret-note")).toContainText(/expired/i);
  await expect(secret.getByTestId("secret-note")).toContainText(/edit it/i);
  await secret.getByRole("button", { name: /saved it/ }).click();
});

test("an administrator-suspended key says an administrator must resume it", async ({ page, browser }) => {
  const team = await admin.ensureTeam("s8-admin-suspend");
  await admin.putMember(team.id, VIEWER_EMAIL);
  const created = await memberKey(browser, VIEWER, team.id, "s8-admin-suspend-key");
  await admin.patch(`/keys/${created.key.id}`, { lifecycle: "suspended" });
  await signIn(page, VIEWER);

  const row = (await myKeyRow(page, "s8-admin-suspend", "s8-admin-suspend-key"));
  await expect(row.getByText("Suspended", { exact: true })).toBeVisible();
  await expect(row.getByRole("button", { name: "Resume" })).toBeDisabled();
  await row.getByRole("button", { name: "Rotate" }).click();
  const dialog = page.getByRole("dialog", { name: /^Rotate/ });
  await expect(dialog.getByTestId("rotate-notice")).toContainText(/suspended by an administrator/i);
  await expect(dialog.getByTestId("rotate-notice")).toContainText(/an administrator must resume it/i);
  await expect(dialog.getByTestId("rotate-notice")).not.toContainText(/Use Resume/i);
  await dialog.getByRole("button", { name: "Rotate key" }).click();
  const secret = page.getByRole("dialog", { name: "API key rotated" });
  await expect(secret.getByTestId("secret-note")).toContainText(/an administrator resumes/i);
  await secret.getByRole("button", { name: /saved it/ }).click();
});

test("a reactivated owner's key still needs an administrator to resume it", async ({ page, browser }) => {
  const team = await admin.ensureTeam("s8-reactivated");
  await admin.putMember(team.id, VIEWER_EMAIL);
  await memberKey(browser, VIEWER, team.id, "s8-reactivated-key");
  const user = await admin.user(VIEWER_EMAIL);
  await admin.patch(`/users/${user.id}`, { is_active: false });
  await admin.patch(`/users/${user.id}`, { is_active: true });
  await signIn(page, VIEWER);

  const row = (await myKeyRow(page, "s8-reactivated", "s8-reactivated-key"));
  await expect(row.getByText("Suspended", { exact: true })).toBeVisible();
  await expect(row.getByText(/owner deactivated/i)).toBeVisible();
  await expect(row.getByRole("button", { name: "Resume" })).toBeDisabled();
  await row.getByRole("button", { name: "Rotate" }).click();
  const dialog = page.getByRole("dialog", { name: /^Rotate/ });
  await expect(dialog.getByTestId("rotate-notice")).toContainText(/an administrator must resume it/i);
  await expect(dialog.getByTestId("rotate-notice")).not.toContainText(/Use Resume/i);
  // The owner is active again, so the reactivation sentence must not appear.
  await expect(dialog.getByTestId("rotate-notice")).not.toContainText(/account must be reactivated/i);
  await dialog.getByRole("button", { name: "Rotate key" }).click();
  const secret = page.getByRole("dialog", { name: "API key rotated" });
  await expect(secret.getByTestId("secret-note")).toContainText(/an administrator resumes/i);
  await expect(secret.getByTestId("secret-note")).not.toContainText(/account must be reactivated/i);
  await secret.getByRole("button", { name: /saved it/ }).click();
});

test("an unknown suspension reason gets the conservative administrator text", async ({ page, browser }) => {
  const team = await admin.ensureTeam("s8-unknown-reason");
  await admin.putMember(team.id, VIEWER_EMAIL);
  const created = await memberKey(browser, VIEWER, team.id, "s8-unknown-key");
  await memberUpdate(browser, VIEWER, created.key.id, { lifecycle: "suspended" });
  await page.route("**/api/v1/users/me/keys*", async (route) => {
    const response = await route.fetch();
    const body = (await response.json()) as {
      items: { id: string; suspended_reason: string | null; owner_active: boolean | null }[];
    };
    for (const item of body.items) {
      if (item.id === created.key.id) {
        item.suspended_reason = "future_reason";
        item.owner_active = false;
      }
    }
    await route.fulfill({ response, body: JSON.stringify(body) });
  });
  await signIn(page, VIEWER);

  const row = (await myKeyRow(page, "s8-unknown-reason", "s8-unknown-key"));
  await expect(row.getByText("Suspended", { exact: true })).toBeVisible();
  await expect(row.getByRole("button", { name: "Resume" })).toBeDisabled();
  await row.getByRole("button", { name: "Rotate" }).click();
  const dialog = page.getByRole("dialog", { name: /^Rotate/ });
  await expect(dialog.getByTestId("rotate-notice")).toContainText(/an administrator must resume it/i);
  await expect(dialog.getByTestId("rotate-notice")).toContainText(/account must be reactivated first/i);
  await expect(dialog.getByTestId("rotate-notice")).not.toContainText(/Use Resume/i);
});

test("a suspended key with a retained previous secret warns that the retention ends now", async ({ page, browser }) => {
  const team = await admin.ensureTeam("s8-grace-suspended");
  await admin.putMember(team.id, VIEWER_EMAIL);
  const created = await memberKey(browser, VIEWER, team.id, "s8-grace-suspended-key");
  await memberRotate(browser, VIEWER, created.key.id, 3600);
  await memberUpdate(browser, VIEWER, created.key.id, { lifecycle: "suspended" });
  await signIn(page, VIEWER);

  const row = (await myKeyRow(page, "s8-grace-suspended", "s8-grace-suspended-key"));
  await expect(row.getByTestId("rotation-note")).toContainText(/kept until/i);
  await row.getByRole("button", { name: "Rotate" }).click();
  const notice = page.getByRole("dialog", { name: /^Rotate/ }).getByTestId("rotate-notice");
  await expect(notice).toContainText(/still has a previous secret kept until/i);
  await expect(notice).toContainText(/ends that retention immediately/i);
  await expect(notice).not.toContainText(/older secret stops working right away/i);
  await expect(notice).not.toContainText(/older secret works/i);
});

test.describe("local expiry dates", () => {
  test.describe("negative UTC offset", () => {
    test.use({ timezoneId: "America/Los_Angeles" });
    test("create then edit keeps the local day west of UTC", async ({ page }) => {
      const team = await admin.ensureTeam("s8-tz-la");
      await admin.putMember(team.id, VIEWER_EMAIL);
      await signIn(page, VIEWER);
      const section = page.getByTestId("team-s8-tz-la");
      await section.getByRole("button", { name: "New key" }).click();
      const create = page.getByRole("dialog", { name: "New key in s8-tz-la" });
      await create.getByLabel("Name").fill("s8-tz-la-key");
      await create.getByLabel("Expires").fill("2027-01-15");
      await create.getByRole("button", { name: "Create key" }).click();
      const secret = page.getByRole("dialog", { name: "API key created" });
      await secret.getByRole("button", { name: /saved it/ }).click();

      const row = (await myKeyRow(page, "s8-tz-la", "s8-tz-la-key"));
      await row.getByRole("button", { name: "Edit" }).click();
      const edit = page.getByRole("dialog", { name: /^Edit “s8-tz-la-key”/ });
      await expect(edit.getByLabel("Expires")).toHaveValue("2027-01-15");
      await edit.getByRole("button", { name: "Save" }).click();
      await expect(edit).toBeHidden();

      await row.getByRole("button", { name: "Edit" }).click();
      const again = page.getByRole("dialog", { name: /^Edit “s8-tz-la-key”/ });
      await again.getByLabel("Expires").fill("2027-02-20");
      await again.getByRole("button", { name: "Save" }).click();
      await expect(again).toBeHidden();
      await row.getByRole("button", { name: "Edit" }).click();
      const third = page.getByRole("dialog", { name: /^Edit “s8-tz-la-key”/ });
      await expect(third.getByLabel("Expires")).toHaveValue("2027-02-20");
      await third.getByRole("button", { name: "Cancel" }).click();
    });
  });

  test.describe("positive UTC offset", () => {
    test.use({ timezoneId: "Asia/Taipei" });
    test("create then edit keeps the local day east of UTC", async ({ page }) => {
      const team = await admin.ensureTeam("s8-tz-tpe");
      await admin.putMember(team.id, VIEWER_EMAIL);
      await signIn(page, VIEWER);
      const section = page.getByTestId("team-s8-tz-tpe");
      await section.getByRole("button", { name: "New key" }).click();
      const create = page.getByRole("dialog", { name: "New key in s8-tz-tpe" });
      await create.getByLabel("Name").fill("s8-tz-tpe-key");
      await create.getByLabel("Expires").fill("2027-01-15");
      await create.getByRole("button", { name: "Create key" }).click();
      const secret = page.getByRole("dialog", { name: "API key created" });
      await secret.getByRole("button", { name: /saved it/ }).click();
      const row = (await myKeyRow(page, "s8-tz-tpe", "s8-tz-tpe-key"));
      await row.getByRole("button", { name: "Edit" }).click();
      const edit = page.getByRole("dialog", { name: /^Edit “s8-tz-tpe-key”/ });
      await expect(edit.getByLabel("Expires")).toHaveValue("2027-01-15");
      await edit.getByRole("button", { name: "Cancel" }).click();
    });
  });
});

test("an outage on guarded pages is a blocked state with retry, not no-access", async ({ page, request }) => {
  const team = await admin.ensureTeam("s8-outage-pages");
  const real = await admin.get<Record<string, unknown>>("/users/me");
  await page.context().addCookies((await request.storageState()).cookies);
  await page.route("**/api/v1/users/me", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ...real, authz_unavailable: true, teams: [] }) }),
  );
  await page.goto("/users");
  await expect(page.getByTestId("authz-blocked")).toBeVisible();
  await expect(page.getByRole("button", { name: "Retry" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "You do not have access to this page" })).toHaveCount(0);
  await expect(page.getByRole("table")).toHaveCount(0);
  await expect(page.getByTestId("state-error")).toHaveCount(0);

  await page.goto(`/teams/${team.id}`);
  await expect(page.getByTestId("authz-blocked")).toBeVisible();
  await expect(page.getByRole("button", { name: "Retry" })).toBeVisible();
  await expect(page.getByRole("tab")).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "You do not have access to this page" })).toHaveCount(0);
});

test("screenshots: My keys, no team, the secret and the rotate dialog", async ({ page, browser }) => {
  test.skip(!SHOTS, "set E2E_SHOTS to a directory to save screenshots");
  test.setTimeout(240_000);
  const team = await admin.ensureTeam("s8-shots", { max_keys_per_member: 20, max_member_key_lifetime_seconds: WEEK });
  await admin.putMember(team.id, SHOTS_EMAIL);
  await memberKey(browser, SHOTS_TOKEN, team.id, "s8-shots-key");
  const rotated = await memberKey(browser, SHOTS_TOKEN, team.id, "s8-shots-rotated");
  await memberRotate(browser, SHOTS_TOKEN, rotated.key.id, 3600);

  for (const [label, width, height, theme] of [
    ["desktop-light", 1280, 900, "light"],
    ["desktop-dark", 1280, 900, "dark"],
    ["phone-light", 390, 844, "light"],
    ["phone-dark", 390, 844, "dark"],
  ] as const) {
    await page.setViewportSize({ width, height });
    await signIn(page, SHOTS_TOKEN, "/my-keys");
    await page.evaluate((t) => localStorage.setItem("lumewarp.theme", t), theme);
    await page.reload();
    await expect(page.getByRole("heading", { name: "My keys", level: 1 })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await shot(page, `s8-my-keys-${label}`, { settle: 300 });

    const section = page.getByTestId("team-s8-shots");
    await section.getByRole("button", { name: "New key" }).click();
    const form = page.getByRole("dialog", { name: "New key in s8-shots" });
    await form.getByLabel("Name").fill("s8-shot-new");
    await form.getByLabel("Expires").fill(isoDay(daysFromNow(3)));
    await shot(page, `s8-my-keys-create-${label}`, { fullPage: false, settle: 400 });
    await form.getByRole("button", { name: "Create key" }).click();
    const secret = page.getByRole("dialog", { name: "API key created" });
    await expect(secret.getByTestId("secret-value")).toBeVisible();
    await shot(page, `s8-my-keys-secret-${label}`, { fullPage: false, settle: 400 });
    await secret.getByRole("button", { name: /saved it/ }).click();

    const row = (await myKeyRow(page, "s8-shots", "s8-shots-key"));
    await row.getByRole("button", { name: "Rotate" }).click();
    const rotate = page.getByRole("dialog", { name: /^Rotate/ });
    await pick(rotate, "Old secret", "24 hours");
    await shot(page, `s8-my-keys-rotate-${label}`, { fullPage: false, settle: 400 });
    await rotate.getByRole("button", { name: "Cancel" }).click();

    // The empty state of a user on no team, in the same four modes.
    const outsiderContext = await browser.newContext({ viewport: { width, height } });
    await outsiderContext.addInitScript((t) => localStorage.setItem("lumewarp.theme", t), theme);
    await outsiderContext.addCookies([{ name: "lumewarp_session", value: OUTSIDER, url: test.info().project.use.baseURL! }]);
    const outsider = await outsiderContext.newPage();
    await outsider.goto("/my-keys");
    await expect(outsider.getByTestId("no-teams")).toBeVisible();
    expect(await outsider.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await shot(outsider, `s8-my-keys-no-team-${label}`, { settle: 300 });
    await outsiderContext.close();
  }
});
