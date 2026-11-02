import { expect, test, type Page } from "@playwright/test";

const BREAK_GLASS = "e2e-break-glass-password";
const VIEWER = "e2e-viewer-session-0123456789abcdef";
const admin: Record<string, string> = {};

test.beforeEach(async ({ request }) => {
  const response = await request.post("/api/v1/session/break-glass", { data: { password: BREAK_GLASS } });
  expect(response.status()).toBe(204);
  const session = await (await request.get("/api/v1/session")).json();
  admin["X-Lumewarp-CSRF"] = session.csrf_token;
});

async function login(page: Page, token: string) {
  await page.goto("/login");
  if (token === VIEWER) {
    await page.context().addCookies([{ name: "lumewarp_session", value: token, url: new URL(page.url()).origin }]);
    await page.goto("/");
    return;
  }
  await page.getByLabel("Break-glass password").fill(token);
  await page.getByRole("button", { name: "Sign in" }).click();
}

const dialog = (page: Page) => page.getByRole("dialog");

test("SSO availability and login errors follow session discovery", async ({ page }) => {
  let oidcEnabled = false;
  let breakGlassEnabled = false;
  await page.route("**/api/v1/session", (route) =>
    route.fulfill({ json: { oidc_enabled: oidcEnabled, break_glass_enabled: breakGlassEnabled, authenticated: false } }),
  );
  const sessionLoaded = () => page.waitForResponse((r) => new URL(r.url()).pathname === "/api/v1/session");

  let discovered = sessionLoaded();
  await page.goto("/login");
  await discovered;
  await expect(page.getByLabel("Break-glass password")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Sign in with SSO" })).toHaveCount(0);

  oidcEnabled = true;
  breakGlassEnabled = true;
  for (const [code, message] of [
    ["invalid_state", /SSO sign-in expired/i],
    ["access_denied", /not granted access/i],
    ["oidc_error", /Single sign-on failed/i],
  ] as const) {
    discovered = sessionLoaded();
    await page.goto(`/login?error=${code}&next=%2Fteams%2Freturn-here`);
    await discovered;
    await expect(page.getByRole("button", { name: "Sign in with SSO" })).toBeVisible();
    await expect(page.getByRole("alert")).toContainText(message);
  }
  // The emergency password form stays available as the secondary option.
  await expect(page.getByLabel("Break-glass password")).toBeVisible();
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.screenshot({ path: "/tmp/lumewarp-admin-login-desktop.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
  await page.screenshot({ path: "/tmp/lumewarp-admin-login-mobile.png", fullPage: true });

  await page.route("**/api/v1/session/oidc/login**", (route) =>
    route.fulfill({ status: 200, body: "Authorization request intercepted." }),
  );
  const loginRequest = page.waitForRequest(
    (request) => new URL(request.url()).pathname === "/api/v1/session/oidc/login",
  );
  await page.getByRole("button", { name: "Sign in with SSO" }).click();
  const request = await loginRequest;
  expect(new URL(request.url()).searchParams.get("next")).toBe("/teams/return-here");
  expect(request.headers().authorization).toBeUndefined();
});

test("cookie session: CSRF on writes, inline member errors, key owner state, logout", async ({ page }) => {
  const teamId = "00000000-0000-4000-8000-000000000001";
  const userId = "00000000-0000-4000-8000-000000000002";
  const inactiveId = "00000000-0000-4000-8000-000000000003";
  const email = "cookie-admin@e2e.test";
  const csrfToken = "cookie-session-csrf-token";
  let signedIn = true;
  await page.route("**/api/v1/session", (route) =>
    route.fulfill({
      json: signedIn
        ? {
            oidc_enabled: true,
            authenticated: true,
            user: { id: userId, email, display_name: "Cookie Admin", is_admin: false },
            csrf_token: csrfToken,
          }
        : { oidc_enabled: true, authenticated: false },
    }),
  );
  await page.route("**/api/v1/users/me", (route) =>
    route.fulfill({
      json: {
        kind: "session",
        user_id: userId,
        email,
        display_name: "Cookie Admin",
        is_admin: false,
        manages_grants: true,
        teams: [{ team_id: teamId, team_name: "Cookie Team", role: "admin" }],
      },
    }),
  );
  await page.route(`**/api/v1/teams/${teamId}`, (route) =>
    route.fulfill({ json: { id: teamId, name: "Cookie Team" } }),
  );
  const inactiveMember = {
    team_id: teamId,
    user_id: inactiveId,
    email: "gone@e2e.test",
    display_name: "Gone",
    role: "member",
    user_active: false,
    model_access: "inherit",
    allowed_models: [],
    rpm_limit: null,
    tpm_limit: null,
    max_parallel: null,
    budget_nano_usd: null,
    soft_budget_nano_usd: null,
  };
  const memberPosts: { csrf?: string; authorization?: string }[] = [];
  await page.route(new RegExp(`/api/v1/teams/${teamId}/members(?:\\?.*)?$`), async (route) => {
    if (route.request().method() === "POST") {
      const headers = route.request().headers();
      memberPosts.push({ csrf: headers["x-lumewarp-csrf"], authorization: headers.authorization });
      if (memberPosts.length === 1) {
        await route.fulfill({ status: 404, json: { error: { code: "user_not_found", message: "user not found" } } });
      } else if (memberPosts.length === 2) {
        await route.fulfill({ status: 409, json: { error: { code: "conflict", message: "user is already a member of this team" } } });
      } else {
        await route.fulfill({ status: 201, json: { ...inactiveMember, user_active: true } });
      }
      return;
    }
    await route.fulfill({ json: { items: [inactiveMember], next_cursor: null } });
  });
  await page.route(new RegExp(`/api/v1/teams/${teamId}/service-accounts(?:\\?.*)?$`), (route) =>
    route.fulfill({ json: { items: [], next_cursor: null } }),
  );
  await page.route(new RegExp(`/api/v1/teams/${teamId}/keys(?:\\?.*)?$`), (route) =>
    route.fulfill({
      json: {
        items: [
          {
            id: "00000000-0000-4000-8000-000000000010",
            team_id: teamId,
            owner_kind: "user",
            user_id: inactiveId,
            service_account_id: null,
            name: "orphaned-key",
            display_prefix: "lwk-orph",
            lifecycle: "suspended",
            suspended_reason: "owner_removed",
            expires_at: null,
            revoked_at: null,
            created_by_user_id: inactiveId,
            created_at: "2026-10-01T00:00:00Z",
            updated_at: "2026-10-02T00:00:00Z",
            owner_active: false,
          },
        ],
        next_cursor: null,
      },
    }),
  );

  await page.goto(`/teams/${teamId}`);
  await expect(page.getByRole("heading", { name: "Cookie Team" })).toBeVisible();
  await expect(page.locator(".principal")).toContainText("Cookie Admin");
  await expect(page.locator("tr", { hasText: "gone@e2e.test" }).getByText("Inactive")).toBeVisible();

  await page.getByRole("button", { name: "Add member" }).click();
  await dialog(page).getByLabel("Email").fill("missing@e2e.test");
  await dialog(page).getByRole("button", { name: "Add member" }).click();
  await expect(dialog(page).getByRole("alert")).toHaveText("No user with this email address exists.");
  await dialog(page).getByLabel("Email").fill("gone@e2e.test");
  await dialog(page).getByRole("button", { name: "Add member" }).click();
  await expect(dialog(page).getByRole("alert")).toHaveText("This user is already a member of this team.");
  await dialog(page).getByLabel("Email").fill("new-member@e2e.test");
  await dialog(page).getByRole("button", { name: "Add member" }).click();
  await expect(dialog(page)).toHaveCount(0);
  expect(memberPosts).toHaveLength(3);
  for (const headers of memberPosts) {
    expect(headers.csrf).toBe(csrfToken);
    expect(headers.authorization).toBeUndefined();
  }

  await page.getByRole("tab", { name: "Keys" }).click();
  const keyRow = page.locator("tr", { hasText: "orphaned-key" });
  await expect(keyRow).toContainText("owner removed from team");
  await expect(keyRow.getByRole("button", { name: "Resume" })).toBeDisabled();
  await expect(keyRow.getByRole("button", { name: "Resume" })).toHaveAttribute("title", /owner is inactive/i);

  let logoutCsrf: string | undefined;
  await page.route("**/api/v1/session/logout", async (route) => {
    logoutCsrf = route.request().headers()["x-lumewarp-csrf"];
    signedIn = false;
    await route.fulfill({ status: 204 });
  });
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login$/);
  expect(logoutCsrf).toBe(csrfToken);
  await expect(page.getByRole("button", { name: "Sign in with SSO" })).toBeVisible();
});

test("team, member, service-account, and key flows", async ({ page, browser, request }) => {
  await page.goto("/keys");
  await expect(page).toHaveURL(/\/login\?next=%2Fkeys$/);
  await login(page, "not-a-token");
  await expect(page.getByRole("alert")).toBeVisible();

  await login(page, BREAK_GLASS);
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  await expect(page.locator(".principal")).toContainText("break-glass");
  await expect(page.getByRole("link", { name: "Organizations" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Projects" })).toHaveCount(0);

  await page.getByRole("link", { name: "Users", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Users" })).toBeVisible();
  await page.getByRole("button", { name: "New user" }).click();
  await dialog(page).getByLabel("Email").fill("created-user@e2e.test");
  await dialog(page).getByLabel("Display name").fill("Created E2E User");
  await dialog(page).getByRole("button", { name: "Create user" }).click();
  const createdUser = page.locator("tr", { hasText: "created-user@e2e.test" });
  await expect(createdUser).toBeVisible();
  await createdUser.getByRole("button", { name: "Deactivate" }).click();
  await expect(dialog(page).getByText(/suspends the user’s API keys/i)).toBeVisible();
  await dialog(page).getByRole("button", { name: "Deactivate" }).click();
  await expect(createdUser.getByText("Inactive")).toBeVisible();

  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "Theme: Auto" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
  await page.screenshot({ path: "/tmp/lumewarp-users-mobile-light.png", fullPage: true });
  await page.getByRole("button", { name: "Theme: Light" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.screenshot({ path: "/tmp/lumewarp-users-mobile-dark.png", fullPage: true });
  await page.setViewportSize({ width: 1280, height: 900 });
  await expect(page.getByRole("button", { name: "Toggle navigation" })).toBeHidden();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
  await page.screenshot({ path: "/tmp/lumewarp-users-desktop-dark.png", fullPage: true });
  await page.getByRole("button", { name: "Theme: Dark" }).click();
  await page.getByRole("button", { name: "Theme: Auto" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await page.screenshot({ path: "/tmp/lumewarp-users-desktop-light.png", fullPage: true });

  await page.getByRole("link", { name: "Teams" }).click();
  await page.getByRole("button", { name: "New team" }).click();
  await dialog(page).getByLabel("Name").fill("Research");
  await dialog(page).getByRole("button", { name: "Create" }).click();
  await expect(page.getByRole("cell", { name: "Research" })).toBeVisible();
  await page.getByRole("link", { name: "Research" }).click();
  await expect(page.getByRole("tab", { name: "Members" })).toHaveAttribute("aria-selected", "true");

  const usersResponse = await request.get("/api/v1/users", { headers: admin });
  const users = (await usersResponse.json()).items as { id: string; email: string }[];
  const viewer = users.find((user) => user.email === "viewer@e2e.test")!;
  const teamsResponse = await request.get("/api/v1/teams", { headers: admin });
  const teams = (await teamsResponse.json()).items as { id: string; name: string }[];
  const team = teams.find((item) => item.name === "Research")!;

  const managedContext = await browser.newContext({ baseURL: page.url().split("/").slice(0, 3).join("/") });
  const managedPage = await managedContext.newPage();
  await managedPage.route("**/api/v1/users/me", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({
      status: response.status(),
      contentType: "application/json",
      body: JSON.stringify({ ...body, manages_grants: false }),
    });
  });
  await login(managedPage, BREAK_GLASS);
  await managedPage.goto(`/teams/${team.id}`);
  await expect(managedPage.getByRole("button", { name: "Add member" })).toBeDisabled();
  await expect(managedPage.getByRole("status").filter({ hasText: /managed by your identity provider \/ IAM/i })).toBeVisible();
  await managedContext.close();

  await page.getByRole("button", { name: "Add member" }).click();
  await dialog(page).getByLabel("Email", { exact: true }).fill("viewer@e2e.test");
  await dialog(page).getByLabel("User", { exact: true }).selectOption(viewer.id);
  await dialog(page).getByRole("button", { name: "Add member" }).click();
  await expect(page.getByRole("cell", { name: "viewer@e2e.test" })).toBeVisible();

  // A managed-external conflict disables the member controls and explains why.
  await page.route(`**/api/v1/teams/${team.id}/members/${viewer.id}`, async (route) => {
    if (route.request().method() === "PATCH") {
      await route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({ error: { code: "managed_externally", message: "membership is managed externally" } }),
      });
      return;
    }
    await route.continue();
  });
  await page.getByRole("button", { name: "Edit" }).click();
  await dialog(page).getByRole("button", { name: "Save" }).click();
  await expect(dialog(page).getByText(/managed by your identity provider \/ IAM/i)).toBeVisible();
  await dialog(page).getByRole("button", { name: "Cancel" }).click();
  await expect(page.getByRole("button", { name: "Add member" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Edit" })).toBeDisabled();
  await page.unroute(`**/api/v1/teams/${team.id}/members/${viewer.id}`);

  await page.getByRole("tab", { name: "Service accounts" }).click();
  await page.getByRole("button", { name: "New service account" }).click();
  await dialog(page).getByLabel("Name").fill("billing-agent");
  await dialog(page).getByRole("button", { name: "Create" }).click();
  await expect(page.getByRole("cell", { name: "billing-agent" })).toBeVisible();

  await page.getByRole("tab", { name: "Keys" }).click();
  await page.getByRole("button", { name: "New key" }).click();
  await dialog(page).getByLabel("Name").fill("service-key");
  await dialog(page).getByRole("button", { name: "Create key" }).click();
  const serviceSecret = (await dialog(page).locator(".secret-value").textContent()) ?? "";
  expect(serviceSecret).toMatch(/^lw-/);
  await expect(dialog(page).getByRole("button", { name: "Copy" })).toBeVisible();
  await dialog(page).getByRole("button", { name: /saved it/ }).click();
  const serviceKeyRow = page.locator("tr", { hasText: "service-key" });
  await expect(serviceKeyRow).toContainText("billing-agent");
  await expect(serviceKeyRow).toContainText("Active");
  let keyUpdateBody: Record<string, unknown> | undefined;
  await page.route("**/api/v1/keys/*", async (route) => {
    if (route.request().method() === "PATCH") {
      keyUpdateBody = route.request().postDataJSON() as Record<string, unknown>;
    }
    await route.continue();
  });
  await serviceKeyRow.getByRole("button", { name: "Edit" }).click();
  await dialog(page).getByLabel("Name").fill("service-key-renamed");
  await dialog(page).getByRole("button", { name: "Save" }).click();
  await expect(page.locator("tr", { hasText: "service-key-renamed" })).toBeVisible();
  expect(keyUpdateBody).not.toHaveProperty("expires_at");
  await page.unroute("**/api/v1/keys/*");
  await page.reload();
  await page.getByRole("tab", { name: "Keys" }).click();
  await expect(page.locator("tr", { hasText: "service-key-renamed" })).toBeVisible();
  expect(await page.content()).not.toContain(serviceSecret);

  // Team policy remains editable by an administrator.
  await page.getByRole("tab", { name: "Settings" }).click();
  await page.getByRole("button", { name: "Edit team" }).click();
  await expect(dialog(page)).toBeVisible();
  await dialog(page).getByLabel("Requests per minute").fill("100");
  await dialog(page).getByRole("button", { name: "Save" }).click();
  await expect(dialog(page)).toHaveCount(0);

  const viewerContext = await browser.newContext({ baseURL: page.url().split("/").slice(0, 3).join("/") });
  const viewerPage = await viewerContext.newPage();
  await login(viewerPage, VIEWER);
  await expect(viewerPage.getByRole("heading", { name: "Overview" })).toBeVisible();
  await expect(viewerPage.getByRole("link", { name: "Users", exact: true })).toHaveCount(0);
  await viewerPage.goto("/users");
  await expect(viewerPage.getByRole("alert")).toContainText("Admin group access is required");
  await viewerPage.goto(`/teams/${team.id}`);
  await expect(viewerPage.getByRole("heading", { name: "Research" })).toBeVisible();
  await expect(viewerPage.getByRole("button", { name: "Add member" })).toBeDisabled();
  await expect(viewerPage.getByRole("button", { name: "Edit" })).toBeDisabled();
  await viewerPage.getByRole("tab", { name: "Service accounts" }).click();
  await expect(viewerPage.getByRole("button", { name: "New service account" })).toBeDisabled();
  await viewerPage.getByRole("tab", { name: "Settings" }).click();
  await expect(viewerPage.getByRole("button", { name: "Edit team" })).toHaveCount(0);
  await expect(viewerPage.getByText("Team settings are read-only for members.")).toBeVisible();

  await viewerPage.getByRole("tab", { name: "Keys" }).click();
  await expect(viewerPage.locator("tr", { hasText: "service-key" }).getByRole("button", { name: "Revoke" })).toBeDisabled();
  await viewerPage.getByRole("button", { name: "New key" }).click();
  await dialog(viewerPage).getByLabel("Name").fill("personal-key");
  await dialog(viewerPage).getByRole("button", { name: "Create key" }).click();
  const personalSecret = (await dialog(viewerPage).locator(".secret-value").textContent()) ?? "";
  expect(personalSecret).toMatch(/^lw-/);
  await dialog(viewerPage).getByRole("button", { name: /saved it/ }).click();
  const personalRow = viewerPage.locator("tr", { hasText: "personal-key" });
  await expect(personalRow).toContainText("viewer@e2e.test");
  await expect(personalRow).toContainText("Active");
  const oldPrefix = await personalRow.locator("code").textContent();

  await viewerPage.getByRole("link", { name: "My keys" }).click();
  await expect(viewerPage.getByRole("heading", { name: "My keys" })).toBeVisible();
  await expect(viewerPage.getByRole("cell", { name: "personal-key" })).toBeVisible();
  await viewerPage.route("**/api/v1/keys/*/rotate", async (route) => {
    await route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({ error: { code: "conflict", message: "rotation unavailable" } }),
    });
  });
  await personalRow.getByRole("button", { name: "Rotate" }).click();
  await expect(viewerPage.getByText("rotation unavailable")).toBeVisible();
  await viewerPage.unroute("**/api/v1/keys/*/rotate");
  await viewerPage.getByRole("button", { name: "New key" }).click();
  await dialog(viewerPage).getByLabel("Name").fill("central-key");
  await dialog(viewerPage).getByRole("button", { name: "Create key" }).click();
  const centralSecret = (await dialog(viewerPage).locator(".secret-value").textContent()) ?? "";
  expect(centralSecret).toMatch(/^lw-/);
  await dialog(viewerPage).getByRole("button", { name: /saved it/ }).click();
  await expect(viewerPage.getByRole("cell", { name: "central-key" })).toBeVisible();

  await personalRow.getByRole("button", { name: "Rotate" }).click();
  const rotatedSecret = (await dialog(viewerPage).locator(".secret-value").textContent()) ?? "";
  expect(rotatedSecret).toMatch(/^lw-/);
  await dialog(viewerPage).getByRole("button", { name: /saved it/ }).click();
  await expect(viewerPage.locator("tr", { hasText: "personal-key" }).locator("code")).not.toHaveText(oldPrefix ?? "");
  await personalRow.getByRole("button", { name: "Revoke" }).click();
  await dialog(viewerPage).getByRole("button", { name: "Revoke" }).click();
  await expect(viewerPage.locator("tr", { hasText: "personal-key" }).getByText("Revoked")).toBeVisible();

  await viewerPage.getByRole("link", { name: "Settings" }).click();
  await expect(viewerPage.getByText("Research · member")).toBeVisible();
  await viewerPage.getByRole("link", { name: "Usage" }).click();
  await viewerPage.getByLabel("Group by").selectOption("team,owner,key");
  await expect(viewerPage.getByRole("heading", { name: "Usage" })).toBeVisible();
  await viewerPage.getByRole("link", { name: "Audit log" }).click();
  await expect(viewerPage.getByLabel("Team").locator("option", { hasText: "Research" })).toBeAttached();

  // The team view works at narrow and wide widths in both explicit themes.
  await viewerPage.setViewportSize({ width: 390, height: 844 });
  await viewerPage.goto(`/teams/${team.id}`);
  await expect(viewerPage.getByRole("tab", { name: "Members" })).toBeVisible();
  await viewerPage.getByRole("button", { name: "Toggle navigation" }).click();
  await expect(viewerPage.getByRole("link", { name: "Teams", exact: true })).toBeVisible();
  await viewerPage.getByRole("button", { name: "Toggle navigation" }).click();
  await viewerPage.getByRole("button", { name: "Theme: Auto" }).click();
  await expect(viewerPage.locator("html")).toHaveAttribute("data-theme", "light");
  await viewerPage.screenshot({ path: "/tmp/lumewarp-team-detail-mobile-light.png", fullPage: true });
  await viewerPage.getByRole("button", { name: "Theme: Light" }).click();
  await viewerPage.screenshot({ path: "/tmp/lumewarp-team-detail-mobile-dark.png", fullPage: true });
  await expect(viewerPage.locator("html")).toHaveAttribute("data-theme", "dark");
  expect(await viewerPage.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
  await viewerPage.setViewportSize({ width: 1280, height: 900 });
  await expect(viewerPage.getByRole("button", { name: "Toggle navigation" })).toBeHidden();
  await viewerPage.screenshot({ path: "/tmp/lumewarp-team-detail-desktop-dark.png", fullPage: true });
  expect(await viewerPage.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
  await viewerContext.close();

  // Remaining pages still load without stale model references.
  for (const [link, heading] of [
    ["Routers", "Routers"],
    ["Models", "Models"],
    ["Upstreams", "Upstreams"],
    ["Gateways", "Gateways"],
    ["Config versions", "Config versions"],
  ]) {
    await page.getByRole("link", { name: link, exact: true }).click();
    await expect(page.getByRole("heading", { name: heading, level: 1 })).toBeVisible();
    await expect(page.locator(".state-error")).toHaveCount(0);
  }

  await page.goto(`/teams/${team.id}`);
  await expect(page.getByRole("heading", { name: "Research" })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { name: "Research" })).toBeVisible();
  const missing = await request.get("/api/v1/nope", { headers: admin });
  expect(missing.status()).toBe(404);
  expect((await missing.json()).error.code).toBe("not_found");

  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login$/);
  await page.goto("/keys");
  await expect(page).toHaveURL(/\/login\?next=%2Fkeys$/);
});

test("router edit: retry, ordered fallback and a traffic split shown as percentages", async ({ page, request }) => {
  const post = async (path: string, data: object) => {
    const r = await request.post(`/api/v1${path}`, { headers: admin, data });
    expect(r.status(), await r.text()).toBe(201);
    return (await r.json()) as { id: string };
  };
  const upstream = await post("/upstreams", { name: "smoke-up", kind: "vllm", base_url: "http://127.0.0.1:9/v1" });
  const deps: Record<string, string> = {};
  for (const name of ["smoke-stable", "smoke-canary", "smoke-backup"]) {
    deps[name] = (await post("/models", { name, upstream_id: upstream.id, upstream_model: "m" })).id;
  }
  const main = await post("/routers", { name: "smoke-main", members: [{ model_id: deps["smoke-stable"] }] });

  await login(page, BREAK_GLASS);
  await page.getByRole("link", { name: "Routers", exact: true }).click();
  // Find the row by searching: what else exists (and on which page it lands) does not matter.
  await page.getByLabel("Search routers").fill("smoke-main");
  const row = page.getByRole("row").filter({ has: page.getByRole("cell", { name: "smoke-main", exact: true }) });
  await row.getByRole("button", { name: "Edit" }).click();
  const d = dialog(page);
  await d.getByLabel("Strategy").selectOption("vllm_load");
  await d.getByLabel("Retry failed attempts").check();
  await d.getByLabel("Max attempts").fill("3");
  await d.getByLabel("Retry on status").fill("503, 502");
  await d.getByLabel("Add fallback model").selectOption({ label: "smoke-backup" });
  await expect(d.locator(".order-row")).toHaveText([/1\. smoke-backup/]);

  await d.getByRole("button", { name: "Add split" }).click();
  await d.getByRole("button", { name: "Add split" }).click();
  const first = d.getByRole("group", { name: "Split 1" });
  const second = d.getByRole("group", { name: "Split 2" });
  await first.getByLabel("Weight", { exact: true }).fill("90");
  await first.getByRole("group", { name: "Models of split 1" }).getByLabel(/smoke-stable/).check();
  await second.getByLabel("Split name").fill("canary");
  await second.getByLabel("Weight", { exact: true }).fill("10");
  await second.getByRole("group", { name: "Models of split 2" }).getByLabel(/smoke-canary/).check();
  await expect(first.getByLabel("Share of split 1")).toHaveText("90%");
  await expect(second.getByLabel("Share of split 2")).toHaveText("10%");
  await second.getByLabel("Weight", { exact: true }).fill("30");
  await expect(first.getByLabel("Share of split 1")).toHaveText("75%");
  await expect(second.getByLabel("Share of split 2")).toHaveText("25%");
  await d.getByLabel("Sticky", { exact: true }).selectOption("header");
  await d.getByLabel("Sticky header").fill("X-Session-Id");
  await page.screenshot({ path: "/tmp/lumewarp-router-edit.png", fullPage: true });
  await d.getByRole("button", { name: "Save" }).click();
  await expect(d).toBeHidden();

  await expect(row).toContainText("stable 75%");
  await expect(row).toContainText("canary 25%");
  await expect(row).toContainText("retry ×3");
  await expect(row).toContainText("→ smoke-backup");
  const stored = await (await request.get(`/api/v1/routers/${main.id}`, { headers: admin })).json();
  expect(stored.strategy).toBe("vllm_load");
  expect(stored.retry).toEqual({ max_attempts: 3, retry_on_status: [502, 503], total_timeout_ms: 0 });
  expect(stored.fallback_model_ids).toEqual([deps["smoke-backup"]]);
  expect(stored.traffic_split).toEqual([
    { name: "stable", weight: 90, members: [{ model_id: deps["smoke-stable"], weight: 1, params: {} }] },
    { name: "canary", weight: 30, members: [{ model_id: deps["smoke-canary"], weight: 1, params: {} }] },
  ]);
  expect([stored.sticky, stored.sticky_header]).toEqual(["header", "x-session-id"]);

  // The fallback target cannot be deleted while referenced: the API error is shown.
  await page.getByRole("link", { name: "Models", exact: true }).click();
  await page.getByLabel("Search models").fill("smoke-backup");
  await page
    .getByRole("row")
    .filter({ has: page.getByRole("cell", { name: "smoke-backup", exact: true }) })
    .getByRole("button", { name: "Delete" })
    .click();
  await dialog(page).getByRole("button", { name: "Delete" }).click();
  await expect(page.getByText(/router smoke-main \(fallback\)/)).toBeVisible();
  // The blocked delete is a dialog listing the referrers; dismiss it to carry on.
  await dialog(page).getByRole("button", { name: "Got it" }).click();

  // Upstream health check and circuit breaker.
  await page.getByRole("link", { name: "Upstreams", exact: true }).click();
  await page
    .getByRole("row")
    .filter({ has: page.getByRole("cell", { name: "smoke-up", exact: true }) })
    .getByRole("button", { name: "Edit" })
    .click();
  const u = dialog(page);
  await u.getByLabel("Probe this upstream").check();
  await u.getByLabel("Probe path").fill("/health");
  await u.getByLabel("Probe interval (ms)").fill("5000");
  await u.getByLabel("Probe timeout (ms)").fill("5000");
  await u.getByRole("button", { name: "Save" }).click();
  await expect(u.getByText("Must be above 0 and below the interval")).toBeVisible();
  await u.getByLabel("Probe timeout (ms)").fill("1000");
  await u.getByLabel("Open the circuit after repeated failures").check();
  await u.getByLabel("Consecutive failures").fill("3");
  await u.getByRole("button", { name: "Save" }).click();
  await expect(u).toBeHidden();
  const up = await (await request.get(`/api/v1/upstreams/${upstream.id}`, { headers: admin })).json();
  expect(up.health_check).toEqual({ path: "/health", interval_ms: 5000, timeout_ms: 1000, unhealthy_threshold: null, healthy_threshold: null });
  expect(up.circuit_breaker).toEqual({ failure_threshold: 3, open_ms: null });
  await expect(page.getByRole("row").filter({ hasText: "smoke-up" })).toContainText("breaker 3×");
});
