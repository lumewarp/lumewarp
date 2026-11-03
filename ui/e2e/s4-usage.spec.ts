import { expect, test, type Page } from "@playwright/test";

import { optionNames, pick } from "./helpers";

/*
 * Usage filters on the shared ComboboxField: the free-text mode of Upstream model (typed text is the value,
 * suggestions only complete it) and the Combobox filters around it. serve.py seeds `use-*` / `use/*`.
 */

const BREAK_GLASS = "e2e-break-glass-password";
const usageRequests = (page: Page) => page.waitForRequest((r) => r.url().includes("/api/v1/usage") && r.url().includes("upstream_model="));

test.beforeEach(async ({ page, request }) => {
  const response = await request.post("/api/v1/session/break-glass", { data: { password: BREAK_GLASS } });
  expect(response.status()).toBe(204);
  await page.context().addCookies((await request.storageState()).cookies);
  await expect
    .poll(async () => ((await (await request.get("/api/v1/usage?group_by=model")).json()).items as unknown[]).length)
    .toBeGreaterThan(0);
  await page.goto("/usage");
  await expect(page.getByRole("heading", { name: "Usage" })).toBeVisible();
});

const upstream = (page: Page) => page.getByRole("combobox", { name: "Upstream model", exact: true });

test("upstream model free text: one control for the label, a suggestion fills it, any typed text is the value", async ({ page }) => {
  await expect(upstream(page)).toHaveCount(1);
  await expect(page.getByLabel("Upstream model", { exact: true })).toHaveCount(1);
  await expect(upstream(page)).toHaveValue("");

  // Picking a suggestion writes its text and queries with it.
  await upstream(page).click();
  const [picked] = await Promise.all([usageRequests(page), page.getByRole("option", { name: "Qwen/UseA", exact: true }).click()]);
  expect(new URL(picked.url()).searchParams.get("upstream_model")).toBe("Qwen/UseA");
  await expect(upstream(page)).toHaveValue("Qwen/UseA");
  await expect(upstream(page)).toHaveAttribute("aria-expanded", "false");

  // Typing narrows the suggestions, and a retired name nobody suggests is still the value.
  await upstream(page).fill("Org/Ret");
  await expect(page.getByRole("option")).toHaveCount(0);
  const [typed] = await Promise.all([usageRequests(page), upstream(page).fill("Org/Retired")]);
  expect(new URL(typed.url()).searchParams.get("upstream_model")).toBe("Org/Retired");
  await expect(upstream(page)).toHaveValue("Org/Retired");

  // Clearing goes back to all (the unfiltered query may come from the cache, so look at the rows).
  const cell = (name: string) => page.getByRole("cell", { name, exact: true });
  await expect(cell("use/a")).toHaveCount(0);
  await upstream(page).fill("");
  await expect(cell("use/a")).toBeVisible();
  await expect(cell("use/retired")).toBeVisible();
});

test("upstream model free text: the input keeps what was typed, spaces included; the request is trimmed as before", async ({ page }) => {
  const [req] = await Promise.all([usageRequests(page), upstream(page).fill("  Org/Retired  ")]);
  await expect(upstream(page)).toHaveValue("  Org/Retired  ");
  expect(new URL(req.url()).searchParams.get("upstream_model")).toBe("Org/Retired");
});

