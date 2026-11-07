import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

import { confirmDialog, control, dialog, optionNames, pick } from "./helpers";

/*
 * Select, Combobox, Dialog / Sheet and the confirm dialog, through the real control plane (e2e/serve.py).
 *
 * The contract under test (src/components/choice.ts): the domain value is the only state and is what the
 * request carries; the widget only sees an index key; a value that is none of the options is shown as such and
 * left alone; an empty string is a value. Payloads are asserted on the request the form sends, not on text.
 */

const BREAK_GLASS = "e2e-break-glass-password";
const admin: Record<string, string> = {};

test.beforeEach(async ({ request }) => {
  const response = await request.post("/api/v1/session/break-glass", { data: { password: BREAK_GLASS } });
  expect(response.status()).toBe(204);
  const session = await (await request.get("/api/v1/session")).json();
  admin["X-Lumewarp-CSRF"] = session.csrf_token;
  // serve.py seeds gateways and usage after the control plane answers: wait for the strategies.
  await expect
    .poll(async () => {
      const strategies = (await (await request.get("/api/v1/strategies", { headers: admin })).json()).items as { name: string; missing_gateways: string[] }[];
      return strategies.find((s) => s.name === "e2e_tuned") !== undefined && strategies.find((s) => s.name === "priority_failover")?.missing_gateways.length === 1;
    })
    .toBe(true);
});

// Everything a test made goes away, apart from what serve.py seeded (`use-*` and `use/*`).
test.afterEach(async ({ request }) => {
  const seeded = (name: string) => /^use[-/]/.test(name);
  for (const path of ["/aliases", "/routers", "/models", "/upstreams"]) {
    const rows = (await (await request.get(`/api/v1${path}?limit=200`, { headers: admin })).json()).items as { id: string; name: string }[];
    for (const r of rows) if (!seeded(r.name)) await request.delete(`/api/v1${path}/${r.id}`, { headers: admin });
  }
});

async function post<T = { id: string; name: string }>(request: APIRequestContext, path: string, data: object): Promise<T> {
  const r = await request.post(`/api/v1${path}`, { headers: admin, data });
  expect(r.status(), await r.text()).toBe(201);
  return (await r.json()) as T;
}
async function get<T = any>(request: APIRequestContext, path: string): Promise<T> {
  const r = await request.get(`/api/v1${path}`, { headers: admin });
  expect(r.status(), await r.text()).toBe(200);
  return (await r.json()) as T;
}
async function signIn(page: Page, request: APIRequestContext, path: string) {
  await page.context().addCookies((await request.storageState()).cookies);
  await page.goto(path);
}
const upstream = (request: APIRequestContext, name: string) => post(request, "/upstreams", { name, kind: "vllm", base_url: "http://127.0.0.1:9/v1" });
const model = (request: APIRequestContext, name: string, upstream_id: string) => post(request, "/models", { name, upstream_id, upstream_model: "m" });

/** Bodies of the writes a page sends: `sent("POST", "routers")` is live, it fills as the page sends. */
function recordWrites(page: Page, method: "POST" | "PATCH", resource: string) {
  const bodies: any[] = [];
  const re = new RegExp(`/api/v1/${resource}(/[^/?]+)?$`);
  page.on("request", (r) => {
    if (r.method() === method && re.test(new URL(r.url()).pathname)) bodies.push(JSON.parse(r.postData() ?? "null"));
  });
  return bodies;
}

async function newRouter(page: Page, request: APIRequestContext, name: string, strategy = "E2E tuned") {
  const up = await upstream(request, `${name}-up`);
  const m = await model(request, `${name}/m`, up.id);
  await signIn(page, request, "/routers");
  await page.getByRole("button", { name: "New router" }).click();
  const d = dialog(page);
  await d.getByLabel("Name", { exact: true }).fill(name);
  await pick(d, "Strategy", strategy);
  await d.getByLabel(`${name}/m`, { exact: true }).check();
  await d.getByLabel("Max waiting requests").fill("7"); // the one setting this strategy requires
  return { d, up, m };
}

test("select payloads: a number enum with 0, an empty string, a boolean and a 64 entry list reach the request as typed values", async ({ page, request }) => {
  const posted = recordWrites(page, "POST", "routers");
  const { d, m } = await newRouter(page, request, "sel-typed");
  await pick(d, "Level", "0");
  await pick(d, "Ratio", "0.5");
  await pick(d, "Scope", "(empty text)");
  await pick(d, "Skip unpriced models", "Off");
  await pick(d, "Big list", "opt-63");
  await d.getByRole("button", { name: "Create" }).click();
  await expect(d).toBeHidden();
  // Typed values, not strings: 0 and 0.5 are numbers, "" is kept, false is kept.
  expect(posted).toHaveLength(1);
  expect(posted[0].params).toStrictEqual({ max_waiting: 7, level: 0, ratio: 0.5, scope: "", skip_unpriced: false, big: "opt-63" });
  expect(posted[0].members).toEqual([{ model_id: m.id, weight: 1, params: {} }]);
  const stored = (await get<{ items: any[] }>(request, "/routers")).items.find((r) => r.name === "sel-typed");
  expect(stored.params).toStrictEqual({ max_waiting: 7, level: 0, ratio: 0.5, scope: "", skip_unpriced: false, big: "opt-63" });
});

