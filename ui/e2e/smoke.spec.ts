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

test("admin flow against the real control plane", async ({ page, browser, request }) => {
  // Unauthenticated deep links go to login; a bad token is rejected inline.
  await page.goto("/keys");
  await expect(page).toHaveURL(/\/login$/);
  await login(page, "not-a-token");
  await expect(page.getByRole("alert")).toBeVisible();

  await login(page, BOOTSTRAP);
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  await expect(page.locator(".principal")).toContainText("superuser");

  // org -> team -> project -> key
  await page.getByRole("link", { name: "Organizations" }).click();
  await page.getByRole("button", { name: "New organization" }).click();
  await dialog(page).getByLabel("Name").fill("Acme");
  await dialog(page).getByRole("button", { name: "Create" }).click();
  await expect(page.getByRole("cell", { name: "Acme" })).toBeVisible();

  await page.getByRole("link", { name: "Teams" }).click();
  await page.getByRole("button", { name: "New team" }).click();
  await dialog(page).getByLabel("Name").fill("Research");
  await dialog(page).getByRole("button", { name: "Create" }).click();
  await expect(page.getByRole("cell", { name: "Research" })).toBeVisible();

  await page.getByRole("link", { name: "Projects" }).click();
  await page.getByRole("button", { name: "New project" }).click();
  await dialog(page).getByLabel("Name").fill("Chatbot");
  await dialog(page).getByRole("button", { name: "Create" }).click();
  await expect(page.getByRole("cell", { name: "Chatbot" })).toBeVisible();

  await page.getByRole("link", { name: "API keys" }).click();
  await page.getByRole("button", { name: "New key" }).click();
  await dialog(page).getByLabel("Name").fill("prod-key");
  await dialog(page).getByRole("button", { name: "Create key" }).click();
  const secret = (await dialog(page).locator(".secret-value").textContent()) ?? "";
  expect(secret).toMatch(/^lw-/);
  await dialog(page).getByRole("button", { name: /saved it/ }).click();
  await expect(page.getByRole("cell", { name: /prod-key/ })).toBeVisible();
  // The secret is shown once: it is nowhere on the page or after a reload.
  await page.reload();
  await expect(page.getByRole("cell", { name: /prod-key/ })).toBeVisible();
  expect(await page.content()).not.toContain(secret);

  // A child cannot exceed its parent: the 422 lands on the field.
  await page.getByRole("link", { name: "Organizations" }).click();
  await page.getByRole("button", { name: "Edit" }).click();
  await dialog(page).getByLabel("Requests per minute").fill("10");
  await dialog(page).getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("cell", { name: "Acme" })).toBeVisible();
  await page.getByRole("link", { name: "Teams" }).click();
  await page.getByRole("button", { name: "Edit" }).click();
  await dialog(page).getByLabel("Requests per minute").fill("100");
  await dialog(page).getByRole("button", { name: "Save" }).click();
  await expect(dialog(page).locator(".field-error")).toContainText("rpm_limit");
  await dialog(page).getByRole("button", { name: "Cancel" }).click();

  // A viewer on the team sees disabled actions, and the server refuses writes.
  const users = (await (await request.get("/api/v1/users", { headers: admin })).json()).items as { id: string; email: string }[];
  const viewer = users.find((u) => u.email === "viewer@e2e.test")!;
  const orgs = (await (await request.get("/api/v1/orgs", { headers: admin })).json()).items as { id: string }[];
  const teams = (await (await request.get(`/api/v1/orgs/${orgs[0].id}/teams`, { headers: admin })).json()).items as { id: string }[];
  const projects = (await (await request.get(`/api/v1/teams/${teams[0].id}/projects`, { headers: admin })).json()).items as { id: string }[];
  const grant = await request.post("/api/v1/memberships", { headers: admin, data: { user_id: viewer.id, org_id: orgs[0].id, team_id: teams[0].id, role: "viewer" } });
  expect(grant.ok()).toBeTruthy();

  const vctx = await browser.newContext({ baseURL: page.url().split("/").slice(0, 3).join("/") });
  const vpage = await vctx.newPage();
  await login(vpage, VIEWER);
  await vpage.getByRole("link", { name: "API keys" }).click();
  await expect(vpage.getByRole("cell", { name: /prod-key/ })).toBeVisible();
  await expect(vpage.getByRole("button", { name: "New key" })).toBeDisabled();
  await expect(vpage.getByRole("button", { name: "Revoke" })).toBeDisabled();
  await expect(vpage.getByRole("link", { name: "Gateways" })).toHaveCount(0);
  const denied = await vctx.request.post(`/api/v1/projects/${projects[0].id}/keys`, { headers: { Authorization: `Bearer ${VIEWER}` }, data: { name: "sneaky" } });
  expect(denied.status()).toBe(403);
  await vctx.close();

  // Revoke the key as admin.
  await page.getByRole("link", { name: "API keys" }).click();
  await page.getByRole("button", { name: "Revoke" }).click();
  await dialog(page).getByRole("button", { name: "Revoke" }).click();
  await expect(page.locator("tr", { hasText: "prod-key" }).getByText("Revoked")).toBeVisible();

  // Usage renders (empty), and every other page loads without an error state.
  await page.getByRole("link", { name: "Usage" }).click();
  await expect(page.getByRole("heading", { name: "Usage" })).toBeVisible();
  await expect(page.getByText("No usage in this period.")).toBeVisible();
  for (const [link, heading] of [
    ["Models / Routes", "Models / Routes"],
    ["Deployments", "Deployments"],
    ["Upstreams", "Upstreams"],
    ["Audit log", "Audit log"],
    ["Users & access", "Users & access"],
    ["Gateways", "Gateways"],
    ["Config versions", "Config versions"],
    ["Settings", "Settings"],
  ]) {
    await page.getByRole("link", { name: link, exact: true }).click();
    await expect(page.getByRole("heading", { name: heading, level: 1 })).toBeVisible();
    await expect(page.locator(".state-error")).toHaveCount(0);
  }

  // Deep link + refresh keep the session and render the right page; unknown API paths stay JSON.
  await page.goto("/teams");
  await expect(page.getByRole("heading", { name: "Teams", level: 1 })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("cell", { name: "Research" })).toBeVisible();
  const missing = await request.get("/api/v1/nope", { headers: admin });
  expect(missing.status()).toBe(404);
  expect((await missing.json()).error.code).toBe("not_found");

  // Logout.
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login$/);
  await page.goto("/keys");
  await expect(page).toHaveURL(/\/login$/);
});
