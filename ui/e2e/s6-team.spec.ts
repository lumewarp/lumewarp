import { expect, test, type APIRequestContext, type Locator, type Page } from "@playwright/test";
import { join } from "node:path";

import { deleteTeams, dialog, optionNames, pick } from "./helpers";

/*
 * TeamDetail after the move to shadcn components: the section tabs (ui/tabs) are one tab stop with
 * activationMode="manual", so arrows only move focus and Enter or Space selects. An inactive panel's
 * content is not in the DOM, so leaving and returning still resets its state, as with the conditional
 * rendering it replaced. The User picker in Add member is a Combobox (previously a native select): the
 * request it produces carries the picked user id, not the label.
 */

const BREAK_GLASS = "e2e-break-glass-password";
const SHOTS = process.env.E2E_SHOTS;
const admin: Record<string, string> = {};

test.beforeEach(async ({ request }) => {
  const response = await request.post("/api/v1/session/break-glass", { data: { password: BREAK_GLASS } });
  expect(response.status()).toBe(204);
  const session = await (await request.get("/api/v1/session")).json();
  admin["X-Lumewarp-CSRF"] = session.csrf_token;
});

// This spec's teams (s6-*) go again, so a later spec's Teams page is not pushed off its first page.
test.afterEach(async ({ request }) => {
  await deleteTeams(request, admin, "s6-");
});

async function signIn(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Break-glass password").fill(BREAK_GLASS);
  await Promise.all([
    page.waitForResponse((r) => new URL(r.url()).pathname === "/api/v1/session/break-glass"),
    page.getByRole("button", { name: "Sign in" }).click(),
  ]);
}

async function post<T>(request: APIRequestContext, path: string, data: object): Promise<T> {
  const response = await request.post(`/api/v1${path}`, { headers: admin, data });
  expect(response.status(), await response.text()).toBe(201);
  return (await response.json()) as T;
}

const panelId = async (tab: Locator) => {
  const id = await tab.getAttribute("aria-controls");
  expect(id, "each tab names its panel").toBeTruthy();
  return id!;
};