test("select payloads: Not set is not the empty string, and picking it again removes the key", async ({ page, request }) => {
  const posted = recordWrites(page, "POST", "routers");
  const { d } = await newRouter(page, request, "sel-unset");
  // Nothing chosen yet: every optional select says so, none shows an option as if it were chosen.
  for (const label of ["Level", "Scope", "Skip unpriced models", "Big list"]) await expect(await control(d, label)).toHaveText("Not set");
  await pick(d, "Scope", "(empty text)");
  await expect(await control(d, "Scope")).toHaveText("(empty text)");
  await pick(d, "Level", "5");
  await pick(d, "Level", "Not set");
  await expect(await control(d, "Level")).toHaveText("Not set");
  await pick(d, "Skip unpriced models", "On");
  await pick(d, "Skip unpriced models", "Not set");
  await d.getByRole("button", { name: "Create" }).click();
  await expect(d).toBeHidden();
  // "" stays; the ones set and then unset are not sent at all.
  expect(posted[0].params).toStrictEqual({ max_waiting: 7, scope: "" });
});

test("select contract: a stored value that is not an option is shown as such, never replaced, and blocks only what it is", async ({ page, request }) => {
  const up = await upstream(request, "sel-stale-up");
  const m = await model(request, "sel-stale/m", up.id);
  const stored = await post<any>(request, "/routers", { name: "sel-stale", strategy: "e2e_tuned", params: { max_waiting: 7, scope: "wide", level: 1 }, members: [{ model_id: m.id, weight: 1, params: {} }] });
  // The page knows a newer schema: "wide" is no longer one of the scopes.
  await page.route("**/api/v1/strategies", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.items.find((s: any) => s.name === "e2e_tuned").params_schema.properties.scope.enum = ["", "narrow"];
    await route.fulfill({ response, json: body });
  });
  const patched = recordWrites(page, "PATCH", "routers");
  await signIn(page, request, "/routers");
  await page.getByLabel("Search routers").fill("sel-stale");
  await page.getByRole("row").filter({ hasText: "sel-stale" }).getByRole("button", { name: "Edit" }).click();
  const d = dialog(page);
  await expect(await control(d, "Scope")).toHaveText("Current value not in list");
  // Opened, none of its options is marked chosen (the first one was not picked for it).
  const { popup } = await openPopup(page, d, "Scope");
  await expect(popup.getByRole("option", { selected: true })).toHaveCount(0);
  expect(await popup.getByRole("option").allTextContents()).toEqual(["Not set", "(empty text)", "narrow"]);
  await page.keyboard.press("Escape");
  await expect(await control(d, "Scope")).toHaveText("Current value not in list");
  // Saving with it present is refused, naming the value as the problem; nothing is sent and nothing is replaced.
  await d.getByLabel("Name", { exact: true }).fill("sel-stale-2");
  await d.getByRole("button", { name: "Save" }).click();
  await expect(d.getByText("Pick one of the listed values")).toBeVisible();
  await expect(await control(d, "Scope")).toHaveText("Current value not in list");
  await expect(d).toBeVisible();
  expect(patched).toEqual([]);
  expect((await get<any>(request, `/routers/${stored.id}`)).params).toStrictEqual({ max_waiting: 7, scope: "wide", level: 1 });
  // The user fixes it by choosing: only then does the request carry the settings, and only the chosen value changed.
  await pick(d, "Scope", "narrow");
  await d.getByRole("button", { name: "Save" }).click();
  await expect(d).toBeHidden();
  expect(patched).toEqual([{ name: "sel-stale-2", params: { max_waiting: 7, scope: "narrow", level: 1 } }]);
});

test("select contract: options that arrive late are chosen from by value", async ({ page, request }) => {
  const up1 = await upstream(request, "sel-a-up");
  const up2 = await upstream(request, "sel-b-up");
  await signIn(page, request, "/models");
  let release!: () => void;
  const gate = new Promise<void>((resolve) => (release = resolve));
  await page.route("**/api/v1/upstreams?**", async (route) => {
    await gate;
    await route.fallback();
  });
  const posted = recordWrites(page, "POST", "models");
  await page.reload();
  await page.getByRole("button", { name: "New model" }).click();
  const d = page.locator("#main");
  // Not loaded yet: nothing to choose from and nothing pre-selected.
  await expect(await control(d, "Upstream")).toHaveText("Select…");
  release();
  await expect.poll(async () => (await optionNames(d, "Upstream")).length).toBeGreaterThanOrEqual(2);
  await pick(d, "Upstream", "sel-b-up");
  await d.getByLabel("Name", { exact: true }).fill("sel-late");
  await d.getByLabel("Upstream model", { exact: true }).fill("m");
  await d.getByRole("button", { name: "Create" }).click();
  await expect(page).toHaveURL(/\/models$/);
  expect(posted[0].upstream_id).toBe(up2.id);
  expect(posted[0].upstream_id).not.toBe(up1.id);
});

