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
