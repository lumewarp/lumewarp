import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

import { confirmDialog, pick } from "./helpers";
import { Admin, rowOf, signIn } from "./selfService";

/*
 * Installation admins: add and remove an admin from the page, a plain admin sees no
 * Transfer, and break-glass transfers the proxy_admin role. serve.py seeds two users
 * that have signed in once for this spec; no other spec uses them.
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

async function asBreakGlass(page: Page, request: APIRequestContext) {
  await page.context().addCookies((await request.storageState()).cookies);
  await page.goto("/installation-admins");
  await expect(page.getByRole("heading", { name: "Installation admins", level: 1 })).toBeVisible();
}

test("installation admin is added and removed from the page", async ({ page, request }) => {
  await asBreakGlass(page, request);
  await page.getByRole("button", { name: "Add admin" }).click();
  const dialog = page.getByRole("dialog");
  await pick(dialog, "User", INSTALL_ADMIN_EMAIL);
  await dialog.getByRole("button", { name: "Add admin" }).click();
  await expect(dialog).toHaveCount(0);
  const row = rowOf(page, INSTALL_ADMIN_EMAIL);
  await expect(row).toContainText("admin");
  const listed = await admin.get<{ items: { email: string; role: string }[] }>("/installation-admins?limit=200");
  expect(listed.items.find((item) => item.email === INSTALL_ADMIN_EMAIL)?.role).toBe("admin");

  await row.getByRole("button", { name: "Remove" }).click();
  await confirmDialog(page).getByRole("button", { name: "Remove" }).click();
  await expect(row).toHaveCount(0);
});

test("a plain installation admin sees no Transfer action", async ({ page, request }) => {
  const user = await admin.user(INSTALL_ADMIN_EMAIL);
  const assigned = await request.put(`/api/v1/installation-admins/${user.id}`, { headers: admin.headers, data: { role: "admin" } });
  expect(assigned.status()).toBe(200);
  await signIn(page, INSTALL_ADMIN, "/installation-admins");
  await expect(page.getByRole("heading", { name: "Installation admins", level: 1 })).toBeVisible();
  const row = rowOf(page, INSTALL_ADMIN_EMAIL);
  await expect(row.getByRole("button", { name: "Remove" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Transfer proxy_admin" })).toHaveCount(0);
});

test("break-glass transfers the proxy_admin role", async ({ page, request }) => {
  const target = await admin.user(INSTALL_TARGET_EMAIL);
  const assigned = await request.put(`/api/v1/installation-admins/${target.id}`, { headers: admin.headers, data: { role: "admin" } });
  expect(assigned.status()).toBe(200);
  await asBreakGlass(page, request);
  const row = rowOf(page, INSTALL_TARGET_EMAIL);
  await row.getByRole("button", { name: "Transfer proxy_admin" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: "Transfer" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(row).toContainText("proxy_admin");
  // The proxy_admin row has no actions; the role cannot be removed, only transferred. The
  // e2e database has no way back to "no proxy_admin", and nothing else uses this user.
  await expect(row.getByRole("button", { name: "Remove" })).toHaveCount(0);
  const listed = await admin.get<{ items: { email: string; role: string }[] }>("/installation-admins?limit=200");
  expect(listed.items.find((item) => item.email === INSTALL_TARGET_EMAIL)?.role).toBe("proxy_admin");
});
