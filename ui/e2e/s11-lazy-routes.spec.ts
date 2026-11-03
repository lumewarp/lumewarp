import { expect, test, type Page } from "@playwright/test";

import { signIn, VIEWER } from "./selfService";

/*
 * The admin screens are their own chunks: nobody downloads them on arrival, a caller without the right to see
 * one never requests it, and a chunk that fails to arrive (a deploy replaced the hashed files) is a recoverable
 * error, not a blank page. serve.py runs the real control plane, so every chunk comes from the SPA's own
 * static handler.
 */

const BREAK_GLASS = "e2e-break-glass-password";

/** The JS files a page actually requested, by basename. */
function jsRequests(page: Page) {
  const seen: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (path.startsWith("/assets/") && path.endsWith(".js")) seen.push(path.slice("/assets/".length));
  });
  return seen;
}

/** A page chunk: the shell and the shared chunks are not pages. */
const pageChunk = (name: string) => /^(Routers|TeamDetail|Teams|Upstreams|Models|Aliases|Gateways|Config|Users|Audit|Settings)-/.test(name);

test("landing: an installation admin downloads the shell only, and a page chunk when its page is opened", async ({ page, request }) => {
  const response = await request.post("/api/v1/session/break-glass", { data: { password: BREAK_GLASS } });
  expect(response.status()).toBe(204);
  await page.context().addCookies((await request.storageState()).cookies);
  const requested = jsRequests(page);

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  await page.waitForLoadState("networkidle");
  expect(requested.filter(pageChunk), "no admin page is fetched on arrival").toEqual([]);
  expect(requested.some((name) => name.startsWith("index-")), "the entry chunk is fetched").toBe(true);

  // Opening the page fetches exactly its chunk (and whatever that chunk shares).
  await page.getByRole("link", { name: "Routers", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Routers", level: 1 })).toBeVisible();
  await expect.poll(() => requested.filter((name) => name.startsWith("Routers-")).length).toBe(1);
});

test("a chunk that fails to load is a recoverable error, never a blank page", async ({ page, request }) => {
  const response = await request.post("/api/v1/session/break-glass", { data: { password: BREAK_GLASS } });
  expect(response.status()).toBe(204);
  await page.context().addCookies((await request.storageState()).cookies);
  await page.route("**/assets/Routers-*.js", (route) => route.abort());
  await page.goto("/routers");
  const failed = page.getByTestId("page-load-failed");
  await expect(failed).toBeVisible();
  await expect(failed).toContainText("This page could not be loaded");
  // The shell is still there, so nothing is blank.
  await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();
  expect(await page.locator("#root").evaluate((root) => root.textContent!.trim().length)).toBeGreaterThan(0);

  // The action fixes it: with the chunk reachable again, a reload lands on the page.
  await page.unroute("**/assets/Routers-*.js");
  await failed.getByRole("button", { name: "Reload" }).click();
  await expect(page.getByRole("heading", { name: "Routers", level: 1 })).toBeVisible();
});

test("a caller without the right sees the no-permission state and never fetches the page chunk", async ({ browser }) => {
  const context = await browser.newContext();
  const member = await context.newPage();
  try {
    const requested = jsRequests(member);
    await signIn(member, VIEWER, "/routers");
    await expect(member.getByRole("heading", { name: "You do not have access to this page" })).toBeVisible();
    await expect(member.getByTestId("state-error")).toHaveCount(0);
    await member.waitForLoadState("networkidle");
    expect(requested.filter(pageChunk)).toEqual([]);
  } finally {
    await context.close();
  }
});

test("the control plane serves the chunks itself, with the immutable cache and a JSON 404 when one is gone", async ({ page, request }) => {
  const response = await request.post("/api/v1/session/break-glass", { data: { password: BREAK_GLASS } });
  expect(response.status()).toBe(204);
  await page.context().addCookies((await request.storageState()).cookies);
  await page.goto("/");
  await page.getByRole("link", { name: "Models", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Models", level: 1 })).toBeVisible();
  const chunk = await page.evaluate(() =>
    performance
      .getEntriesByType("resource")
      .map((entry) => new URL(entry.name).pathname)
      .find((path) => /\/assets\/Models-[^/]+\.js$/.test(path)),
  );
  expect(chunk, "the page chunk was served from /assets").toBeTruthy();
  const served = await page.request.get(chunk!);
  expect(served.status()).toBe(200);
  expect(served.headers()["cache-control"]).toBe("public, max-age=31536000, immutable");
  // What a stale tab asks for after a deploy: a 404 the browser reports to the import, not an HTML page.
  const gone = await page.request.get(chunk!.replace(/Models-[^.]+\.js$/, "Models-deadbeef.js"));
  expect(gone.status()).toBe(404);
  expect((await gone.headers())["content-type"]).toContain("application/json");
});