test("select contract: when the options are reordered after a choice, the chosen value stays, not its position", async ({ page, request }) => {
  const up1 = await upstream(request, "sel-a-up");
  const up2 = await upstream(request, "sel-b-up");
  // Fake time so that the list is stale (15 s) on demand and a focus event refetches it, with no waiting.
  await page.clock.install();
  await signIn(page, request, "/models");
  let reverse = false;
  let answered = 0;
  await page.route("**/api/v1/upstreams?**", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    if (reverse) body.items.reverse();
    answered += 1;
    await route.fulfill({ response, json: body });
  });
  const posted = recordWrites(page, "POST", "models");
  await page.reload();
  await page.getByRole("button", { name: "New model" }).click();
  const d = page.locator("#main");
  await expect.poll(async () => (await optionNames(d, "Upstream")).length).toBeGreaterThanOrEqual(3);
  const before = await optionNames(d, "Upstream");
  await pick(d, "Upstream", "sel-b-up");
  const positionBefore = before.indexOf("sel-b-up");
  // Force the second fetch now, answered in the opposite order.
  reverse = true;
  const seen = answered;
  await page.clock.fastForward(20_000);
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange", { bubbles: true })));
  await expect.poll(() => answered).toBeGreaterThan(seen);
  // Wait until the reversed list is what the form offers: the same option now sits somewhere else ...
  await expect.poll(async () => (await optionNames(d, "Upstream")).indexOf("sel-b-up")).not.toBe(positionBefore);
  expect((await optionNames(d, "Upstream")).slice().reverse()).toEqual(before);
  // ... and the trigger still shows the upstream that was chosen, not whatever sits at its old position.
  await expect(await control(d, "Upstream")).toHaveText("sel-b-up");
  await d.getByLabel("Name", { exact: true }).fill("sel-reordered");
  await d.getByLabel("Upstream model", { exact: true }).fill("m");
  await d.getByRole("button", { name: "Create" }).click();
  await expect(page).toHaveURL(/\/models$/);
  expect(posted[0].upstream_id).toBe(up2.id);
  expect(posted[0].upstream_id).not.toBe(up1.id);
});

test("select contract: no options is an empty list, not a crash", async ({ page, request }) => {
  await signIn(page, request, "/models");
  await page.route("**/api/v1/upstreams?**", (route) => route.fulfill({ json: { items: [], next_cursor: null } }));
  await page.reload();
  await page.getByRole("button", { name: "New model" }).click();
  const d = page.locator("#main");
  await expect(await control(d, "Upstream")).toHaveText("Select…");
  expect(await optionNames(d, "Upstream")).toEqual([]);
  await expect(d.getByRole("heading", { name: "New model" })).toBeVisible();
});

/** Open without choosing; like `pick`'s first half. */
async function openPopup(page: Page, scope: Page | ReturnType<typeof dialog>, label: string) {
  const trigger = await control(scope, label);
  await trigger.click();
  await expect(trigger).toHaveAttribute("aria-expanded", "true");
  const popup = page.locator(`[id="${await trigger.getAttribute("aria-controls")}"]`);
  return { trigger, popup };
}

test("select: the label names its control; clicking it activates that control and Esc gives focus back to it", async ({ page, request }) => {
  await newRouter(page, request, "sel-label");
  const d = dialog(page);
  // One label, one control: each label of the form names exactly one combobox.
  for (const name of ["Strategy", "Level", "Scope", "Big list"]) await expect(d.getByRole("combobox", { name, exact: true })).toHaveCount(1);
  const scope = await control(d, "Scope");
  const label = d.locator("label", { hasText: /^Scope$/ });
  await expect(label).toHaveAttribute("for", (await scope.getAttribute("id"))!);
  await label.click();
  // Radix opens a select on the click a label forwards (a native select only takes focus): the list is the
  // control the label names, and closing it returns focus to that control.
  await expect(scope).toHaveAttribute("aria-expanded", "true");
  await page.keyboard.press("Escape");
  await expect(scope).toHaveAttribute("aria-expanded", "false");
  await expect(scope).toBeFocused();
  await expect(d).toBeVisible();
});

test("select: keyboard picks an option with Enter and ArrowDown, Home and End reach the ends of a 64 entry list", async ({ page, request }) => {
  const posted = recordWrites(page, "POST", "routers");
  const { d } = await newRouter(page, request, "sel-keys");
  const big = await control(d, "Big list");
  await big.focus();
  await page.keyboard.press("Enter");
  await expect(big).toHaveAttribute("aria-expanded", "true");
  await page.keyboard.press("End");
  // Radix moves focus on a timer; Enter before it lands would pick the old focus.
  const bigPopup = page.locator(`[id="${await big.getAttribute("aria-controls")}"]`);
  await expect(bigPopup.getByRole("option", { name: "opt-63", exact: true })).toHaveAttribute("data-highlighted", "");
  await page.keyboard.press("Enter");
  await expect(big).toHaveText("opt-63");
  await expect(big).toBeFocused();
  await page.keyboard.press("ArrowDown"); // opens again, on the chosen option
  await expect(big).toHaveAttribute("aria-expanded", "true");
  const popup = page.locator(`[id="${await big.getAttribute("aria-controls")}"]`);
  await page.keyboard.press("Home");
  await expect(popup.getByRole("option").first()).toHaveAttribute("data-highlighted", "");
  await expect(popup.getByRole("option").first()).toHaveText("Not set");
  await page.keyboard.press("ArrowDown");
  await expect(popup.getByRole("option", { name: "opt-00", exact: true })).toHaveAttribute("data-highlighted", "");
  await page.keyboard.press("Enter");
  await expect(big).toHaveText("opt-00");
  const level = await control(d, "Level");
  await level.focus();
  await page.keyboard.press("Enter");
  const levelPopup = page.locator(`[id="${await level.getAttribute("aria-controls")}"]`);
  const highlighted = (name: string) => expect(levelPopup.getByRole("option", { name, exact: true })).toHaveAttribute("data-highlighted", "");
  await highlighted("Not set");
  await page.keyboard.press("ArrowDown");
  await highlighted("0");
  await page.keyboard.press("ArrowDown");
  await highlighted("1");
  await page.keyboard.press("Enter");
  await expect(level).toHaveText("1");
  await d.getByRole("button", { name: "Create" }).click();
  await expect(d).toBeHidden();
  expect(posted[0].params.level).toBe(1);
  expect(posted[0].params.big).toBe("opt-00");
});

