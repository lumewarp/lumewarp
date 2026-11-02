import { expect, test, type APIRequestContext, type Browser, type Locator, type Page } from "@playwright/test";

import { confirmDialog, pick, SHOTS, shot } from "./helpers";

/*
 * Self-service keys as a regular member against the real control plane. serve.py seeds the users and
 * their cookie sessions (a login here is OIDC or break-glass, so a spec cannot sign a member in any
 * other way); every team, membership and key is made through the API in the test that needs it.
 *
 * The viewer is a plain member, `member` is a second plain member of the same team, `teamadmin` is an
 * admin of one team only, and `outsider` is on no team at all.
 */

const BREAK_GLASS = "e2e-break-glass-password";
const VIEWER = "e2e-viewer-session-0123456789abcdef";
const MEMBER = "e2e-member-session-0123456789abcdef";
const TEAM_ADMIN = "e2e-teamadmin-session-0123456789abcdef";
const OUTSIDER = "e2e-outsider-session-0123456789abcdef";
const SHOTS_TOKEN = "e2e-shots-session-0123456789abcdef";
const VIEWER_EMAIL = "viewer@e2e.test";
const MEMBER_EMAIL = "member@e2e.test";
const TEAM_ADMIN_EMAIL = "teamadmin@e2e.test";
const SHOTS_EMAIL = "shots@e2e.test";
const WEEK = 604_800;

const admin: Record<string, string> = {};

test.beforeEach(async ({ request }) => {
  const response = await request.post("/api/v1/session/break-glass", { data: { password: BREAK_GLASS } });
  expect(response.status()).toBe(204);
  admin["X-Lumewarp-CSRF"] = (await (await request.get("/api/v1/session")).json()).csrf_token;
});

async function post<T = { id: string; name: string }>(request: APIRequestContext, path: string, data: object): Promise<T> {
  const response = await request.post(`/api/v1${path}`, { headers: admin, data });
  expect(response.status(), await response.text()).toBe(201);
  return (await response.json()) as T;
}

/** Idempotent: a later test may run alone or after the one that first made the team and the membership. */
async function ensureTeam(
  request: APIRequestContext,
  name: string,
  limits: { max_keys_per_member?: number; max_member_key_lifetime_seconds?: number } = {},
) {
  const response = await request.get("/api/v1/teams?limit=200", { headers: admin });
  expect(response.status(), await response.text()).toBe(200);
  const found = ((await response.json()) as { items: { id: string; name: string }[] }).items.find((team) => team.name === name);
  if (found) return found;
  return post(request, "/teams", { name, ...limits });
}

async function putMember(request: APIRequestContext, teamId: string, email: string, role: "admin" | "member" = "member") {
  const response = await request.get("/api/v1/users?limit=200", { headers: admin });
  expect(response.status(), await response.text()).toBe(200);
  const user = ((await response.json()) as { items: { id: string; email: string }[] }).items.find((item) => item.email === email);
  expect(user, `${email} exists`).toBeTruthy();
  const put = await request.put(`/api/v1/teams/${teamId}/members/${user!.id}`, { headers: admin, data: { role } });
  expect(put.ok(), await put.text()).toBe(true);
}

/** A cookie session's API context: the only way a spec can act as a member (no password, no OIDC). */
async function memberSession(browser: Browser, token: string) {
  const baseURL = test.info().project.use.baseURL!;
  const context = await browser.newContext({ baseURL });
  await context.addCookies([{ name: "lumewarp_session", value: token, url: baseURL }]);
  const session = await (await context.request.get("/api/v1/session")).json();
  expect(session.authenticated).toBe(true);
  return { context, csrf: session.csrf_token as string };
}

async function memberKey(browser: Browser, token: string, teamId: string, name: string) {
  const { context, csrf } = await memberSession(browser, token);
  try {
    const response = await context.request.post(`/api/v1/teams/${teamId}/keys`, {
      headers: { "X-Lumewarp-CSRF": csrf },
      data: { name },
    });
    expect(response.status(), await response.text()).toBe(201);
    return (await response.json()) as { secret: string; key: { id: string; display_prefix: string } };
  } finally {
    await context.close();
  }
}

