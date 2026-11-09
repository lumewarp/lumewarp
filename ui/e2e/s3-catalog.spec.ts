import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

import { dialog, pick } from "./helpers";

/*
 * The Combobox inside a plain Dialog: the alias form's "Points to" (wave 0 could only test it in a Sheet).
 * Desktop and 390 px, a 65 entry list, search, keyboard, Esc, focus return and the payload.
 */

const BREAK_GLASS = "e2e-break-glass-password";
const admin: Record<string, string> = {};

test.beforeEach(async ({ request }) => {
  const response = await request.post("/api/v1/session/break-glass", { data: { password: BREAK_GLASS } });
  expect(response.status()).toBe(204);
  admin["X-Lumewarp-CSRF"] = (await (await request.get("/api/v1/session")).json()).csrf_token;
});

test.afterEach(async ({ request }) => {
  const seeded = (name: string) => /^use[-/]/.test(name);
  for (const path of ["/aliases", "/routers", "/models", "/upstreams"]) {
    const rows = (await (await request.get(`/api/v1${path}?limit=200`, { headers: admin })).json()).items as { id: string; name: string }[];
    for (const r of rows) if (!seeded(r.name)) await request.delete(`/api/v1${path}/${r.id}`, { headers: admin });
  }
});

async function post(request: APIRequestContext, path: string, data: object): Promise<{ id: string; name: string }> {
  const r = await request.post(`/api/v1${path}`, { headers: admin, data });
  expect(r.status(), await r.text()).toBe(201);
  return r.json();
}

async function unlocked(page: Page) {
  await expect.poll(() => page.evaluate(() => [document.body.hasAttribute("data-scroll-locked"), getComputedStyle(document.body).pointerEvents])).toEqual([false, "auto"]);
}

for (const [label, viewport] of [["desktop", { width: 1280, height: 800 }], ["390 px", { width: 390, height: 844 }]] as const) {
  test(`combobox in a plain dialog (${label}): long list, search, keyboard, Esc closes only the popover, focus returns, payload is the picked id`, async ({ page, request }) => {
    await page.setViewportSize(viewport);
    const up = await post(request, "/upstreams", { name: "s3c-up", kind: "vllm", base_url: "http://127.0.0.1:9/v1" });
    const models: { id: string; name: string }[] = [];
    for (let i = 0; i < 65; i++) models.push(await post(request, "/models", { name: `s3c/${String(i).padStart(2, "0")}`, upstream_id: up.id, upstream_model: "m", model_type: "llm" }));
    await page.context().addCookies((await request.storageState()).cookies);
    await page.goto("/aliases");
    const sent: any[] = [];
    page.on("request", (r) => {
      if (r.method() === "POST" && new URL(r.url()).pathname === "/api/v1/aliases") sent.push(JSON.parse(r.postData() ?? "null"));
    });
    const opener = page.getByRole("button", { name: "New alias" });
    await opener.click();
    const d = dialog(page);
    await expect(d).toBeVisible();
    const trigger = d.getByRole("combobox", { name: "Points to", exact: true });
    const triggerId = (await trigger.getAttribute("id"))!;
    const control = page.locator(`[id="${triggerId}"]`);

    // Esc closes only the popover; the dialog and focus on the trigger stay.
    await control.click();
    await expect(control).toHaveAttribute("aria-expanded", "true");
    const search = page.getByRole("combobox", { name: "Search Points to" });
    await expect(search).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(control).toHaveAttribute("aria-expanded", "false");
    await expect(d).toBeVisible();
    await expect(control).toBeFocused();

    // The long list: first and last entries are reachable by scrolling, the popover stays inside the viewport.
    await control.click();
    const popup = page.locator(`[id="${await control.getAttribute("aria-controls")}"]`);
    const options = popup.getByRole("option");
    await expect(options.filter({ hasText: /^s3c\// })).toHaveCount(65);
    const box = (await popup.boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(viewport.width);
    expect(box.y + box.height).toBeLessThanOrEqual(viewport.height);
    const last = popup.getByRole("option", { name: "s3c/64 (model · LLM)", exact: true });
    await last.scrollIntoViewIfNeeded();
    await expect(last).toBeInViewport();
    await popup.getByRole("option", { name: "s3c/00 (model · LLM)", exact: true }).scrollIntoViewIfNeeded();
    await expect(popup.getByRole("option", { name: "s3c/00 (model · LLM)", exact: true })).toBeInViewport();

    // Search matches the label and narrows the list; no match says so.
    await page.getByRole("combobox", { name: "Search Points to" }).fill("s3c/4");
    await expect(options.filter({ hasText: /^s3c\// })).toHaveCount(10);
    await page.getByRole("combobox", { name: "Search Points to" }).fill("nothing like this");
    await expect(popup.getByText("No model or router matches")).toBeVisible();
    await page.getByRole("combobox", { name: "Search Points to" }).fill("s3c/07");
    await expect(options).toHaveCount(1);

    // Keyboard: Enter picks the highlighted entry, the popover closes and focus is back on the trigger.
    await page.keyboard.press("Enter");
    await expect(control).toHaveAttribute("aria-expanded", "false");
    await expect(popup).toHaveCount(0);
    await expect(control).toBeFocused();
    await expect(control).toHaveText(/s3c\/07 \(model · LLM\)/);
    await expect(d).toBeVisible();

    // Arrow keys move through the list: a second pick moves the choice.
    await page.keyboard.press("Enter");
    await expect(control).toHaveAttribute("aria-expanded", "true");
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("Enter");
    await expect(control).toHaveAttribute("aria-expanded", "false");
    await expect(control).not.toHaveText(/s3c\/07 \(model · LLM\)/);
    await expect(control).toHaveText(/\(model · LLM\)/);

    // A click picks the last entry; the request carries the id, not the label.
    await pick(d, "Points to", "s3c/64 (model · LLM)");
    await d.getByLabel("Name", { exact: true }).fill("s3c-alias");
    await d.getByRole("button", { name: "Create" }).click();
    await expect(d).toHaveCount(0);
    expect(sent).toHaveLength(1);
    expect(sent[0]).toEqual({ name: "s3c-alias", target: models[64].id });
    await expect(opener).toBeFocused();
    await unlocked(page);
  });
}