test("upstream model free text: arrows move through suggestions, Enter picks the highlighted one or keeps the typed text, Esc closes", async ({ page }) => {
  const box = upstream(page);
  await box.click();
  const options = page.getByRole("option");
  // The suggestions come from the models list: the page also loads its usage, so wait for them.
  await expect(options.first()).toBeVisible();
  const count = await options.count();
  expect(count).toBeGreaterThan(1);
  await box.press("ArrowDown");
  await expect(options.first()).toHaveAttribute("aria-selected", "true");
  const first = (await options.first().textContent())!;
  await box.press("Enter");
  await expect(box).toHaveValue(first);
  await expect(box).toHaveAttribute("aria-expanded", "false");
  await expect(box).toBeFocused();

  // Enter with nothing highlighted never replaces the typed text with a suggestion.
  await box.fill("Qwen");
  await expect(options.first()).toBeVisible();
  await box.press("Enter");
  await expect(box).toHaveValue("Qwen");
  await expect(box).toHaveAttribute("aria-expanded", "false");

  await box.press("ArrowDown");
  await expect(box).toHaveAttribute("aria-expanded", "true");
  await box.press("Escape");
  await expect(box).toHaveAttribute("aria-expanded", "false");
  await expect(box).toHaveValue("Qwen");
});

for (const [where, width] of [["desktop", 1280], ["390 px", 390]] as const) {
  test(`upstream model free text (${where}): the highlighted suggestion of a long list stays in view and Enter takes it`, async ({ page }) => {
    await page.setViewportSize({ width, height: 700 });
    // Only the suggestions are long: answered here so the shared catalog stays as the other tests expect it.
    await page.route(/\/api\/v1\/models(\?|$)/, async (route) => {
      const response = await route.fetch();
      const body = (await response.json()) as { items: Record<string, unknown>[] };
      const extra = Array.from({ length: 65 }, (_, i) => ({ ...body.items[0], id: `long-${i}`, upstream_model: `Long/${String(i).padStart(2, "0")}` }));
      await route.fulfill({ response, json: { ...body, items: [...body.items, ...extra] } });
    });
    await page.reload();
    const box = upstream(page);
    await box.fill("Long/");
    const options = page.getByRole("option");
    await expect(options).toHaveCount(65);
    const inView = async (name: string) => {
      const list = await page.getByRole("listbox", { name: "Suggestions" }).boundingBox();
      const row = await page.getByRole("option", { name, exact: true }).boundingBox();
      return row!.y >= list!.y - 1 && row!.y + row!.height <= list!.y + list!.height + 1;
    };
    // From nothing highlighted, ArrowUp goes to the last entry, far below the first screen.
    await box.press("ArrowUp");
    await expect(page.getByRole("option", { name: "Long/64", exact: true })).toHaveAttribute("aria-selected", "true");
    await expect.poll(() => inView("Long/64")).toBe(true);
    // It wraps round to the first, and walking down keeps the highlight in view.
    await box.press("ArrowDown");
    await expect(options.first()).toHaveAttribute("aria-selected", "true");
    await expect.poll(() => inView("Long/00")).toBe(true);
    for (let i = 0; i < 20; i++) await box.press("ArrowDown");
    await expect(page.getByRole("option", { name: "Long/20", exact: true })).toHaveAttribute("aria-selected", "true");
    await expect.poll(() => inView("Long/20")).toBe(true);
    await expect.poll(() => box.getAttribute("aria-activedescendant")).toBe(await page.getByRole("option", { name: "Long/20", exact: true }).getAttribute("id"));
    await box.press("Enter");
    await expect(box).toHaveValue("Long/20");
  });
}

test("usage filters: Team, Model, Alias and Router are searchable comboboxes that keep their 'all' row", async ({ page }) => {
  for (const [label, all] of [["Team", "All teams"], ["Model", "All models"], ["Alias", "Any"], ["Router", "Any"]] as const) {
    await expect(page.getByRole("combobox", { name: label, exact: true })).toHaveText(all);
    expect((await optionNames(page, label))[0]).toBe(all);
  }
  const [req] = await Promise.all([page.waitForRequest((r) => r.url().includes("/api/v1/usage") && r.url().includes("model=use%2Fa")), pick(page, "Model", "use/a")]);
  expect(new URL(req.url()).searchParams.get("model")).toBe("use/a");
  await pick(page, "Model", "All models");
  await expect(page.getByRole("combobox", { name: "Model", exact: true })).toHaveText("All models");
});