async function memberRotate(browser: Browser, token: string, keyId: string, graceSeconds: number) {
  const { context, csrf } = await memberSession(browser, token);
  try {
    const response = await context.request.post(`/api/v1/keys/${keyId}/rotate`, {
      headers: { "X-Lumewarp-CSRF": csrf },
      data: { grace_seconds: graceSeconds },
    });
    expect(response.status(), await response.text()).toBe(200);
  } finally {
    await context.close();
  }
}

async function signIn(page: Page, token: string, path = "/") {
  await page.context().addCookies([{ name: "lumewarp_session", value: token, url: test.info().project.use.baseURL! }]);
  await page.goto(path);
}

const isoDay = (date: Date) => date.toISOString().slice(0, 10);
const daysFromNow = (days: number) => new Date(Date.now() + days * 86_400_000);
const rowOf = (scope: Page | Locator, name: string) => scope.getByRole("row").filter({ hasText: name });

test("a regular member lands on My keys and sees only their teams and own keys", async ({ page, browser, request }) => {
  const mine = await ensureTeam(request, "s8-mine");
  const elsewhere = await ensureTeam(request, "s8-elsewhere");
  await putMember(request, mine.id, VIEWER_EMAIL);
  await putMember(request, mine.id, MEMBER_EMAIL);
  await putMember(request, elsewhere.id, MEMBER_EMAIL);
  await memberKey(browser, VIEWER, mine.id, "s8-viewer-key");
  await memberKey(browser, MEMBER, mine.id, "s8-other-key");
  await memberKey(browser, MEMBER, elsewhere.id, "s8-hidden-key");

  await signIn(page, VIEWER, "/");
  await expect(page).toHaveURL(/\/my-keys$/);
  await expect(page.getByRole("heading", { name: "My keys", level: 1 })).toBeVisible();
  const section = page.getByTestId("team-s8-mine");
  await expect(rowOf(section, "s8-viewer-key")).toBeVisible();
  await expect(section).not.toContainText("s8-other-key");
  await expect(page.getByTestId("team-s8-elsewhere")).toHaveCount(0);
  await expect(page.getByText("s8-hidden-key")).toHaveCount(0);
  // The navigation shows the self-service page first and no admin page for a plain member.
  const nav = page.getByRole("navigation", { name: "Main" });
  await expect(nav.getByRole("link").first()).toHaveText("My keys");
  await expect(nav.getByRole("link", { name: "Teams", exact: true })).toHaveCount(0);
  await expect(nav.getByRole("link", { name: "Users", exact: true })).toHaveCount(0);

  // The home entry was replaced: Back does not land on / (and bounce forward again).
  await page.goBack();
  await expect(page).not.toHaveURL(/\/my-keys$/);
  await page.goForward();
  await expect(page).toHaveURL(/\/my-keys$/);
});

test("create with expiry: the secret is shown once and gone after closing", async ({ page, request }) => {
  const team = await ensureTeam(request, "s8-keys");
  await putMember(request, team.id, VIEWER_EMAIL);
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
  await expect(rowOf(section, "s8-created")).toBeVisible();
});

