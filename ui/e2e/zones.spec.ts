import { expect, test, type Page } from "@playwright/test";

import { confirmDialog, dialog, pick } from "./helpers";

/*
 * Zones in the Admin UI: the header switcher, its colour, Manage zones (create, archive, the default zone
 * refuses) and the zone-scoped Gateways page. The second zone is created here, through the UI.
 */

const BREAK_GLASS = "e2e-break-glass-password";
const ZONE = "zdev";

async function signIn(page: Page) {
  const response = await page.request.post("/api/v1/session/break-glass", { data: { password: BREAK_GLASS } });
  expect(response.status()).toBe(204);
}

test.afterEach(async ({ page }) => {
  // Leave no live zone behind for the specs that follow.
  const csrf = await page.request.get("/api/v1/session").then((r) => r.json()).then((s) => (s as { csrf_token?: string }).csrf_token);
  await page.request.delete(`/api/v1/zones/${ZONE}`, { headers: csrf ? { "X-Lumewarp-CSRF": csrf } : {} }).catch(() => undefined);
});

test("zones: edit, switch, colour, per-zone gateways, reload, archive, default refuses", async ({ page }) => {
  await signIn(page);
  await page.goto("/gateways");
  const header = page.getByTestId("app-header");
  const switcher = page.getByTestId("zone-switcher");
  await expect(header).toHaveAttribute("data-zone", "default");
  // The zone's colour is its dot in the switcher; the header itself carries no colour bar.
  await expect(switcher.getByTestId("zone-badge-default").locator("span").first()).toHaveClass(/bg-zinc-500/);
  await expect(header).not.toHaveClass(/border-t-/);
  await expect(page.getByTestId("gateway-e2e-gw")).toBeVisible();

  // Zones are declared on the control plane: the header menu has no "New zone", and Manage zones has no create button.
  await switcher.click();
  await expect(page.getByRole("menuitem", { name: "New zone" })).toHaveCount(0);
  await page.getByRole("menuitem", { name: "Manage zones" }).click();
  await expect(page).toHaveURL(/\/zones$/);
  await expect(page.getByRole("button", { name: "New zone" })).toHaveCount(0);
  // The default zone has no token in this server; zdev has one.
  await expect(page.getByTestId("zone-no-token-default")).toContainText("No gateway token");
  await expect(page.getByTestId(`zone-no-token-${ZONE}`)).toHaveCount(0);

  // Name and colour the declared zone.
  await page.getByRole("row").filter({ hasText: ZONE }).getByRole("button", { name: "Edit" }).click();
  const form = dialog(page);
  await expect(form).toBeVisible();
  await expect(form.getByLabel("ID", { exact: true })).toHaveCount(0);
  await form.getByLabel("Name", { exact: true }).fill("Dev zone");
  await pick(form, "Colour", "Blue");
  await form.getByRole("button", { name: "Save" }).click();
  await expect(form).toHaveCount(0);

  await page.getByTestId("zone-switcher").click();
  await page.getByTestId(`zone-option-${ZONE}`).click();
  await expect(header).toHaveAttribute("data-zone", ZONE);
  await expect(switcher.getByTestId(`zone-badge-${ZONE}`).locator("span").first()).toHaveClass(/bg-blue-500/);
  await expect(switcher).toContainText("Dev zone");
  await page.goto("/gateways");
  await expect(page.getByRole("heading", { name: "Gateways", level: 1 })).toBeVisible();
  await expect(page.getByText("No gateways have connected yet")).toBeVisible();
  await expect(page.getByTestId("gateway-e2e-gw")).toHaveCount(0);

  // Config versions are asked for this zone; the rollback confirmation says it is installation-wide.
  const asked: string[] = [];
  await page.route("**/api/v1/config/versions*", (route) => {
    asked.push(new URL(route.request().url()).searchParams.get("zone") ?? "");
    const row = (version: number) => ({ version, kind: "publish", rollback_of_version: null, created_at: "2026-01-01T00:00:00Z", key_count: 0, size_bytes: 1024, digest: "abcdef0123456789", compacted_at: null, quarantined: false });
    return route.fulfill({ json: { items: [row(2), row(1)], next_cursor: null } });
  });
  await page.goto("/config");
  await expect(page.getByRole("heading", { name: "Config versions", level: 1 })).toBeVisible();
  expect(asked).toContain(ZONE);
  await page.getByRole("row").filter({ hasText: "v1" }).getByRole("button", { name: "Roll back" }).click();
  await expect(dialog(page).getByTestId("rollback-scope")).toContainText("installation-wide");
  await page.keyboard.press("Escape");
  await page.unroute("**/api/v1/config/versions*");

  // The choice survives a reload in the same tab.
  await page.reload();
  await expect(page.getByTestId("app-header")).toHaveAttribute("data-zone", ZONE);
  await expect(page.getByTestId("zone-switcher")).toContainText("Dev zone");

  // Back to the default zone through the menu: its gateways return.
  await page.getByTestId("zone-switcher").click();
  await page.getByTestId("zone-option-default").click();
  await expect(page.getByTestId("app-header")).toHaveAttribute("data-zone", "default");
  await page.goto("/gateways");
  await expect(page.getByTestId("gateway-e2e-gw")).toBeVisible();

  // Manage zones: the default zone cannot be archived, the new one can.
  await page.getByTestId("zone-switcher").click();
  await page.getByRole("menuitem", { name: "Manage zones" }).click();
  await expect(page.getByRole("heading", { name: "Zones", level: 1 })).toBeVisible();
  const defaultRow = page.getByRole("row").filter({ hasText: "default" }).first();
  await expect(defaultRow.getByRole("button", { name: "Archive" })).toBeDisabled();
  const csrf = ((await (await page.request.get("/api/v1/session")).json()) as { csrf_token: string }).csrf_token;
  const refused = await page.request.delete("/api/v1/zones/default", { headers: { "X-Lumewarp-CSRF": csrf } });
  expect(refused.status()).toBe(409);

  const row = page.getByRole("row").filter({ hasText: ZONE });
  await row.getByRole("button", { name: "Archive" }).click();
  await confirmDialog(page).getByRole("button", { name: "Archive" }).click();
  await expect(row).toHaveCount(0);
  // A tab on an archived zone falls back to the default zone.
  await expect(page.getByTestId("app-header")).toHaveAttribute("data-zone", "default");

  // Its token is still configured, so the archived zone says its gateways are refused.
  await page.getByRole("switch", { name: "Show archived" }).click();
  await expect(page.getByTestId(`zone-refused-${ZONE}`)).toContainText("Gateways refused");
});

test("zones: there is no sidebar entry for zones", async ({ page }) => {
  await signIn(page);
  await page.goto("/usage");
  await expect(page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: /zones/i })).toHaveCount(0);
});
