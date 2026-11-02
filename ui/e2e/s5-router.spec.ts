import { expect, test } from "@playwright/test";

import { dialog, pick } from "./helpers";

/* The router form: labels and descriptions reach the control they belong to. */

const BREAK_GLASS = "e2e-break-glass-password";

test("router form: the fallback label opens its picker, and hints describe the picker and the models group", async ({ page, request }) => {
  const response = await request.post("/api/v1/session/break-glass", { data: { password: BREAK_GLASS } });
  expect(response.status()).toBe(204);
  await page.context().addCookies((await request.storageState()).cookies);
  await page.goto("/routers");
  await page.getByRole("button", { name: "New router" }).click();
  const d = dialog(page);

  const picker = d.getByRole("combobox", { name: "Add fallback model", exact: true });
  await expect(picker).toHaveAccessibleDescription(/Tried in order once this router/);
  await d.getByText("Fallback model (optional)", { exact: true }).click();
  await expect(page.getByRole("combobox", { name: "Search fallback models" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(picker).toHaveAttribute("aria-expanded", "false");

  await pick(d, "Strategy", "Least in flight");
  await expect(d.getByRole("group", { name: "Models", exact: true })).toHaveAccessibleDescription("This strategy does not use weights.");
});