test("the create dialog refuses an expiry past the team's limit with the API's message", async ({ page, request }) => {
  const team = await ensureTeam(request, "s8-limit", { max_member_key_lifetime_seconds: WEEK });
  await putMember(request, team.id, VIEWER_EMAIL);
  await signIn(page, VIEWER);

  const section = page.getByTestId("team-s8-limit");
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

test("rotate with a one hour grace, then with no grace", async ({ page, browser, request }) => {
  const team = await ensureTeam(request, "s8-keys");
  await putMember(request, team.id, VIEWER_EMAIL);
  const created = await memberKey(browser, VIEWER, team.id, "s8-rotate-key");
  await signIn(page, VIEWER);

  const section = page.getByTestId("team-s8-keys");
  const row = rowOf(section, "s8-rotate-key");
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
  await expect(section.getByTestId("grace")).toContainText(/Old secret works until/i);

  // With no grace the old secret stops at once and the row no longer claims a grace.
  await row.getByRole("button", { name: "Rotate" }).click();
  const second = page.getByRole("dialog", { name: /^Rotate/ });
  await expect(second.getByTestId("rotate-notice")).toHaveText("The old secret stops working right away.");
  await second.getByRole("button", { name: "Rotate key" }).click();
  const rotatedAgain = page.getByRole("dialog", { name: "API key rotated" });
  await expect(rotatedAgain.getByTestId("secret-note")).toHaveText("The previous secret stopped working immediately.");
  await rotatedAgain.getByRole("button", { name: /saved it/ }).click();
  await expect(section.getByTestId("grace")).toHaveCount(0);
});

test("revoke a key after a confirmation", async ({ page, browser, request }) => {
  const team = await ensureTeam(request, "s8-keys");
  await putMember(request, team.id, VIEWER_EMAIL);
  await memberKey(browser, VIEWER, team.id, "s8-revoked-key");
  await signIn(page, VIEWER);

  const section = page.getByTestId("team-s8-keys");
  const row = rowOf(section, "s8-revoked-key");
  await row.getByRole("button", { name: "Revoke" }).click();
  await expect(confirmDialog(page).getByText(/stop working immediately/i)).toBeVisible();
  await confirmDialog(page).getByRole("button", { name: "Revoke" }).click();
  await expect(row.getByText("Revoked", { exact: true })).toBeVisible();
  await expect(row.getByRole("button", { name: "Rotate" })).toHaveCount(0);
});

test("a 409 from the per-member cap is shown as text", async ({ page, browser, request }) => {
  const team = await ensureTeam(request, "s8-cap", { max_keys_per_member: 1 });
  await putMember(request, team.id, VIEWER_EMAIL);
  await memberKey(browser, VIEWER, team.id, "s8-cap-first");
  await signIn(page, VIEWER);

  const section = page.getByTestId("team-s8-cap");
  await expect(section).toContainText("1 of 1 keys");
  await section.getByRole("button", { name: "New key" }).click();
  const form = page.getByRole("dialog", { name: "New key in s8-cap" });
  await form.getByLabel("Name").fill("s8-cap-second");
  await form.getByRole("button", { name: "Create key" }).click();
  await expect(form.getByRole("alert")).toContainText("member key limit reached (1 per member in this team)");
  await expect(form).toBeVisible();
  await form.getByRole("button", { name: "Cancel" }).click();
});

test("a hidden page opened by URL explains instead of failing", async ({ page, request }) => {
  const team = await ensureTeam(request, "s8-keys");
  await putMember(request, team.id, VIEWER_EMAIL);
  await signIn(page, VIEWER);

  await page.goto("/users");
  await expect(page.getByRole("heading", { name: "You do not have access to this page" })).toBeVisible();
  await expect(page.getByTestId("no-access")).toBeVisible();
  await expect(page.getByTestId("state-error")).toHaveCount(0);
  await expect(page.getByRole("table")).toHaveCount(0);
  await page.getByRole("link", { name: "Go to My keys" }).click();
  await expect(page).toHaveURL(/\/my-keys$/);
});

test("an unavailable authorization backend is a notice, not 'no access'", async ({ page, request }) => {
  const team = await ensureTeam(request, "s8-keys");
  await putMember(request, team.id, VIEWER_EMAIL);
  await signIn(page, VIEWER);
  await page.route("**/api/v1/users/me", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({
      status: response.status(),
      contentType: "application/json",
      body: JSON.stringify({ ...body, authz_unavailable: true }),
    });
  });
  await page.goto("/users");
  await expect(page.getByTestId("authz-unavailable")).toBeVisible();
  await expect(page.getByTestId("authz-blocked")).toBeVisible();
  await expect(page.getByRole("heading", { name: "You do not have access to this page" })).toHaveCount(0);
  await page.goto("/my-keys");
  await expect(page.getByTestId("authz-unavailable")).toBeVisible();
  await expect(page.getByTestId("team-s8-keys")).toBeVisible();
});

test("a user on no team sees an empty state, not an empty table", async ({ page }) => {
  await signIn(page, OUTSIDER, "/");
  await expect(page).toHaveURL(/\/my-keys$/);
  await expect(page.getByTestId("no-teams")).toContainText("Ask an administrator to add you to a team");
  await expect(page.getByRole("table")).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Overview", exact: true })).toHaveCount(0);
});

test("a team admin's own keys rotate and revoke; the admin key view lists the team", async ({ page, browser, request }) => {
  const team = await ensureTeam(request, "s8-admin");
  await putMember(request, team.id, TEAM_ADMIN_EMAIL, "admin");
  await putMember(request, team.id, VIEWER_EMAIL);
  await memberKey(browser, TEAM_ADMIN, team.id, "s8-admin-key");
  await memberKey(browser, VIEWER, team.id, "s8-admin-other");

  await signIn(page, TEAM_ADMIN, "/");
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  await page.goto("/my-keys");
  const section = page.getByTestId("team-s8-admin");
  const row = rowOf(section, "s8-admin-key");
  await expect(row).toBeVisible();
  await expect(section).not.toContainText("s8-admin-other");

  await row.getByRole("button", { name: "Rotate" }).click();
  const rotate = page.getByRole("dialog", { name: /^Rotate/ });
  await rotate.getByRole("button", { name: "Rotate key" }).click();
  const secret = page.getByRole("dialog", { name: "API key rotated" });
  await expect(secret.getByTestId("secret-value")).toHaveText(/^lw-/);
  await secret.getByRole("button", { name: /saved it/ }).click();
  await row.getByRole("button", { name: "Revoke" }).click();
  await confirmDialog(page).getByRole("button", { name: "Revoke" }).click();
  await expect(row.getByText("Revoked", { exact: true })).toBeVisible();

  // The admin view lists every key in the team, including another member's.
  await page.getByRole("link", { name: "Keys", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Keys", level: 1 })).toBeVisible();
  await expect(rowOf(page, "s8-admin-other")).toBeVisible();
  await expect(rowOf(page, "s8-admin-key")).toBeVisible();
});

test("a team admin sets the member key limits from the team settings", async ({ page, request }) => {
  const team = await ensureTeam(request, "s8-limits");
  await putMember(request, team.id, TEAM_ADMIN_EMAIL, "admin");
  await signIn(page, TEAM_ADMIN, `/teams/${team.id}`);
  await page.getByRole("tab", { name: "Settings" }).click();
  await expect(page.getByText("Keys per member")).toBeVisible();
  await page.getByRole("button", { name: "Edit team" }).click();
  const form = page.getByRole("dialog", { name: "Edit team settings" });
  await form.getByLabel("Max keys per member").fill("3");
  await form.getByLabel("Longest member key lifetime (seconds)").fill("3600");
  await form.getByRole("button", { name: "Save" }).click();
  await expect(form).toBeHidden();
  const saved = await (await request.get(`/api/v1/teams/${team.id}`, { headers: admin })).json();
  expect(saved.max_keys_per_member).toBe(3);
  expect(saved.max_member_key_lifetime_seconds).toBe(3600);
  await expect(page.getByText("1 hour", { exact: true })).toBeVisible();
  await page.goto("/my-keys");
  await expect(page.getByTestId("team-s8-limits")).toContainText("0 of 3 keys");
  await expect(page.getByTestId("team-s8-limits")).toContainText("Longest lifetime: 1 hour");
});

test("screenshots: My keys, no team, the secret and the rotate dialog", async ({ page, browser, request }) => {
  test.skip(!SHOTS, "set E2E_SHOTS to a directory to save screenshots");
  test.setTimeout(240_000);
  const team = await ensureTeam(request, "s8-shots", { max_keys_per_member: 20, max_member_key_lifetime_seconds: WEEK });
  await putMember(request, team.id, SHOTS_EMAIL);
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

    const row = rowOf(section, "s8-shots-key");
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
