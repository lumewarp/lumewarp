import { expect, test, type Page } from "@playwright/test";

const BOOTSTRAP = "e2e-bootstrap-token";
const VIEWER = "lwp-e2e-viewer-token-0123456789abcdef";
const admin = { Authorization: `Bearer ${BOOTSTRAP}` };

async function login(page: Page, token: string) {
  await page.goto("/login");
  await page.getByLabel("Access token").fill(token);
  await page.getByRole("button", { name: "Sign in" }).click();
}

const dialog = (page: Page) => page.getByRole("dialog");

test("team, member, service-account, and key flows", async ({ page, browser, request }) => {
  await page.goto("/keys");
  await expect(page).toHaveURL(/\/login$/);
  await login(page, "not-a-token");
  await expect(page.getByRole("alert")).toBeVisible();

  await login(page, BOOTSTRAP);
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  await expect(page.locator(".principal")).toContainText("superuser");
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
  await login(managedPage, BOOTSTRAP);
  await managedPage.goto(`/teams/${team.id}`);
  await expect(managedPage.getByRole("button", { name: "Add member" })).toBeDisabled();
  await expect(managedPage.getByText(/membership is managed by an external authorization system/i)).toBeVisible();
  await managedContext.close();

  await page.getByRole("button", { name: "Add member" }).click();
  await dialog(page).getByLabel("Search users by email").fill("viewer@e2e.test");
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
  await expect(page.getByText(/managed by an external authorization system/i)).toBeVisible();
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
  await expect(viewerPage.getByRole("alert")).toContainText("Superuser access is required");
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
    ["Models / Routes", "Models / Routes"],
    ["Deployments", "Deployments"],
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
  await expect(page).toHaveURL(/\/login$/);
});