test("select and combobox inside a sheet: Esc closes only the top layer, focus returns, the page is not left locked", async ({ page, request }) => {
  const { d } = await newRouter(page, request, "sel-esc");
  const scope = await control(d, "Scope");
  await scope.focus();
  await page.keyboard.press("Enter");
  await expect(scope).toHaveAttribute("aria-expanded", "true");
  await page.keyboard.press("Escape");
  await expect(scope).toHaveAttribute("aria-expanded", "false");
  await expect(d).toBeVisible(); // the sheet is still there
  await expect(scope).toBeFocused();
  // The Combobox: its popover is the top layer.
  const fallback = await control(d, "Add fallback model");
  await fallback.click();
  await expect(fallback).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByRole("combobox", { name: "Search fallback models" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(fallback).toHaveAttribute("aria-expanded", "false");
  await expect(d).toBeVisible();
  await expect(fallback).toBeFocused();
  // Then the sheet itself, and focus goes back to the button that opened it.
  await page.keyboard.press("Escape");
  await expect(d).toHaveCount(0);
  await expect(page.getByRole("button", { name: "New router" })).toBeFocused();
  await expectPageUnlocked(page);
});

async function expectPageUnlocked(page: Page) {
  await expect.poll(() => page.evaluate(() => [document.body.hasAttribute("data-scroll-locked"), getComputedStyle(document.body).pointerEvents, getComputedStyle(document.body).overflow, document.body.getAttribute("aria-hidden")])).toEqual([false, "auto", "visible", null]);
  // The page behind answers a click again.
  await page.getByRole("button", { name: "New router" }).click();
  await expect(dialog(page)).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog(page)).toHaveCount(0);
}

test("select inside a dialog and a drawer: Esc, focus return and scrolling at the end of the page", async ({ page, request }) => {
  const up0 = await upstream(request, "sel-dlg-up");
  await model(request, "sel-dlg/m", up0.id);
  await signIn(page, request, "/aliases");
  await page.getByRole("button", { name: "New alias" }).click();
  const d = dialog(page);
  const up = await control(d, "Points to");
  await up.click();
  await page.keyboard.press("Escape");
  await expect(d).toBeVisible();
  await expect(up).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(d).toHaveCount(0);
  await expect(page.getByRole("button", { name: "New alias" })).toBeFocused();
  await expect.poll(() => page.evaluate(() => document.body.hasAttribute("data-scroll-locked"))).toBe(false);
  // A drawer (the router sheet) holds Selects too.
  await page.goto("/routers");
  await page.getByRole("button", { name: "New router" }).click();
  await pick(dialog(page), "Strategy", "E2E tuned");
  await page.keyboard.press("Escape");
  await expect(dialog(page)).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => document.body.hasAttribute("data-scroll-locked"))).toBe(false);
});

