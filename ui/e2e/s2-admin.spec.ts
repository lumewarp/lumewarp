import { expect, test, type Page } from "@playwright/test";
import { join } from "node:path";

import { deleteTeams, pick } from "./helpers";

/* Audit, Gateways, Settings, Users and Config after the move to shadcn components. */

const BREAK_GLASS = "e2e-break-glass-password";
const SHOTS = process.env.E2E_SHOTS;
const admin: Record<string, string> = {};

test.beforeEach(async ({ request }) => {
  const response = await request.post("/api/v1/session/break-glass", { data: { password: BREAK_GLASS } });
  expect(response.status()).toBe(204);
  const session = await (await request.get("/api/v1/session")).json();
  admin["X-Lumewarp-CSRF"] = session.csrf_token;
});

// This spec's teams (s2-*) go again, so a later spec's Teams page is not pushed off its first page.
test.afterEach(async ({ request }) => {
  await deleteTeams(request, admin, "s2-");
});

async function signIn(page: Page, path: string) {
  await page.goto("/login");
  await page.getByLabel("Break-glass password").fill(BREAK_GLASS);
  await Promise.all([
    page.waitForResponse((r) => new URL(r.url()).pathname === "/api/v1/session/break-glass"),
    page.getByRole("button", { name: "Sign in" }).click(),
  ]);
  await page.goto(path);
}

test("audit: the Team filter is a combobox that sends team_id, and the log narrows to that team", async ({ page, request }) => {
  const team = await (await request.post("/api/v1/teams", { headers: admin, data: { name: "s2-audit-team" } })).json();
  await request.post("/api/v1/teams", { headers: admin, data: { name: "s2-audit-other" } });
  const cell = (name: string) => page.getByRole("cell", { name, exact: true }).first();
  await signIn(page, "/audit");
  await expect(page.getByRole("heading", { name: "Audit log" })).toBeVisible();
  await expect(cell("s2-audit-team")).toBeVisible();
  await expect(cell("s2-audit-other")).toBeVisible();
  const filtered = page.waitForRequest((r) => new URL(r.url()).pathname === "/api/v1/audit" && new URL(r.url()).searchParams.get("team_id") === team.id);
  await pick(page, "Team", "s2-audit-team");
  await filtered;
  await expect(page.getByRole("combobox", { name: "Team" })).toContainText("s2-audit-team");
  await expect(cell("s2-audit-other")).toHaveCount(0);
  await expect(cell("s2-audit-team")).toBeVisible();
  // The unfiltered query is cached, so clearing may not hit the network: assert the control, not a request.
  await pick(page, "Team", "All teams");
  await expect(page.getByRole("combobox", { name: "Team" })).toContainText("All teams");
  await expect(cell("s2-audit-other")).toBeVisible();
  await expect(cell("s2-audit-team")).toBeVisible();
});

test("settings and users: description list, buttons and pager render and work", async ({ page }) => {
  await signIn(page, "/settings");
  await expect(page.getByRole("heading", { name: "Session" })).toBeVisible();
  const theme = page.getByRole("main").getByRole("button", { name: /^Theme:/ });
  const before = await page.locator("html").getAttribute("data-theme");
  await theme.click();
  await expect(page.locator("html")).not.toHaveAttribute("data-theme", before ?? "");
  await page.goto("/users");
  await expect(page.getByRole("heading", { name: "Users" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Previous" })).toBeDisabled();
  await expect(page.getByText(/Showing \d+ users/)).toBeVisible();
});

test("screenshots: s2 pages, desktop and phone, light and dark", async ({ page, request }) => {
  test.skip(!SHOTS, "screenshots only with E2E_SHOTS");
  await request.post("/api/v1/teams", { headers: admin, data: { name: "s2-shot-team" } });
  await signIn(page, "/");
  const modes: [string, number, number, string][] = [
    ["desktop-light", 1280, 900, "light"],
    ["desktop-dark", 1280, 900, "dark"],
    ["phone-light", 390, 844, "light"],
    ["phone-dark", 390, 844, "dark"],
  ];
  for (const [label, width, height, theme] of modes) {
    await page.setViewportSize({ width, height });
    await page.evaluate((t) => localStorage.setItem("lumewarp.theme", t), theme);
    for (const name of ["audit", "gateways", "settings", "users", "config"]) {
      await page.goto(`/${name}`);
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      await page.waitForTimeout(400);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), `${label} ${name} overflows`).toBe(true);
      await page.screenshot({ path: join(SHOTS!, `s2-${label}-${name}.png`), fullPage: true });
    }
    await page.goto("/audit");
    await (await page.getByRole("combobox", { name: "Team" }).click(), page.waitForTimeout(400));
    await page.screenshot({ path: join(SHOTS!, `s2-${label}-audit-combobox-open.png`) });
    await page.keyboard.press("Escape");
  }
});