for (const [where, width] of [["desktop", 1280], ["390 px", 390]] as const) {
  test(`section tabs (${where}): arrows move focus only, Enter or Space activates, an inactive panel is not mounted`, async ({ page, request }) => {
    const team = await post<{ id: string; name: string }>(request, "/teams", { name: `s6-tabs-${where}` });
    await page.setViewportSize({ width, height: 844 });
    await signIn(page);
    await page.goto(`/teams/${team.id}`);
    await expect(page.getByRole("heading", { name: team.name, level: 1 })).toBeVisible();

    const members = page.getByRole("tab", { name: "Members", exact: true });
    const accounts = page.getByRole("tab", { name: "Service accounts", exact: true });
    const keys = page.getByRole("tab", { name: "Keys", exact: true });
    const settings = page.getByRole("tab", { name: "Settings", exact: true });
    await expect(members).toHaveAttribute("aria-selected", "true");

    // One tab stop: the list itself is in the tab order, and once a tab has focus only that tab is.
    const list = page.getByRole("tablist", { name: "Team sections" });
    await expect(list).toHaveAttribute("tabindex", "0");
    for (const tab of [members, accounts, keys, settings]) await expect(tab).toHaveAttribute("tabindex", "-1");
    await members.focus();
    await expect(page.locator('[role="tab"][tabindex="0"]')).toHaveCount(1);
    await expect(members).toHaveAttribute("tabindex", "0");
    for (const other of [accounts, keys, settings]) await expect(other).toHaveAttribute("tabindex", "-1");

    // An inactive panel is not rendered. ui/tabs keeps an empty, hidden tabpanel scaffold behind each tab
    // (aria-controls must resolve), but none of the panel's content is in the DOM: the subtree is unmounted,
    // so a switch still resets its state, as with the conditional rendering it replaced.
    const membersPanel = await panelId(members);
    const accountsPanel = await panelId(accounts);
    const settingsPanel = await panelId(settings);
    const panel = (id: string) => page.locator(`[id="${id}"]`);
    await expect(page.getByRole("tabpanel")).toHaveCount(1);
    await expect(panel(membersPanel)).toBeVisible();
    await expect(panel(membersPanel)).toContainText("No members yet.");
    for (const id of [accountsPanel, settingsPanel]) {
      await expect(panel(id)).toBeHidden();
      await expect(panel(id)).toBeEmpty();
    }
    await expect(page.getByText("No service accounts yet.")).toHaveCount(0);

    // Arrows move the focus; they do not select, so the panel stays where it is.
    await page.keyboard.press("ArrowRight");
    await expect(accounts).toBeFocused();
    await expect(accounts).toHaveAttribute("aria-selected", "false");
    await expect(members).toHaveAttribute("aria-selected", "true");
    await expect(panel(accountsPanel)).toBeHidden();
    await expect(panel(accountsPanel)).toBeEmpty();
    await expect(panel(membersPanel)).toBeVisible();
    await expect(page.getByRole("tabpanel")).toContainText("No members yet.");
    await expect(page.getByRole("button", { name: "Add member" })).toBeVisible();

    // Enter selects the focused tab: its panel mounts, the previous one goes away.
    await page.keyboard.press("Enter");
    await expect(accounts).toHaveAttribute("aria-selected", "true");
    await expect(accounts).toHaveAttribute("tabindex", "0");
    await expect(members).toHaveAttribute("tabindex", "-1");
    await expect(panel(accountsPanel)).toBeVisible();
    await expect(panel(accountsPanel)).toContainText("No service accounts yet.");
    await expect(panel(membersPanel)).toBeHidden();
    await expect(panel(membersPanel)).toBeEmpty();
    await expect(page.getByText("No members yet.")).toHaveCount(0);

    // Back with the arrow keys, then Space also activates.
    await page.keyboard.press("ArrowLeft");
    await expect(members).toBeFocused();
    await expect(members).toHaveAttribute("aria-selected", "false");
    await expect(panel(membersPanel)).toBeHidden();
    await page.keyboard.press("Space");
    await expect(members).toHaveAttribute("aria-selected", "true");
    await expect(panel(membersPanel)).toBeVisible();
    await expect(panel(membersPanel)).toContainText("No members yet.");

    // Home and End move the focus too, without selecting.
    await page.keyboard.press("End");
    await expect(settings).toBeFocused();
    await expect(settings).toHaveAttribute("aria-selected", "false");
    await page.keyboard.press("Home");
    await expect(members).toBeFocused();
    // Tab leaves the tablist for the panel.
    await page.keyboard.press("Tab");
    await expect(page.getByRole("tabpanel")).toBeFocused();
    // Shift+Tab comes back to the tab that had focus, which need not be the selected one, and selects nothing.
    await page.keyboard.press("Shift+Tab");
    await expect(members).toBeFocused();
    await page.keyboard.press("End");
    await expect(settings).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(page.getByRole("tabpanel")).toBeFocused();
    await page.keyboard.press("Shift+Tab");
    await expect(settings).toBeFocused();
    await expect(settings).toHaveAttribute("aria-selected", "false");
    await expect(members).toHaveAttribute("aria-selected", "true");
    // Once more leaves the list altogether, with no second stop inside it.
    await page.keyboard.press("Shift+Tab");
    await expect.poll(() => page.evaluate(() => !!document.activeElement?.closest("[role=tablist]"))).toBe(false);

    // A panel that is left and entered again starts fresh: its content was unmounted, so state did not survive.
    const groupBy = page.getByRole("combobox", { name: "Group by", exact: true });
    await page.getByRole("tab", { name: "Usage", exact: true }).click();
    await pick(page, "Group by", "Key");
    await expect(groupBy).toContainText("Key");
    await page.getByRole("tab", { name: "Keys", exact: true }).click();
    await page.getByRole("tab", { name: "Usage", exact: true }).click();
    await expect(groupBy).toContainText("Owner");
  });
}

test("adding a member: the User Combobox sends the picked user id", async ({ page, request }) => {
  const team = await post<{ id: string; name: string }>(request, "/teams", { name: "s6-members" });
  const picked = await post<{ id: string; email: string }>(request, "/users", { email: "s6-picked@e2e.test", display_name: "S6 Picked" });
  const other = await post<{ id: string; email: string }>(request, "/users", { email: "s6-other@e2e.test", display_name: "S6 Other" });

  await signIn(page);
  // The directory for the picker is a separate query; wait for it before opening the form.
  const directory = page.waitForResponse((r) => new URL(r.url()).pathname === "/api/v1/users");
  await page.goto(`/teams/${team.id}`);
  await directory;
  await expect(page.getByRole("heading", { name: team.name, level: 1 })).toBeVisible();

  await page.getByRole("button", { name: "Add member" }).click();
  const d = dialog(page);
  await d.getByLabel("Email", { exact: true }).fill("s6-");
  // Both users match the typed address, and the picker is a combobox, not the native select it replaced.
  await expect(d.getByRole("combobox", { name: "User", exact: true })).toContainText("Select a user (optional)");
  const names = await optionNames(d, "User");
  expect(names).toContain(picked.email);
  expect(names).toContain(other.email);

  const memberPost = page.waitForRequest((r) => r.method() === "POST" && new URL(r.url()).pathname === `/api/v1/teams/${team.id}/members`);
  await pick(d, "User", picked.email);
  // Picking writes the user's email into the Email field and shows the picked user on the trigger.
  await expect(d.getByLabel("Email", { exact: true })).toHaveValue(picked.email);
  await expect(d.getByRole("combobox", { name: "User", exact: true })).toContainText(picked.email);

  await d.getByRole("button", { name: "Add member" }).click();
  const body = (await memberPost).postDataJSON() as Record<string, unknown>;
  expect(body.user_id).toBe(picked.id);
  expect(body.user_id).not.toBe(other.id);
  expect(body).not.toHaveProperty("email");
  expect(body.role).toBe("member");

  await expect(d).toHaveCount(0);
  await expect(page.getByRole("cell", { name: picked.email, exact: true })).toBeVisible();
});