test("long lists at 390 px: a 65 entry Combobox on a page, a 64 entry Select and a 65 entry Combobox in a sheet reach their first and last entries", async ({ page, request }) => {
  await page.setViewportSize({ width: 390, height: 700 });
  // A 65 entry Combobox on the model page: the upstream list.
  const ups = [] as { id: string; name: string }[];
  for (let i = 0; i < 65; i++) ups.push(await upstream(request, `lng-${String(i).padStart(2, "0")}-up`));
  await signIn(page, request, "/models");
  await page.getByRole("button", { name: "New model" }).click();
  const main = page.locator("#main");
  const names = await optionNames(main, "Upstream");
  const lngs = names.filter((n) => n.startsWith("lng-"));
  expect(lngs).toHaveLength(65);
  await pick(main, "Upstream", lngs[0]);
  await expect(await control(main, "Upstream")).toHaveText(lngs[0]);
  await pick(main, "Upstream", lngs[lngs.length - 1]);
  await expect(await control(main, "Upstream")).toHaveText(lngs[lngs.length - 1]);
  // The list scrolls with the wheel too.
  const { trigger, popup } = await openPopup(page, main, "Upstream");
  const scroller = popup.locator("[cmdk-list]");
  await scroller.hover();
  await page.mouse.wheel(0, 400);
  await expect.poll(() => scroller.evaluate((el) => el.scrollTop)).toBeGreaterThan(0);
  await page.keyboard.press("Escape");
  await expect(popup).toHaveCount(0);
  await expect(trigger).toBeFocused();

  // A 64 entry Select and a 65 entry Combobox inside the router sheet.
  const up = ups[0];
  for (let i = 0; i < 65; i++) await model(request, `fb/${String(i).padStart(2, "0")}`, up.id);
  await page.goto("/routers");
  await page.getByRole("button", { name: "New router" }).click();
  const d = dialog(page);
  await pick(d, "Strategy", "E2E tuned");
  await pick(d, "Big list", "opt-00");
  await expect(await control(d, "Big list")).toHaveText("opt-00");
  await pick(d, "Big list", "opt-63");
  await expect(await control(d, "Big list")).toHaveText("opt-63");
  const fb = await control(d, "Add fallback model");
  const all = await optionNames(d, "Add fallback model");
  expect(all.filter((n) => n.startsWith("fb/"))).toHaveLength(65);
  await pick(d, "Add fallback model", "fb/00");
  await pick(d, "Add fallback model", "fb/64");
  await expect(d.getByTestId("order-row")).toHaveText([/1\. fb\/00/, /2\. fb\/64/]);
  await expect(fb).toHaveText("None — add a fallback model…");
  // Keyboard: type to narrow, arrow to the entry, Enter adds it.
  await fb.click();
  const search = page.getByRole("combobox", { name: "Search fallback models" });
  await search.fill("fb/3");
  await expect(page.getByRole("option")).toHaveCount(10);
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await expect(d.getByTestId("order-row")).toHaveCount(3);
  await expect(fb).toBeFocused();
  // The popover scrolls with the wheel inside the sheet's scroll lock, and End jumps to the last entry.
  await fb.click();
  const list = page.locator("[cmdk-list]");
  await list.hover();
  await page.mouse.wheel(0, 300);
  await expect.poll(() => list.evaluate((el) => el.scrollTop)).toBeGreaterThan(0);
  const shown = await page.getByRole("option").allTextContents();
  await page.keyboard.press("End");
  await expect(page.getByRole("option", { selected: true })).toHaveText(shown[shown.length - 1]);
  await page.keyboard.press("Escape");
  await expect(d).toBeVisible();
});

test("combobox contract: the search matches labels and never the keys, an unmatched search says so, Enter on the first match picks it", async ({ page, request }) => {
  const posted = recordWrites(page, "POST", "routers");
  const up = await upstream(request, "cb-contract-up");
  const m = await model(request, "cb-contract/m", up.id);
  for (const n of ["lead", "Upper", "i:1", "u"]) await model(request, `cb/${n}`, up.id);
  await signIn(page, request, "/routers");
  await page.getByRole("button", { name: "New router" }).click();
  const d2 = dialog(page);
  await d2.getByLabel("Name", { exact: true }).fill("cb-contract");
  await pick(d2, "Strategy", "Weighted");
  await d2.getByLabel("cb-contract/m", { exact: true }).check();
  // A fallback needs a retry policy with room for a second attempt.
  await d2.getByLabel("Retry failed attempts").check();
  await d2.getByLabel("Max attempts").fill("2");
  const fb = await control(d2, "Add fallback model");
  await fb.click();
  const search = page.getByRole("combobox", { name: "Search fallback models" });
  // A search for an index key finds nothing: keys are not searchable text.
  await search.fill("i:0");
  await expect(page.getByRole("option")).toHaveCount(0);
  await expect(page.getByText("No match")).toBeVisible();
  // Case does not matter, the match is on the label.
  await search.fill("UPPER");
  await expect(page.getByRole("option")).toHaveText(["cb/Upper"]);
  // A label that looks like a key is still just a label, and it is what is picked.
  await search.fill("cb/i:1");
  await expect(page.getByRole("option")).toHaveText(["cb/i:1"]);
  await page.keyboard.press("Enter");
  await expect(d2.getByTestId("order-row")).toHaveText([/1\. cb\/i:1/]);
  await d2.getByRole("button", { name: "Create" }).click();
  await expect(d2).toBeHidden();
  const stored = (await get<{ items: any[] }>(request, "/models")).items.find((x) => x.name === "cb/i:1");
  expect(posted[0].fallback_model_ids).toEqual([stored.id]);
  expect(posted[0].members.map((x: any) => x.model_id)).toEqual([m.id]);
});

