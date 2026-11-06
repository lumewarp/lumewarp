import { expect, type APIRequestContext, type Locator, type Page } from "@playwright/test";
import { join } from "node:path";

/** Where screenshots go; set by the runner, never by a spec. Unset in normal runs: nothing is written. */
export const SHOTS = process.env.E2E_SHOTS;

/**
 * Save a screenshot under E2E_SHOTS. Without it this is a no-op, so a normal `pnpm test:e2e` writes no files (and
 * test runs never write to /tmp). `settle` waits out a dialog or popover animation before the picture.
 */
export async function shot(page: Page, name: string, { fullPage = true, settle = 0 }: { fullPage?: boolean; settle?: number } = {}) {
  if (!SHOTS) return;
  if (settle) await page.waitForTimeout(settle);
  await page.screenshot({ path: join(SHOTS, `${name}.png`), fullPage });
}

/** An open Dialog or Sheet (a form). */
export const dialog = (page: Page) => page.getByRole("dialog");
/** The delete / revoke / deactivate confirmation: an alertdialog, never the loose union with `dialog`. */
export const confirmDialog = (page: Page) => page.getByRole("alertdialog");

const pageOf = (scope: Page | Locator): Page => ("goto" in scope ? scope : scope.page());

/**
 * Open the Select or Combobox labelled `label` (exact). Both are a button with role combobox that points at its
 * popup through aria-controls. While a popup is open Radix hides everything else from the accessibility tree
 * (a Dialog included), so the trigger is pinned by its id before the click and found again by that id.
 */
/**
 * The Select or Combobox labelled `label`, pinned by its id: unlike a role query it keeps resolving while a popup
 * is open and has hidden the rest of the page from the accessibility tree.
 */
export async function control(scope: Page | Locator, label: string): Promise<Locator> {
  const id = await scope.getByRole("combobox", { name: label, exact: true }).getAttribute("id");
  expect(id, `${label} has an id`).toBeTruthy();
  return pageOf(scope).locator(`[id="${id}"]`);
}

async function open(scope: Page | Locator, label: string) {
  const page = pageOf(scope);
  const trigger = await control(scope, label);
  await trigger.click();
  await expect(trigger).toHaveAttribute("aria-expanded", "true");
  const popupId = await trigger.getAttribute("aria-controls");
  expect(popupId, `${label} names its popup`).toBeTruthy();
  // The popup is portalled outside any Dialog, so it is looked up from the page.
  return { trigger, popup: page.locator(`[id="${popupId}"]`), page };
}

/**
 * Choose `optionName` in the Select or Combobox labelled `label`. Label and option are matched exactly. The
 * hidden native <select> Radix renders inside forms is never touched. One helper drives Select and Combobox,
 * so a page can move from one to the other without a spec change.
 */
export async function pick(scope: Page | Locator, label: string, optionName: string) {
  const { trigger, popup } = await open(scope, label);
  await popup.getByRole("option", { name: optionName, exact: true }).click();
  await expect(trigger).toHaveAttribute("aria-expanded", "false");
  // Gone from the DOM too: a closing popup still holds options another label could match.
  await expect(popup).toHaveCount(0);
}

/** The option names of a Select / Combobox, in order. The popup is closed again with Escape. */
export async function optionNames(scope: Page | Locator, label: string): Promise<string[]> {
  const { trigger, popup, page } = await open(scope, label);
  const names = await popup.getByRole("option").allTextContents();
  await page.keyboard.press("Escape");
  await expect(trigger).toHaveAttribute("aria-expanded", "false");
  await expect(popup).toHaveCount(0);
  return names;
}

type Items<T> = { items: T[] };

/**
 * The org a spec's teams belong to. There is no implicit default org any more, so a team needs an
 * explicit one; every spec that creates teams goes through here.
 */
export async function ensureOrg(request: APIRequestContext, headers: Record<string, string>, name = "e2e") {
  const listed = await request.get("/api/v1/orgs?limit=200", { headers });
  expect(listed.status(), await listed.text()).toBe(200);
  const items = ((await listed.json()) as Items<{ id: string; name: string }>).items;
  const found = items.find((org) => org.name === name) ?? items[0];
  if (found) return found.id;
  const created = await request.post("/api/v1/orgs", { headers, data: { name } });
  expect(created.status(), await created.text()).toBe(201);
  return ((await created.json()) as { id: string }).id;
}

/**
 * Archive the teams a spec made for itself (every team whose name starts with `prefix`), through the API.
 *
 * Archiving revokes keys, deactivates service accounts and frees the name, so a spec no longer has to
 * work around a team it cannot delete. The team row itself stays for usage history but leaves the list.
 */
export async function deleteTeams(request: APIRequestContext, headers: Record<string, string>, prefix: string): Promise<string[]> {
  const listed = await request.get("/api/v1/teams?limit=200", { headers });
  expect(listed.status(), await listed.text()).toBe(200);
  const own = ((await listed.json()) as Items<{ id: string; name: string }>).items.filter((team) =>
    team.name.startsWith(prefix),
  );
  for (const team of own) {
    const response = await request.delete(`/api/v1/teams/${team.id}`, { headers });
    expect(response.status(), `archive team ${team.name}: ${await response.text()}`).toBe(204);
  }
  return [];
}