test("key actions at 390 px: the table scrolls inside its card and the last action can be reached, without the page scrolling sideways", async ({ page, request }) => {
  const team = await post<{ id: string; name: string }>(request, "/teams", { name: "s6-phone-keys" });
  const account = await post<{ id: string }>(request, `/teams/${team.id}/service-accounts`, { name: "s6-phone-agent", description: "Phone" });
  await post(request, `/teams/${team.id}/keys`, { name: "s6-phone-key", service_account_id: account.id });
  await page.setViewportSize({ width: 390, height: 844 });
  await signIn(page);
  await page.goto(`/teams/${team.id}`);
  await page.getByRole("tab", { name: "Keys", exact: true }).click();
  await expect(page.getByRole("cell", { name: /s6-phone-key/ })).toBeVisible();
  const container = page.getByRole("tabpanel").locator("[data-slot=table-container]");
  expect(await container.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(true);
  for (const name of ["Rotate", "Suspend", "Revoke", "Edit"]) {
    const action = page.getByRole("tabpanel").getByRole("button", { name, exact: true });
    await action.scrollIntoViewIfNeeded();
    await expect(action).toBeInViewport({ ratio: 1 });
    await action.click({ trial: true });
  }
  expect(await container.evaluate((el) => el.scrollLeft)).toBeGreaterThan(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test("screenshots: TeamDetail tabs, desktop and phone, light and dark", async ({ page, request }) => {
  test.skip(!SHOTS, "screenshots only with E2E_SHOTS");
  const team = await post<{ id: string; name: string }>(request, "/teams", { name: "s6-shots" });
  await post(request, `/teams/${team.id}/members`, { email: "viewer@e2e.test", role: "member" });
  const account = await post<{ id: string; name: string }>(request, `/teams/${team.id}/service-accounts`, {
    name: "s6-agent",
    description: "Screenshots",
  });
  await post(request, `/teams/${team.id}/keys`, { name: "s6-key", service_account_id: account.id });
  await post(request, "/users", { email: "s6-shot-picker@e2e.test", display_name: "S6 Shot Picker" });

  await signIn(page);
  const modes: [string, number, number, string][] = [
    ["desktop-light", 1280, 900, "light"],
    ["desktop-dark", 1280, 900, "dark"],
    ["phone-light", 390, 844, "light"],
    ["phone-dark", 390, 844, "dark"],
  ];
  const tabs = ["Members", "Service accounts", "Keys", "Usage", "Settings"];
  for (const [label, width, height, theme] of modes) {
    await page.setViewportSize({ width, height });
    await page.evaluate((t) => localStorage.setItem("lumewarp.theme", t), theme);
    await page.goto(`/teams/${team.id}`);
    for (const name of tabs) {
      await page.getByRole("tab", { name, exact: true }).click();
      await expect(page.getByRole("tab", { name, exact: true })).toHaveAttribute("aria-selected", "true");
      await page.waitForTimeout(400);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), `${label} ${name} overflows`).toBe(true);
      await page.screenshot({ path: join(SHOTS!, `s6-${label}-team-${name.toLowerCase().replaceAll(" ", "-")}.png`), fullPage: true });
    }
    // The form with the Combobox the page gained this wave, open.
    await page.getByRole("tab", { name: "Members", exact: true }).click();
    await page.getByRole("button", { name: "Add member" }).click();
    await dialog(page).getByLabel("Email", { exact: true }).fill("s6-shot");
    await dialog(page).getByRole("combobox", { name: "User", exact: true }).click();
    await expect(page.getByRole("option", { name: "s6-shot-picker@e2e.test", exact: true })).toBeVisible();
    // Let the popover finish fading in (a screenshot mid-animation shows the dialog through it), and undo
    // Playwright's scroll-into-view of the trigger so the whole form stays in the picture.
    await page.waitForTimeout(400);
    await page.locator("[data-slot=dialog-content] div.overflow-y-auto").evaluate((el) => {
      el.scrollTop = 0;
    });
    await page.waitForTimeout(200);
    await page.screenshot({ path: join(SHOTS!, `s6-${label}-team-add-member-combobox.png`) });
    await page.keyboard.press("Escape");
    await expect(page.getByRole("option", { name: "s6-shot-picker@e2e.test", exact: true })).toHaveCount(0);
    await dialog(page).getByRole("button", { name: "Cancel" }).click();
    await expect(dialog(page)).toHaveCount(0);
  }
});