test("sheet: full width on a phone, xl from sm up, and a long form keeps its header and footer reachable", async ({ page, request }) => {
  await upstream(request, "sheet-up");
  await signIn(page, request, "/routers");
  const widthOf = async () => (await dialog(page).boundingBox())!.width;
  // 390 px: the whole screen (the library default would be 75%).
  await page.setViewportSize({ width: 390, height: 700 });
  await page.getByRole("button", { name: "New router" }).click();
  await expect(dialog(page)).toBeVisible();
  await expect.poll(widthOf).toBe(390);
  await page.keyboard.press("Escape");
  await expect(dialog(page)).toHaveCount(0);
  // 1280 px: the router form is a wide drawer, xl (36rem = 576).
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.getByRole("button", { name: "New router" }).click();
  await expect.poll(widthOf).toBe(576);
  await page.setViewportSize({ width: 390, height: 700 });
  await expect.poll(widthOf).toBe(390);
  // Long form, short screen: scroll the body to its end; Close, Cancel and Create stay on screen.
  await pick(dialog(page), "Strategy", "E2E tuned");
  const d = dialog(page);
  const body = d.locator("div.overflow-y-auto");
  await body.evaluate((el) => (el.scrollTop = el.scrollHeight));
  await expect.poll(() => body.evaluate((el) => el.scrollTop > 0 && el.scrollTop + el.clientHeight >= el.scrollHeight - 1)).toBe(true);
  const inView = async (name: string) => {
    const box = (await d.getByRole("button", { name, exact: true }).boundingBox())!;
    return box.y >= 0 && box.y + box.height <= 700 && box.x >= 0 && box.x + box.width <= 390;
  };
  for (const name of ["Close", "Cancel", "Create"]) expect(await inView(name), `${name} on screen`).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await d.getByRole("button", { name: "Cancel" }).click();
  await expect(d).toHaveCount(0);
});

async function aliasFixture(request: APIRequestContext, name: string) {
  const up = await upstream(request, `${name}-up`);
  const m = await model(request, `${name}/m`, up.id);
  return post(request, "/aliases", { name, target: m.id });
}
async function openDelete(page: Page, name: string) {
  await page.getByLabel("Search aliases").fill(name);
  await page.getByRole("row").filter({ has: page.getByRole("cell", { name, exact: true }) }).getByRole("button", { name: "Delete" }).click();
}

test("confirm dialog: an alertdialog with focus on Cancel; backdrop does not close it, Esc and Cancel do", async ({ page, request }) => {
  await aliasFixture(request, "cf-focus");
  await signIn(page, request, "/aliases");
  await openDelete(page, "cf-focus");
  const c = confirmDialog(page);
  await expect(c).toBeVisible();
  await expect(dialog(page)).toHaveCount(0); // not a plain dialog
  await expect(c.getByRole("button", { name: "Cancel" })).toBeFocused();
  await page.mouse.click(5, 5); // the backdrop
  await expect(c).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(c).toHaveCount(0);
  await expect(page.getByRole("row").filter({ has: page.getByRole("cell", { name: "cf-focus", exact: true }) })).toBeVisible();
  await openDelete(page, "cf-focus");
  await confirmDialog(page).getByRole("button", { name: "Cancel" }).click();
  await expect(confirmDialog(page)).toHaveCount(0);
  const stored = (await get<{ items: any[] }>(request, "/aliases")).items.map((a) => a.name);
  expect(stored).toContain("cf-focus");
});

test("confirm dialog: success closes it and the row goes; a slow answer keeps it open and busy until it arrives", async ({ page, request }) => {
  await aliasFixture(request, "cf-ok");
  await aliasFixture(request, "cf-slow");
  await signIn(page, request, "/aliases");
  await openDelete(page, "cf-ok");
  await confirmDialog(page).getByRole("button", { name: "Delete" }).click();
  await expect(confirmDialog(page)).toHaveCount(0);
  await expect((await get<{ items: any[] }>(request, "/aliases")).items.map((a) => a.name)).not.toContain("cf-ok");

  let release!: () => void;
  const gate = new Promise<void>((resolve) => (release = resolve));
  await page.route("**/api/v1/aliases/*", async (route) => {
    if (route.request().method() !== "DELETE") return route.fallback();
    await gate;
    await route.fallback();
  });
  await openDelete(page, "cf-slow");
  const c = confirmDialog(page);
  await c.getByRole("button", { name: "Delete" }).click();
  // Still open, busy, and the confirm button cannot be pressed twice.
  await expect(c.getByRole("button", { name: "Working…" })).toBeDisabled();
  await page.waitForTimeout(300);
  await expect(c).toBeVisible();
  release();
  await expect(c).toHaveCount(0);
  await expect((await get<{ items: any[] }>(request, "/aliases")).items.map((a) => a.name)).not.toContain("cf-slow");
});

test("confirm dialog: a 409 stays open as Cannot delete with the referrers and focus on Got it; another error toasts and closes", async ({ page, request }) => {
  const up = await upstream(request, "cf-409-up");
  const m = await model(request, "cf-409/m", up.id);
  await post(request, "/aliases", { name: "cf-409-alias", target: m.id });
  await signIn(page, request, "/models");
  await page.getByLabel("Search models").fill("cf-409/m");
  await page.getByRole("row").filter({ has: page.getByRole("cell", { name: "cf-409/m", exact: true }) }).getByRole("button", { name: "Delete" }).click();
  const c = confirmDialog(page);
  await c.getByRole("button", { name: "Delete" }).click();
  await expect(c.getByRole("heading", { name: "Cannot delete" })).toBeVisible();
  await expect(c.getByTestId("delete-blocked")).toHaveAttribute("role", "alert");
  await expect(c.getByTestId("delete-blocked")).toContainText("alias cf-409-alias");
  await expect(c.getByRole("button")).toHaveText(["Got it"]);
  await expect(c.getByRole("button", { name: "Got it" })).toBeFocused();
  await page.mouse.click(5, 5);
  await expect(c).toBeVisible();
  await c.getByRole("button", { name: "Got it" }).click();
  await expect(c).toHaveCount(0);

  // Any other failure: a toast, and the dialog goes (it has nothing more to say).
  await page.route("**/api/v1/models/*", (route) => (route.request().method() === "DELETE" ? route.fulfill({ status: 500, json: { error: { code: "internal", message: "boom from the server" } } }) : route.fallback()));
  await page.getByRole("row").filter({ has: page.getByRole("cell", { name: "cf-409/m", exact: true }) }).getByRole("button", { name: "Delete" }).click();
  await confirmDialog(page).getByRole("button", { name: "Delete" }).click();
  await expect(confirmDialog(page)).toHaveCount(0);
  await expect(page.getByText("boom from the server")).toBeVisible();
  await expect(page.getByRole("row").filter({ has: page.getByRole("cell", { name: "cf-409/m", exact: true }) })).toBeVisible();
});

