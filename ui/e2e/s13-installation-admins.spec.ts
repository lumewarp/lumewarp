import { expect, test, type APIRequestContext, type Locator, type Page } from "@playwright/test";

import { confirmDialog } from "./helpers";
import { Admin, rowOf, signIn } from "./selfService";

/*
 * Installation admins are managed on the Users page: make and remove an admin from a user
 * row, a user who never signed in cannot be made one, a plain admin sees no Transfer, and
 * break-glass transfers the proxy_admin role. serve.py seeds two users that have signed in
 * once for this spec; no other spec uses them.
 */

const INSTALL_ADMIN = "e2e-installadmin-session-0123456789abcdef";
const INSTALL_ADMIN_EMAIL = "installadmin@e2e.test";
const INSTALL_TARGET_EMAIL = "installtarget@e2e.test";

let admin: Admin;

async function removeIfAdmin(request: APIRequestContext, email: string) {
  const rows = await admin.get<{ items: { user_id: string; email: string; role: string }[] }>("/installation-admins?limit=200");
  const row = rows.items.find((item) => item.email === email && item.role === "admin");
  if (row) {
    const response = await request.delete(`/api/v1/installation-admins/${row.user_id}`, { headers: admin.headers });
    expect(response.status()).toBe(204);
  }
}

test.beforeEach(async ({ request }) => {
  admin = await Admin.login(request);
  await removeIfAdmin(request, INSTALL_ADMIN_EMAIL);
});

test.afterEach(async ({ request }) => {
  await removeIfAdmin(request, INSTALL_ADMIN_EMAIL);
});

async function asBreakGlass(page: Page, request: APIRequestContext, path = "/users") {
  await page.context().addCookies((await request.storageState()).cookies);
  await page.goto(path);
  await expect(page.getByRole("heading", { name: "Users", level: 1 })).toBeVisible();
}

/** The user's row on the paged Users list, paging forward until it shows. */
async function userRow(page: Page, email: string): Promise<Locator> {
  const row = rowOf(page, email);
  for (;;) {
    await expect(page.getByRole("row").nth(1)).toBeVisible();
    if (await row.count()) return row;
    const next = page.getByRole("button", { name: "Next page" });
    await expect(next, `${email} is on some page`).toBeEnabled();
    await next.click();
  }
}

test("an installation admin is made and removed from the Users page", async ({ page, request }) => {
  await asBreakGlass(page, request);
  const row = await userRow(page, INSTALL_ADMIN_EMAIL);
  await row.getByRole("button", { name: "Make admin" }).click();
  await confirmDialog(page).getByRole("button", { name: "Make admin" }).click();
  await expect(row.getByRole("cell", { name: "admin", exact: true })).toBeVisible();
  const listed = await admin.get<{ items: { email: string; role: string }[] }>("/installation-admins?limit=200");
  expect(listed.items.find((item) => item.email === INSTALL_ADMIN_EMAIL)?.role).toBe("admin");

  // "Admins only" lists exactly the admins, and the old page address lands there.
  await page.goto("/installation-admins");
  await expect(page).toHaveURL(/\/users\?admins=true/);
  await expect(page.getByRole("switch", { name: "Admins only" })).toBeChecked();
  const adminRow = rowOf(page, INSTALL_ADMIN_EMAIL);
  await adminRow.getByRole("button", { name: "Remove admin" }).click();
  await confirmDialog(page).getByRole("button", { name: "Remove admin" }).click();
  await expect(adminRow).toHaveCount(0);
});

test("a user who never signed in cannot be made an admin", async ({ page, request }) => {
  const email = `never-${Date.now()}@e2e.test`;
  const created = await request.post("/api/v1/users", { headers: admin.headers, data: { email, display_name: "Never" } });
  expect(created.status()).toBe(201);
  const user = (await created.json()) as { id: string };
  try {
    await asBreakGlass(page, request);
    const row = await userRow(page, email);
    await expect(row.getByRole("button", { name: "Make admin" })).toBeDisabled();
  } finally {
    await request.delete(`/api/v1/users/${user.id}`, { headers: admin.headers });
  }
});

test("a plain installation admin sees no Transfer action", async ({ page, request }) => {
  const user = await admin.user(INSTALL_ADMIN_EMAIL);
  const assigned = await request.put(`/api/v1/installation-admins/${user.id}`, { headers: admin.headers, data: { role: "admin" } });
  expect(assigned.status()).toBe(200);
  await signIn(page, INSTALL_ADMIN, "/users?admins=true");
  await expect(page.getByRole("heading", { name: "Users", level: 1 })).toBeVisible();
  const row = rowOf(page, INSTALL_ADMIN_EMAIL);
  await expect(row.getByRole("button", { name: "Remove admin" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Transfer proxy_admin" })).toHaveCount(0);
});

test("break-glass transfers the proxy_admin role", async ({ page, request }) => {
  const target = await admin.user(INSTALL_TARGET_EMAIL);
  const assigned = await request.put(`/api/v1/installation-admins/${target.id}`, { headers: admin.headers, data: { role: "admin" } });
  expect(assigned.status()).toBe(200);
  await asBreakGlass(page, request, "/users?admins=true");
  const row = rowOf(page, INSTALL_TARGET_EMAIL);
  await row.getByRole("button", { name: "Transfer proxy_admin" }).click();
  await confirmDialog(page).getByRole("button", { name: "Transfer" }).click();
  await expect(row.getByRole("cell", { name: "proxy_admin", exact: true })).toBeVisible();
  // The proxy_admin row has no admin actions; the role cannot be removed, only transferred.
  // The e2e database has no way back to "no proxy_admin", and nothing else uses this user.
  await expect(row.getByRole("button", { name: "Remove admin" })).toHaveCount(0);
  const listed = await admin.get<{ items: { email: string; role: string }[] }>("/installation-admins?limit=200");
  expect(listed.items.find((item) => item.email === INSTALL_TARGET_EMAIL)?.role).toBe("proxy_admin");
});
