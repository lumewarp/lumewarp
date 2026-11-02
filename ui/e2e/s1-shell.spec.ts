import { expect, test, type Page } from "@playwright/test";

const BREAK_GLASS = "e2e-break-glass-password";
const SHOTS = process.env.E2E_SHOTS;

test.use({ viewport: { width: 390, height: 844 } });

test.beforeEach(async ({ page, request }) => {
  const response = await request.post("/api/v1/session/break-glass", { data: { password: BREAK_GLASS } });
  expect(response.status()).toBe(204);
  await page.context().addCookies((await request.storageState()).cookies);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
});

const toggle = (page: Page) => page.getByRole("button", { name: "Toggle navigation" });

test("phone: the navigation opens, closes on Esc and returns focus", async ({ page }) => {
  await expect(page.getByRole("navigation", { name: "Main" })).toHaveCount(0);
  await expect(toggle(page)).toHaveAttribute("aria-expanded", "false");
  await toggle(page).click();
  const nav = page.getByRole("navigation", { name: "Main" });
  await expect(nav).toBeVisible();
  await expect(page.getByRole("link", { name: "Teams", exact: true })).toBeVisible();
  // Sheet and overlay sit above the sticky header, and the sheet is 15rem wide.
  const sheet = page.locator("[data-mobile=true]");
  expect((await sheet.boundingBox())?.width).toBeCloseTo(240, 0);
  const top = await page.evaluate(() => document.elementFromPoint(300, 20)?.getAttribute("data-slot"));
  expect(top).toBe("sheet-overlay");
  // Background is locked: no scrolling and the page behind is inert for assistive tech.
  expect(await page.evaluate(() => getComputedStyle(document.body).overflow)).toBe("hidden");
  await expect(page.getByRole("heading", { name: "Overview" })).toBeHidden();
  await page.keyboard.press("Escape");
  await expect(nav).toHaveCount(0);
  await expect(toggle(page)).toBeFocused();
  await expect(toggle(page)).toHaveAttribute("aria-expanded", "false");
  expect(await page.evaluate(() => getComputedStyle(document.body).overflow)).not.toBe("hidden");
});

test("phone: the Close button shuts the navigation in place and returns focus", async ({ page }) => {
  await toggle(page).click();
  const nav = page.getByRole("navigation", { name: "Main" });
  await expect(nav).toBeVisible();
  await page.getByRole("button", { name: "Close" }).click();
  await expect(nav).toHaveCount(0);
  await expect(page).toHaveURL(/\/$/);
  await expect(toggle(page)).toBeFocused();
  await expect(toggle(page)).toHaveAttribute("aria-expanded", "false");
});

test("the navigation is reachable on both sides of the breakpoint, and with a larger browser font", async ({ page }) => {
  // Below md: the toggle opens the sheet. From md up: the sidebar is there and there is no toggle.
  const assertPhone = async () => {
    await expect(toggle(page)).toBeVisible();
    await toggle(page).click();
    await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("navigation", { name: "Main" })).toHaveCount(0);
  };
  const assertDesktop = async () => {
    await expect(toggle(page)).toBeHidden();
    await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();
  };
  await page.setViewportSize({ width: 767, height: 800 });
  await assertPhone();
  await page.setViewportSize({ width: 768, height: 800 });
  await assertDesktop();
  // At a 20px browser font 48rem is 960px, so 800px is below md for CSS and has to be for the script too.
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Page.setFontSizes", { fontSizes: { standard: 20, fixed: 16 } });
  await page.setViewportSize({ width: 800, height: 800 });
  await page.reload();
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  expect(await page.evaluate(() => window.matchMedia("(min-width: 48rem)").matches)).toBe(false);
  await assertPhone();
});

test("phone: clicking the overlay or a link closes the navigation", async ({ page }) => {
  await toggle(page).click();
  await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();
  await page.mouse.click(350, 400);
  await expect(page.getByRole("navigation", { name: "Main" })).toHaveCount(0);

  await toggle(page).click();
  const link = page.getByRole("link", { name: "Routers", exact: true });
  await link.click();
  await expect(page).toHaveURL(/\/routers$/);
  await expect(page.getByRole("navigation", { name: "Main" })).toHaveCount(0);
  await expect(toggle(page)).toHaveAttribute("aria-expanded", "false");
  expect(await page.evaluate(() => getComputedStyle(document.body).overflow)).not.toBe("hidden");
  await toggle(page).click();
  await expect(page.getByRole("link", { name: "Routers", exact: true })).toHaveAttribute("aria-current", "page");
});

test("desktop: the navigation is always there, 15rem wide, with no toggle", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await expect(toggle(page)).toBeHidden();
  const nav = page.getByRole("navigation", { name: "Main" });
  await expect(nav).toBeVisible();
  expect((await page.locator("[data-slot=sidebar-container]").boundingBox())?.width).toBeCloseTo(240, 0);
  await expect(page.getByRole("link", { name: "Overview", exact: true })).toHaveAttribute("aria-current", "page");
  await page.keyboard.press("Control+b");
  await expect(nav).toBeVisible();
  expect(await page.evaluate(() => document.cookie)).not.toContain("sidebar_state");
});

test("Overview: sample usage is labelled and fits 390 px", async ({ page }) => {
  const card = page.locator("[data-slot=card]").filter({ hasText: "Usage" });
  await expect(card.getByText("Sample", { exact: true })).toBeVisible();
  await expect(card.getByText(/not real usage/i).first()).toBeVisible();
  await expect(card.getByRole("img", { name: /sample data/i })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
});

test("screenshots: shell, login and overview", async ({ page, browser }) => {
  test.skip(!SHOTS, "set E2E_SHOTS to a directory to save screenshots");
  for (const [w, h, tag] of [[1280, 900, "desktop"], [390, 844, "phone"]] as const) {
    await page.setViewportSize({ width: w, height: h });
    for (const theme of ["light", "dark"]) {
      await page.evaluate((t) => localStorage.setItem("lumewarp.theme", t), theme);
      await page.goto("/");
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
      await page.screenshot({ path: `${SHOTS}/s1-overview-${tag}-${theme}.png`, fullPage: true });
      if (tag === "phone") {
        await toggle(page).click();
        await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();
        await page.waitForTimeout(400);
        await page.screenshot({ path: `${SHOTS}/s1-nav-phone-${theme}.png` });
        await page.keyboard.press("Escape");
      }
    }
  }
  for (const [w, h, tag] of [[1280, 900, "desktop"], [390, 844, "phone"]] as const) {
    for (const theme of ["light", "dark"]) {
      const ctx = await browser.newContext({ viewport: { width: w, height: h } });
      const p = await ctx.newPage();
      await p.addInitScript((t) => localStorage.setItem("lumewarp.theme", t), theme);
      await p.goto("/login");
      await expect(p.getByLabel("Break-glass password")).toBeVisible();
      await p.screenshot({ path: `${SHOTS}/s1-login-${tag}-${theme}.png`, fullPage: true });
      await ctx.close();
    }
  }
});