const SHOTS = process.env.E2E_SHOTS;

test("screenshots of forms, open dropdowns and confirmations", async ({ page, request }, info) => {
  test.skip(!SHOTS, "set E2E_SHOTS to a directory to save screenshots");
  info.setTimeout(240_000);
  const { join } = await import("node:path");
  const up = await upstream(request, "shot-up");
  const m = await model(request, "shot/m", up.id);
  for (let i = 0; i < 12; i++) await model(request, `shot/fb-${String(i).padStart(2, "0")}`, up.id);
  await post(request, "/aliases", { name: "shot-alias", target: m.id });
  await signIn(page, request, "/models");
  const modes: [string, number, number, "light" | "dark"][] = [
    ["desktop-light", 1280, 900, "light"],
    ["desktop-dark", 1280, 900, "dark"],
    ["phone-light", 390, 844, "light"],
    ["phone-dark", 390, 844, "dark"],
  ];
  const shot = async (name: string) => {
    await page.waitForTimeout(450);
    await page.screenshot({ path: join(SHOTS!, `${name}.png`) });
  };
  for (const [label, width, height, theme] of modes) {
    await page.setViewportSize({ width, height });
    await page.evaluate((t) => localStorage.setItem("lumewarp.theme", t), theme);
    await page.goto("/models");
    await page.getByRole("button", { name: "New model" }).click();
    await shot(`${label}-model-page`);
    await (await control(page.locator("#main"), "Upstream")).click();
    await shot(`${label}-model-page-select-open`);
    await page.keyboard.press("Escape");
    await page.goto("/upstreams/new");
    await (await control(page.locator("#main"), "Kind")).click();
    await shot(`${label}-upstream-page-select-open`);
    await page.keyboard.press("Escape");
    await page.goto("/routers");
    await page.getByRole("button", { name: "New router" }).click();
    await pick(dialog(page), "Strategy", "E2E tuned");
    await (await control(dialog(page), "Big list")).click();
    await shot(`${label}-router-sheet-enum-open`);
    await page.keyboard.press("Escape");
    await (await control(dialog(page), "Add fallback model")).click();
    await shot(`${label}-router-sheet-combobox-open`);
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "Create" }).click();
    await shot(`${label}-router-sheet-errors`);
    await page.keyboard.press("Escape");
    await page.goto("/aliases");
    await openDelete(page, "shot-alias");
    await shot(`${label}-confirm-delete`);
    await page.keyboard.press("Escape");
    await page.goto("/models");
    await page.getByLabel("Search models").fill("shot/m");
    await page.getByRole("row").filter({ has: page.getByRole("cell", { name: "shot/m", exact: true }) }).getByRole("button", { name: "Delete" }).click();
    await confirmDialog(page).getByRole("button", { name: "Delete" }).click();
    await shot(`${label}-confirm-blocked`);
    await confirmDialog(page).getByRole("button", { name: "Got it" }).click();
  }
});

test("confirm dialog: a long list of referrers scrolls inside the dialog at 390 px; header and Got it stay on screen", async ({ page, request }) => {
  await page.setViewportSize({ width: 390, height: 700 });
  const up = await upstream(request, "cf-long-up");
  const m = await model(request, "cf-long/m", up.id);
  const longName = "cf-long-" + "x".repeat(60);
  const names = Array.from({ length: 60 }, (_, i) => `cf-long-alias-${String(i).padStart(2, "0")}`);
  names[59] = longName;
  for (const name of names) await post(request, "/aliases", { name, target: m.id });
  await signIn(page, request, "/models");
  await page.getByLabel("Search models").fill("cf-long/m");
  await page.getByRole("row").filter({ has: page.getByRole("cell", { name: "cf-long/m", exact: true }) }).getByRole("button", { name: "Delete" }).click();
  const c = confirmDialog(page);
  await c.getByRole("button", { name: "Delete" }).click();
  const blocked = c.getByTestId("delete-blocked");
  await expect(blocked).toBeVisible();
  const inView = async (loc: ReturnType<typeof c.locator>) => {
    const box = (await loc.boundingBox())!;
    return box.y >= 0 && box.y + box.height <= 700 && box.x >= 0 && box.x + box.width <= 390;
  };
  // The dialog fits the screen; only the list scrolls; the title and Got it are never pushed out.
  const box = (await c.boundingBox())!;
  expect(box.height).toBeLessThanOrEqual(700);
  expect(await blocked.evaluate((el) => el.scrollHeight > el.clientHeight)).toBe(true);
  expect(await inView(c.getByRole("heading", { name: "Cannot delete" }))).toBe(true);
  expect(await inView(c.getByRole("button", { name: "Got it" }))).toBe(true);
  // The last referrer (a 60 character name) is reachable by scrolling the list, and wraps instead of overflowing.
  await blocked.evaluate((el) => (el.scrollTop = el.scrollHeight));
  const last = c.getByRole("listitem").filter({ hasText: longName });
  await expect(last).toBeVisible();
  expect(await last.evaluate((el) => {
    const list = el.closest("[data-testid=delete-blocked]")!.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    return r.top >= list.top - 1 && r.bottom <= list.bottom + 1;
  })).toBe(true);
  expect(await blocked.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true);
  expect(await inView(c.getByRole("button", { name: "Got it" }))).toBe(true);
  await c.getByRole("button", { name: "Got it" }).click();
  await expect(c).toHaveCount(0);
});

async function openModelDelete(page: Page, name: string) {
  await page.getByLabel("Search models").fill(name);
  await page.getByRole("row").filter({ has: page.getByRole("cell", { name, exact: true }) }).getByRole("button", { name: "Delete" }).click();
}

test("confirm dialog: an answer to a request that was cancelled does not touch the next dialog (success)", async ({ page, request }) => {
  const a = await aliasFixture(request, "cf-stale-a");
  await aliasFixture(request, "cf-stale-b");
  await signIn(page, request, "/aliases");
  let release!: () => void;
  const gate = new Promise<void>((resolve) => (release = resolve));
  await page.route(`**/api/v1/aliases/${a.id}`, async (route) => {
    if (route.request().method() !== "DELETE") return route.fallback();
    await gate;
    await route.fallback();
  });
  await openDelete(page, "cf-stale-a");
  await confirmDialog(page).getByRole("button", { name: "Delete" }).click();
  await expect(confirmDialog(page).getByRole("button", { name: "Working…" })).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(confirmDialog(page)).toHaveCount(0);
  // Another row, while the first request is still out: a fresh dialog, not busy.
  await openDelete(page, "cf-stale-b");
  const c = confirmDialog(page);
  await expect(c.getByRole("heading", { name: "Delete alias" })).toBeVisible();
  await expect(c.getByRole("button", { name: "Delete" })).toBeEnabled();
  const answered = page.waitForResponse((r) => r.request().method() === "DELETE" && r.url().endsWith(`/aliases/${a.id}`));
  release();
  await answered;
  await expect.poll(async () => (await get<{ items: any[] }>(request, "/aliases")).items.map((a) => a.name)).not.toContain("cf-stale-a");
  // A's success did not close B's dialog, and B is still there to be decided.
  await page.waitForTimeout(300);
  await expect(c).toBeVisible();
  await expect(c.getByRole("button", { name: "Delete" })).toBeEnabled();
  await c.getByRole("button", { name: "Cancel" }).click();
  expect((await get<{ items: any[] }>(request, "/aliases")).items.map((a) => a.name)).toContain("cf-stale-b");
});

test("confirm dialog: a 409 for a cancelled request does not turn the next dialog into Cannot delete, whether it arrives while it is open or after", async ({ page, request }) => {
  const up = await upstream(request, "cf-s409-up");
  const a = await model(request, "cf-s409/a", up.id);
  await model(request, "cf-s409/b", up.id);
  await post(request, "/aliases", { name: "cf-s409-alias", target: a.id });
  await signIn(page, request, "/models");
  let gate!: Promise<void>;
  let release!: () => void;
  const arm = () => (gate = new Promise<void>((resolve) => (release = resolve)));
  arm();
  await page.route(`**/api/v1/models/${a.id}`, async (route) => {
    if (route.request().method() !== "DELETE") return route.fallback();
    await gate;
    await route.fallback(); // the real 409: an alias still points at it
  });
  for (const whileOpen of [true, false]) {
    await openModelDelete(page, "cf-s409/a");
    await confirmDialog(page).getByRole("button", { name: "Delete" }).click();
    await expect(confirmDialog(page).getByRole("button", { name: "Working…" })).toBeDisabled();
    await page.keyboard.press("Escape");
    await expect(confirmDialog(page)).toHaveCount(0);
    const answered = page.waitForResponse((r) => r.request().method() === "DELETE" && r.url().endsWith(`/models/${a.id}`));
    if (!whileOpen) {
      release();
      expect((await answered).status()).toBe(409);
    }
    await openModelDelete(page, "cf-s409/b");
    const c = confirmDialog(page);
    await expect(c.getByRole("heading", { name: "Delete model" })).toBeVisible();
    if (whileOpen) {
      release();
      expect((await answered).status()).toBe(409);
      await page.waitForTimeout(300);
    }
    await expect(c.getByRole("heading", { name: "Delete model" })).toBeVisible();
    await expect(c.getByTestId("delete-blocked")).toHaveCount(0);
    await expect(c.getByRole("button", { name: "Delete" })).toBeEnabled();
    await c.getByRole("button", { name: "Cancel" }).click();
    await expect(c).toHaveCount(0);
    arm();
  }
  expect((await get<{ items: any[] }>(request, "/models")).items.map((m) => m.name)).toContain("cf-s409/b");
});
