import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { confirmDialog, dialog, optionNames, pick } from "./helpers";

/*
 * The model catalog UI through the real control plane (e2e/serve.py): Models, Aliases, Routers with
 * schema-driven strategy forms, the permission picker, My keys, Usage and Rollback.
 *
 * serve.py seeds two connected gateways (e2e-gw has every strategy, e2e-gw-b lacks `priority_failover`),
 * a small catalog (`use-*`, `use/*`) and usage events across every path a request can take.
 * Everything else is created here through the forms or the API, and removed again after each test (see
 * `afterEach`), so no spec depends on the order the files run in or on how many rows an earlier one left.
 */

const BREAK_GLASS = "e2e-break-glass-password";
const VIEWER = "e2e-viewer-session-0123456789abcdef";
const SHOTS = process.env.E2E_SHOTS;
// The title serve.py gives the hostile strategy (what the Strategy select shows for it).
const HOSTILE_TITLE = '<img src=x onerror="window.__pwned=1"> <b>bold</b> [click](javascript:window.__pwned=1) https://evil.example/phish';
const admin: Record<string, string> = {};

const row = (page: Page, name: string | RegExp) => page.getByRole("row").filter({ has: page.getByRole("cell", { name, exact: typeof name === "string" }) });

test.beforeEach(async ({ request }) => {
  const response = await request.post("/api/v1/session/break-glass", { data: { password: BREAK_GLASS } });
  expect(response.status()).toBe(204);
  const session = await (await request.get("/api/v1/session")).json();
  admin["X-Lumewarp-CSRF"] = session.csrf_token;
  // serve.py seeds gateways and usage after the control plane answers: wait for it.
  await expect
    .poll(async () => {
      const strategies = (await (await request.get("/api/v1/strategies", { headers: admin })).json()).items as { name: string; missing_gateways: string[] }[];
      const usage = (await (await request.get("/api/v1/usage?group_by=model", { headers: admin })).json()).items as unknown[];
      return strategies.find((s) => s.name === "priority_failover")?.missing_gateways.length === 1 && usage.length > 0;
    })
    .toBe(true);
});

// Everything a test made goes away, apart from what serve.py seeded (`use-*` and `use/*`). Delete order follows
// the references: aliases and routers point at models, models at upstreams.
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
const items = async <T = any>(request: APIRequestContext, path: string) => (await get<{ items: T[] }>(request, path)).items;

/** Carry the break-glass session of the API context into the browser. */
async function signIn(page: Page, request: APIRequestContext, path: string) {
  await page.context().addCookies((await request.storageState()).cookies);
  await page.goto(path);
}

async function upstream(request: APIRequestContext, name: string) {
  return post(request, "/upstreams", { name, kind: "vllm", base_url: "http://127.0.0.1:9/v1" });
}
async function model(request: APIRequestContext, name: string, upstream_id: string, upstream_model = "m") {
  return post(request, "/models", { name, upstream_id, upstream_model });
}
const noOverflow = (page: Page) => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);

async function shot(page: Page, name: string) {
  // Let dialogs finish sliding in before the picture.
  if (SHOTS) await page.waitForTimeout(400);
  if (SHOTS) await page.screenshot({ path: join(SHOTS, `${name}.png`), fullPage: true });
}

test("models, aliases and routers: create through the forms, search by upstream model, delete blocked, rename", async ({ page, request }) => {
  await upstream(request, "cat-up");
  await signIn(page, request, "/models");

  // --- Models
  await expect(page.getByRole("heading", { name: "Models" })).toBeVisible();
  for (const [name, upstreamModel] of [["cat/alpha", "Org/Alpha-8B"], ["cat/beta", "Org/Beta-8B"]]) {
    await page.getByRole("button", { name: "New model" }).click();
    const d = dialog(page);
    await d.getByLabel("Name").fill(name);
    await pick(d, "Upstream", "cat-up");
    await d.getByLabel("Upstream model").fill(upstreamModel);
    await d.getByRole("button", { name: "Create" }).click();
    await expect(d).toBeHidden();
  }
  const alpha = row(page, "cat/alpha");
  await expect(alpha).toContainText("cat-up");
  await expect(alpha).toContainText("Org/Alpha-8B");

  // Search matches the public name and the upstream model name.
  await page.getByLabel("Search models").fill("Alpha-8B");
  await expect(row(page, "cat/alpha")).toBeVisible();
  await expect(row(page, "cat/beta")).toHaveCount(0);
  await page.getByLabel("Search models").fill("cat/beta");
  await expect(row(page, "cat/beta")).toBeVisible();
  await expect(row(page, "cat/alpha")).toHaveCount(0);
  await page.getByLabel("Search models").fill("no-such-thing");
  await expect(page.getByText("No models match your search.")).toBeVisible();
  await page.getByLabel("Search models").fill("");

  // --- Aliases (a new page in the Routing group)
  const nav = page.getByRole("navigation", { name: "Main" });
  await expect(nav.getByRole("group", { name: "Routing" }).getByRole("link")).toHaveText(["Upstreams", "Models", "Aliases", "Routers"]);
  await nav.getByRole("link", { name: "Aliases" }).click();
  await page.getByRole("button", { name: "New alias" }).click();
  await dialog(page).getByLabel("Name").fill("cat-alias");
  await pick(dialog(page), "Points to", "cat/alpha (model)");
  await dialog(page).getByRole("button", { name: "Create" }).click();
  const aliasRow = row(page, "cat-alias");
  await expect(aliasRow).toContainText("cat/alpha");
  await expect(aliasRow).toContainText("model");
  await expect(aliasRow).toContainText("Org/Alpha-8B");
  // Search also matches the upstream model name.
  await page.getByLabel("Search aliases").fill("Alpha-8B");
  await expect(aliasRow).toBeVisible();
  await page.getByLabel("Search aliases").fill("Beta-8B");
  await expect(page.getByText("No aliases match your search.")).toBeVisible();
  await page.getByLabel("Search aliases").fill("");

  // --- Routers
  await nav.getByRole("link", { name: "Routers" }).click();
  await page.getByRole("button", { name: "New router" }).click();
  let d = dialog(page);
  await d.getByLabel("Name").fill("cat-router");
  await pick(d, "Strategy", "Weighted");
  await d.getByLabel("cat/alpha", { exact: true }).check();
  await d.getByLabel("Weight of cat/alpha").fill("20");
  await d.getByLabel("cat/beta", { exact: true }).check();
  await d.getByLabel("Weight of cat/beta").fill("80");
  await d.getByRole("button", { name: "Create" }).click();
  await expect(d).toBeHidden();
  const routerRow = row(page, "cat-router");
  await expect(routerRow).toContainText("Weighted");
  await expect(routerRow).toContainText("×20");
  await expect(routerRow).toContainText("×80");
  await expect(routerRow).toContainText("OK");
  await page.getByLabel("Search routers").fill("Beta-8B");
  await expect(routerRow).toBeVisible();
  await page.getByLabel("Search routers").fill("");

  // The Models page now says who uses each model.
  await nav.getByRole("link", { name: "Models" }).click();
  await expect(row(page, "cat/alpha")).toContainText("cat-router");
  await expect(row(page, "cat/alpha")).toContainText("cat-alias");

  // --- Delete is blocked while referenced: the 409 lists who refers to it.
  await row(page, "cat/alpha").getByRole("button", { name: "Delete" }).click();
  await confirmDialog(page).getByRole("button", { name: "Delete" }).click();
  const blocked = confirmDialog(page).getByTestId("delete-blocked");
  await expect(blocked).toContainText("alias cat-alias");
  await expect(blocked).toContainText("router cat-router");
  await confirmDialog(page).getByRole("button", { name: "Got it" }).click();
  await expect(row(page, "cat/alpha")).toBeVisible();

  // --- Rename with "Keep old name as alias" (checked by default) ...
  await row(page, "cat/beta").getByRole("button", { name: "Edit" }).click();
  d = dialog(page);
  await expect(d.getByLabel("Keep old name as alias")).toHaveCount(0); // only once the name changes
  await d.getByLabel("Name").fill("cat/beta2");
  await expect(d.getByLabel("Keep old name as alias")).toBeChecked();
  await d.getByRole("button", { name: "Save" }).click();
  await expect(d).toBeHidden();
  const aliases = await items<{ name: string; target: string; target_name: string }>(request, "/aliases");
  const kept = aliases.find((a) => a.name === "cat/beta");
  expect(kept?.target_name).toBe("cat/beta2");

  // ... and without it.
  await row(page, "cat/alpha").getByRole("button", { name: "Edit" }).click();
  d = dialog(page);
  await d.getByLabel("Name").fill("cat/alpha2");
  await d.getByLabel("Keep old name as alias").uncheck();
  await d.getByRole("button", { name: "Save" }).click();
  await expect(d).toBeHidden();
  const names = (await items<{ name: string }>(request, "/catalog")).map((e) => e.name);
  expect(names).toContain("cat/alpha2");
  expect(names).not.toContain("cat/alpha");
  expect(names).toContain("cat/beta");

  // Aliases and routers rename through their forms too, and keep their references.
  await nav.getByRole("link", { name: "Aliases" }).click();
  await row(page, "cat-alias").getByRole("button", { name: "Edit" }).click();
  await expect(dialog(page).getByLabel("Keep old name as alias")).toHaveCount(0);
  await dialog(page).getByLabel("Name").fill("cat-alias2");
  await pick(dialog(page), "Points to", "cat-router (router)");
  await dialog(page).getByRole("button", { name: "Save" }).click();
  await expect(row(page, "cat-alias2")).toContainText("router");
  await expect(row(page, "cat-alias2")).toContainText("Org/Alpha-8B");
  await nav.getByRole("link", { name: "Routers" }).click();
  await expect(row(page, "cat-router")).toContainText("cat-alias2");
  await row(page, "cat-router").getByRole("button", { name: "Edit" }).click();
  await expect(dialog(page).getByLabel("Keep old name as alias")).toHaveCount(0);
  await dialog(page).getByLabel("Name").fill("cat-router2");
  await dialog(page).getByRole("button", { name: "Save" }).click();
  await expect(row(page, "cat-router2")).toContainText("cat-alias2");
  expect((await items<{ name: string; members: unknown[] }>(request, "/routers")).find((r) => r.name === "cat-router2")?.members).toHaveLength(2);

  // A router with an alias pointing at it cannot be deleted either.
  await row(page, "cat-router2").getByRole("button", { name: "Delete" }).click();
  await confirmDialog(page).getByRole("button", { name: "Delete" }).click();
  await expect(confirmDialog(page).getByTestId("delete-blocked")).toContainText("alias cat-alias2");
  await confirmDialog(page).getByRole("button", { name: "Got it" }).click();
});

test("reference strategy: form from its descriptor, weight hidden, Save anyway for a gateway that lacks it", async ({ page, request }) => {
  const up = await upstream(request, "pf-up");
  const one = await model(request, "pf/one", up.id);
  const two = await model(request, "pf/two", up.id);
  await signIn(page, request, "/routers");

  await page.getByRole("button", { name: "New router" }).click();
  const d = dialog(page);
  await d.getByLabel("Name").fill("pf-router");
  await pick(d, "Strategy", "Priority failover");
  await expect(d.getByText("Use the lowest priority number that is healthy.")).toBeVisible();

  // A gateway without the strategy: warning plus a required "Save anyway".
  const warning = d.getByTestId("missing-gateways");
  await expect(warning).toContainText("e2e-gw-b");
  // Strategy settings from the schema; no weights because the strategy does not use them.
  await expect(d.getByTestId("strategy-settings")).toBeVisible();
  await expect(d.getByLabel("Max in flight")).toBeVisible();
  await d.getByLabel("pf/one", { exact: true }).check();
  await d.getByLabel("pf/two", { exact: true }).check();
  await expect(d.getByLabel(/^Weight of/)).toHaveCount(0);
  const memberOne = d.getByTestId("member-pf/one");
  const memberTwo = d.getByTestId("member-pf/two");

  // Required member field, lower bound, then the confirmation.
  await d.getByRole("button", { name: "Create" }).click();
  await expect(d.getByText("Priority is required")).toHaveCount(2);
  await expect(d.getByText(/Tick “Save anyway”/)).toBeVisible();
  await memberOne.getByLabel("Priority").fill("0");
  await memberTwo.getByLabel("Priority").fill("0");
  await d.getByLabel("Max in flight").fill("0");
  await d.getByRole("button", { name: "Create" }).click();
  await expect(d.getByText("Must be at least 1")).toHaveCount(3);
  await expect(d).toBeVisible();
  await memberOne.getByLabel("Priority").fill("1");
  await memberTwo.getByLabel("Priority").fill("2");
  await d.getByLabel("Max in flight").fill("5");
  await d.getByRole("button", { name: "Create" }).click();
  await expect(d.getByText("Must be at least 1")).toHaveCount(0);
  await expect(d.getByText(/Tick “Save anyway”/)).toBeVisible(); // still not confirmed
  await expect(d).toBeVisible();
  await shot(page, "router-form-priority-failover");
  await d.getByLabel("Save anyway").check();
  await d.getByRole("button", { name: "Create" }).click();
  await expect(d).toBeHidden();

  const stored = (await items<any>(request, "/routers")).find((r) => r.name === "pf-router");
  expect(stored.strategy).toBe("priority_failover");
  expect(stored.params).toEqual({ max_in_flight: 5 });
  expect(stored.members).toEqual([
    { model_id: one.id, weight: 1, params: { priority: 1 } },
    { model_id: two.id, weight: 1, params: { priority: 2 } },
  ]);
  const forced = await items<{ resource_id: string }>(request, "/audit?action=router.force_save");
  expect(forced.some((e) => e.resource_id === stored.id)).toBe(true);

  const listed = row(page, "pf-router");
  await expect(listed).toContainText("Priority failover");
  await expect(listed).toContainText("Missing on e2e-gw-b");
  await expect(listed).toContainText("priority 1");
  // Installed strategies and how many gateways support each.
  const strategies = page.getByRole("heading", { name: "Strategies" });
  await strategies.scrollIntoViewIfNeeded();
  await page.getByLabel("Search routers").fill("");
  const installedTable = page.getByRole("region", { name: "Strategies" });
  const installed = installedTable.getByRole("row").filter({ has: page.getByText("priority_failover", { exact: true }) });
  await expect(installed).toContainText("Extension");
  await expect(installed).toContainText("Max in flight, Priority");
  await expect(installed).toContainText("1 / 2");
  await expect(installedTable.getByRole("row").filter({ has: page.getByText("weighted", { exact: true }) })).toContainText("Built-in");

  // Editing shows what was saved.
  await listed.getByRole("button", { name: "Edit" }).click();
  await expect(dialog(page).getByLabel("Max in flight")).toHaveValue("5");
  await expect(dialog(page).getByTestId("member-pf/two").getByLabel("Priority")).toHaveValue("2");
});

test("no UI code knows a strategy by name", () => {
  const found: string[] = [];
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir)) {
      const path = join(dir, entry);
      if (statSync(path).isDirectory()) walk(path);
      else if (readFileSync(path, "utf8").includes("priority_failover")) found.push(path);
    }
  };
  walk(fileURLToPath(new URL("../src", import.meta.url)));
  expect(found).toEqual([]);
});

test("schema fields: required, bounds, enum, boolean, nested, header list", async ({ page, request }) => {
  const up = await upstream(request, "tuned-up");
  const a = await model(request, "tuned/a", up.id);
  await signIn(page, request, "/routers");
  await page.getByRole("button", { name: "New router" }).click();
  const d = dialog(page);
  await d.getByLabel("Name").fill("tuned-router");
  await pick(d, "Strategy", "E2E tuned");
  // Supported by every connected gateway: nothing to confirm.
  await expect(d.getByTestId("missing-gateways")).toHaveCount(0);
  await expect(d.getByLabel("Save anyway")).toHaveCount(0);
  await d.getByLabel("tuned/a", { exact: true }).check();
  await expect(d.getByLabel(/^Weight of/)).toHaveCount(0);

  await d.getByRole("button", { name: "Create" }).click();
  await expect(d.getByText("Max waiting requests is required")).toBeVisible();
  const max = d.getByLabel("Max waiting requests");
  await max.fill("0");
  await expect(d.getByText("Must be at least 1")).toBeVisible();
  await max.fill("101");
  await expect(d.getByText("Must be at most 100")).toBeVisible();
  await max.fill("7");
  await expect(d.getByText(/Must be at (least|most)/)).toHaveCount(0);
  await max.fill("1.5");
  await expect(d.getByText("Must be a whole number")).toBeVisible();
  await max.fill("7");

  await pick(d, "Mode", "relaxed");
  await pick(d, "Skip unpriced models", "On");
  await d.getByRole("button", { name: "Add Tuning" }).click();
  await d.getByTestId("schema-tuning").getByLabel("Fail back after (ms)").fill("5");
  await d.getByTestId("member-tuned/a").getByLabel("Region").fill("eu");

  // The header-list widget: platform rules apply whatever the schema says.
  const headers = d.getByRole("group", { name: "Pin headers" });
  await headers.getByRole("button", { name: "Add header" }).click();
  await headers.getByLabel("Pin headers 1", { exact: true }).fill("authorization");
  await d.getByRole("button", { name: "Create" }).click();
  await expect(d.getByText(/carries credentials/)).toBeVisible();
  await headers.getByLabel("Pin headers 1", { exact: true }).fill("X-Session-Id");
  await headers.getByRole("button", { name: "Add header" }).click();
  await headers.getByLabel("Pin headers 2", { exact: true }).fill("x-team");
  await shot(page, "router-form-tuned");
  await d.getByRole("button", { name: "Create" }).click();
  await expect(d).toBeHidden();

  const stored = (await items<any>(request, "/routers")).find((r) => r.name === "tuned-router");
  expect(stored.params).toEqual({ max_waiting: 7, mode: "relaxed", skip_unpriced: true, tuning: { fail_back_ms: 5 }, headers: ["X-Session-Id", "x-team"] });
  expect(stored.members).toEqual([{ model_id: a.id, weight: 1, params: { region: "eu" } }]);
});

test("no usable schema: a JSON editor, for null and for a schema outside the subset", async ({ page, request }) => {
  const up = await upstream(request, "json-up");
  const a = await model(request, "json/a", up.id);
  // A strategy a gateway reported in a shape outside the subset: the control plane would not store it,
  // but the form must cope should one arrive.
  await page.route("**/api/v1/strategies", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.items.push({ ...body.items[0], name: "e2e_weird", title: "E2E weird", builtin: false, params_schema: { type: "object", oneOf: [{ required: ["a"] }], properties: {} }, member_schema: { $ref: "#/x" }, missing_gateways: [], gateways: [] });
    await route.fulfill({ response, json: body });
  });
  await signIn(page, request, "/routers");
  await page.getByRole("button", { name: "New router" }).click();
  const d = dialog(page);

  await pick(d, "Strategy", "E2E weird");
  await expect(d.getByTestId("strategy-settings-json")).toContainText("outside what the form can show");
  await pick(d, "Strategy", "E2E schemaless");
  await expect(d.getByTestId("strategy-settings-json")).toContainText("No settings schema is known");
  const editor = d.getByLabel("Settings (JSON)", { exact: true });
  await d.getByLabel("Name").fill("json-router");
  await d.getByLabel("json/a", { exact: true }).check();
  await expect(d.getByLabel("Settings for json/a (JSON)")).toBeVisible();
  await editor.fill("{ not json");
  await expect(d.getByText(/JSON|Expected|Unexpected/).first()).toBeVisible();
  await d.getByRole("button", { name: "Create" }).click();
  await expect(d).toBeVisible(); // refused while the text is not a JSON object
  await editor.fill("[1]");
  await expect(d.getByText("Must be a JSON object")).toBeVisible();
  await editor.fill('{"threshold": 3}');
  await d.getByLabel("Settings for json/a (JSON)").fill('{"zone": "z1"}');
  await d.getByRole("button", { name: "Create" }).click();
  await expect(d).toBeHidden();
  const stored = (await items<any>(request, "/routers")).find((r) => r.name === "json-router");
  expect(stored.params).toEqual({ threshold: 3 });
  expect(stored.members[0].params).toEqual({ zone: "z1" });
  expect(a.id).toBe(stored.members[0].model_id);
});

test("hostile strategy text is only ever shown as text", async ({ page, request }) => {
  await signIn(page, request, "/routers");
  await page.evaluate(() => ((window as any).__pwned = undefined));
  await page.getByRole("button", { name: "New router" }).click();
  const d = dialog(page);
  await pick(d, "Strategy", HOSTILE_TITLE);
  const settings = d.getByTestId("strategy-settings");
  await expect(settings).toBeVisible();
  const text = (await settings.textContent()) ?? "";
  // Markup, a markdown link and a URL appear literally, as characters.
  expect(text).toContain('<img src=x onerror="window.__pwned=1">');
  expect(text).toContain("<b>bold</b>");
  expect(text).toContain("[click](javascript:window.__pwned=1)");
  expect(text).toContain("https://evil.example/phish");
  for (const tag of ["img", "b", "a", "script", "iframe"]) await expect(page.locator(`main ${tag}, [role=dialog] ${tag}`)).toHaveCount(0);
  await expect(d.locator("[onerror], [href]")).toHaveCount(0);
  // A 3000 character description is cut to the size limit, not rendered whole.
  const longest = await d.evaluate((el) => Math.max(...[...el.querySelectorAll("p")].map((p) => p.textContent?.length ?? 0)));
  expect(longest).toBeLessThanOrEqual(2100);
  expect(await page.evaluate(() => (window as any).__pwned)).toBeUndefined();
  // The enum option that is hostile is an option's text.
  const options = await optionNames(d, "Pick");
  const hostile = options.find((o) => o.includes("<img"));
  expect(hostile).toBeTruthy();
  await pick(d, "Pick", hostile!);
  await expect(page.locator("img, [onerror]")).toHaveCount(0);
  await d.getByRole("button", { name: "Cancel" }).click();
  // The installed-strategies table shows the title as text too.
  await expect(page.getByRole("row").filter({ has: page.getByText("e2e_hostile", { exact: true }) })).toContainText("<b>bold</b>");
  await expect(page.locator("main img")).toHaveCount(0);
  expect(await page.evaluate(() => (window as any).__pwned)).toBeUndefined();
});

test("traffic split: models move under each split", async ({ page, request }) => {
  const up = await upstream(request, "split-up");
  const stable = await model(request, "split/stable", up.id);
  const canary = await model(request, "split/canary", up.id);
  await signIn(page, request, "/routers");
  await page.getByRole("button", { name: "New router" }).click();
  const d = dialog(page);
  await d.getByLabel("Name").fill("split-router");
  await d.getByLabel("split/stable", { exact: true }).check();
  await d.getByRole("button", { name: "Add split" }).click();
  // The models picked so far are now the first split's; the router-level list is gone.
  await expect(d.getByTestId("members-in-splits")).toBeVisible();
  await expect(d.getByRole("group", { name: "Models", exact: true })).toHaveCount(0);
  const first = d.getByRole("group", { name: "Split 1" });
  await expect(first.getByLabel("split/stable", { exact: true })).toBeChecked();
  await d.getByRole("button", { name: "Add split" }).click();
  const second = d.getByRole("group", { name: "Split 2" });
  await second.getByLabel("Split name").fill("canary");
  await second.getByLabel("Weight", { exact: true }).fill("10");
  await second.getByLabel("split/canary", { exact: true }).check();
  await first.getByLabel("Weight", { exact: true }).fill("90");
  await expect(first.getByLabel("Share of split 1")).toHaveText("90%");
  // Fallback is optional: none picked.
  await expect(d.getByTestId("order-row")).toHaveCount(0);
  await expect(d.getByLabel("Add fallback model")).toHaveText("None — add a fallback model…");
  await d.getByRole("button", { name: "Create" }).click();
  await expect(d).toBeHidden();
  const stored = (await items<any>(request, "/routers")).find((r) => r.name === "split-router");
  expect(stored.members).toEqual([]);
  expect(stored.fallback_model_ids).toEqual([]);
  expect(stored.traffic_split.map((s: any) => [s.name, s.weight, s.members.map((m: any) => m.model_id)])).toEqual([
    ["stable", 90, [stable.id]],
    ["canary", 10, [canary.id]],
  ]);
  await expect(row(page, "split-router")).toContainText("canary 10%");

  // Removing the last split hands the models back to the router.
  await row(page, "split-router").getByRole("button", { name: "Edit" }).click();
  await d.getByRole("button", { name: "Remove split 2" }).click();
  await d.getByRole("button", { name: "Remove split 1" }).click();
  await expect(d.getByRole("group", { name: "Models", exact: true }).getByLabel("split/stable", { exact: true })).toBeChecked();
});

test("permission picker: team All or Selected, member Inherit or Selected within the team", async ({ page, request }) => {
  const up = await upstream(request, "perm-up");
  const pa = await model(request, "perm/a", up.id);
  await model(request, "perm/b", up.id);
  await model(request, "perm/c", up.id);
  const r = await post(request, "/routers", { name: "perm-router", members: [{ model_id: pa.id }] });
  await post(request, "/aliases", { name: "perm-alias", target: r.id });
  await signIn(page, request, "/teams");

  // Team: All | Selected, grouped by Models, Aliases, Routers.
  await page.getByRole("button", { name: "New team" }).click();
  let d = dialog(page);
  await d.getByLabel("Name").fill("Perm team");
  await expect(d.getByRole("radio", { name: "All" })).toBeChecked();
  await expect(d.getByRole("radio", { name: "Inherit" })).toHaveCount(0);
  await expect(d.getByTestId("permission-options")).toHaveCount(0);
  await d.getByRole("radio", { name: "Selected" }).click();
  const options = d.getByTestId("permission-options");
  for (const group of ["Models", "Aliases", "Routers"]) await expect(options.getByRole("group", { name: group })).toBeVisible();
  await expect(options.getByRole("group", { name: "Models" }).getByLabel("perm/a", { exact: true })).toBeVisible();
  await expect(options.getByRole("group", { name: "Aliases" }).getByLabel("perm-alias", { exact: true })).toBeVisible();
  await expect(options.getByRole("group", { name: "Routers" }).getByLabel("perm-router", { exact: true })).toBeVisible();
  await options.getByLabel("perm/a", { exact: true }).check();
  await options.getByLabel("perm/b", { exact: true }).check();
  await options.getByLabel("perm-alias", { exact: true }).check();
  await shot(page, "permission-picker-team");
  await d.getByRole("button", { name: "Create" }).click();
  await expect(d).toBeHidden();
  const team = (await items<any>(request, "/teams")).find((t) => t.name === "Perm team");
  expect(team.model_access).toBe("list");
  expect(team.allowed_models.sort()).toEqual(["perm-alias", "perm/a", "perm/b"]);

  // Member: Inherit | Selected, and only what the team allows can be picked.
  const viewer = (await items<any>(request, "/users")).find((u) => u.email === "viewer@e2e.test");
  const put = await request.put(`/api/v1/teams/${team.id}/members/${viewer.id}`, { headers: admin, data: { role: "member" } });
  expect(put.ok(), await put.text()).toBe(true);
  await page.goto(`/teams/${team.id}`);
  await row(page, "viewer@e2e.test").getByRole("button", { name: "Edit" }).click();
  d = dialog(page);
  await expect(d.getByRole("radio", { name: "Inherit" })).toBeChecked();
  await expect(d.getByRole("radio", { name: "All" })).toHaveCount(0);
  await d.getByRole("radio", { name: "Selected" }).click();
  const memberOptions = d.getByTestId("permission-options");
  await expect(memberOptions.getByLabel("perm/a", { exact: true })).toBeVisible();
  await expect(memberOptions.getByLabel("perm-alias", { exact: true })).toBeVisible();
  await expect(memberOptions.getByLabel("perm/c", { exact: true })).toHaveCount(0); // outside the team
  await expect(memberOptions.getByLabel("perm-router", { exact: true })).toHaveCount(0);
  await expect(memberOptions.getByRole("group", { name: "Routers" })).toHaveCount(0);
  await memberOptions.getByLabel("perm/b", { exact: true }).check();
  await d.getByRole("button", { name: "Save" }).click();
  await expect(d).toBeHidden();
  const members = await items<any>(request, `/teams/${team.id}/members`);
  expect(members[0].model_access).toBe("list");
  expect(members[0].allowed_models).toEqual(["perm/b"]);

  // Service accounts use the same picker, scoped the same way.
  await page.getByRole("tab", { name: "Service accounts" }).click();
  await page.getByRole("button", { name: "New service account" }).click();
  d = dialog(page);
  await d.getByLabel("Name").fill("perm-agent");
  await d.getByRole("radio", { name: "Selected" }).click();
  await expect(d.getByTestId("permission-options").getByLabel("perm/c", { exact: true })).toHaveCount(0);
  await d.getByTestId("permission-options").getByLabel("perm-alias", { exact: true }).check();
  await d.getByRole("button", { name: "Create" }).click();
  await expect(d).toBeHidden();
  const accounts = await items<any>(request, `/teams/${team.id}/service-accounts`);
  expect(accounts[0].allowed_models).toEqual(["perm-alias"]);

  // Narrowing the team keeps the member's list but the picker flags what fell out of scope.
  const patched = await request.patch(`/api/v1/teams/${team.id}`, { headers: admin, data: { allowed_models: ["perm/a"] } });
  expect(patched.ok(), await patched.text()).toBe(true);
  await page.reload();
  await row(page, "viewer@e2e.test").getByRole("button", { name: "Edit" }).click();
  await expect(dialog(page).getByTestId("permission-options")).toContainText("outside the team's scope");
});

test("My keys shows what a new key can call; a member cannot open admin pages", async ({ request, browser }) => {
  const up = await upstream(request, "mine-up");
  await model(request, "mine/a", up.id);
  await model(request, "mine/b", up.id);
  const team = await post<any>(request, "/teams", { name: "Mine team", model_access: "list", allowed_models: ["mine/a", "mine/b"] });
  const viewer = (await items<any>(request, "/users")).find((u) => u.email === "viewer@e2e.test");
  const put = await request.put(`/api/v1/teams/${team.id}/members/${viewer.id}`, { headers: admin, data: { role: "member", model_access: "list", allowed_models: ["mine/b"] } });
  expect(put.ok(), await put.text()).toBe(true);

  const context = await browser.newContext({ baseURL: test.info().project.use.baseURL });
  const member = await context.newPage();
  await context.addCookies([{ name: "lumewarp_session", value: VIEWER, url: test.info().project.use.baseURL! }]);
  await member.goto("/my-keys");
  const section = member.getByTestId("team-Mine team");
  const names = section.getByTestId("callable-models");
  await expect(names).toContainText("mine/b");
  await expect(names).not.toContainText("mine/a"); // the member's own list narrows the team's
  await section.getByRole("button", { name: "New key" }).click();
  const d = dialog(member);
  const dialogNames = d.getByTestId("callable-models");
  await expect(dialogNames).toContainText("mine/b");
  await expect(dialogNames).not.toContainText("mine/a");
  await expect(dialogNames.getByRole("checkbox")).toHaveCount(0); // read-only
  await expect(dialogNames.locator("input")).toHaveCount(0);
  await shot(member, "my-keys-new-key");
  await d.getByLabel("Name").fill("viewer-key");
  await d.getByRole("button", { name: "Create key" }).click();
  const secret = member.getByRole("dialog", { name: "API key created" });
  await expect(secret.getByTestId("secret-value")).toBeVisible();
  await secret.getByRole("button", { name: /saved it/ }).click();

  // Admin pages are not shown to a plain member, and opening one by URL says so instead of failing.
  for (const path of ["/models", "/aliases", "/routers", "/upstreams"]) {
    await member.goto(path);
    await expect(member.getByRole("heading", { name: "You do not have access to this page" })).toBeVisible();
    await expect(member.getByTestId("state-error")).toHaveCount(0);
  }
  await expect(member.getByRole("link", { name: "Models", exact: true })).toHaveCount(0);
  await context.close();
});

test("usage: grouped by model by default, with how each request was reached, and filters", async ({ page, request }) => {
  await signIn(page, request, "/usage");
  await expect(page.getByLabel("Group by")).toHaveText("Model");
  const headers = page.getByRole("columnheader");
  await expect(headers.filter({ hasText: "Upstream model" })).toBeVisible();
  await expect(headers.filter({ hasText: "Via alias" })).toBeVisible();
  await expect(headers.filter({ hasText: "Via router" })).toBeVisible();

  const a = row(page, "use/a");
  await expect(a).toContainText("Qwen/UseA");
  await expect(a).toContainText("use-alias");
  await expect(a).toContainText("use-router");
  await expect(a.getByRole("cell", { name: /^2$/ })).toBeVisible(); // two requests
  // use/c was reached directly and through an alias: the alias cell says "mixed", the router cell is empty.
  const c = row(page, "use/c");
  await expect(c).toContainText("Other/UseC");
  await expect(c.getByTestId("mixed")).toContainText("use-direct");
  await expect(c.getByTestId("mixed")).toContainText("mixed");
  await shot(page, "usage-by-model");

  // Group by alias / router show who served.
  await pick(page, "Group by", "Alias");
  await expect(page.getByRole("columnheader", { name: "Served by" })).toBeVisible();
  await expect(row(page, "use-alias")).toContainText("use/a");
  await expect(row(page, "use-alias")).toContainText("use/b");
  await expect(row(page, "none")).toContainText("use/c");
  await pick(page, "Group by", "Router");
  await expect(row(page, "use-router")).toContainText("use/a");
  await expect(row(page, "none")).toContainText("use/c");
  await pick(page, "Group by", "Upstream model");
  await expect(row(page, "Qwen/UseA")).toContainText("use/a");
  await expect(row(page, "Qwen/UseA")).toContainText("use/b");
  await pick(page, "Group by", "Model");

  // Filters: model, upstream model, alias (including none), router (including none).
  await pick(page, "Model", "use/b");
  await expect(row(page, "use/b")).toBeVisible();
  await expect(row(page, "use/a")).toHaveCount(0);
  await pick(page, "Model", "All models");
  await page.getByLabel("Upstream model", { exact: true }).fill("Other/UseC");
  await expect(row(page, "use/c")).toBeVisible();
  await expect(row(page, "use/a")).toHaveCount(0);
  await page.getByLabel("Upstream model", { exact: true }).fill("");
  await pick(page, "Alias", "None (no alias)");
  await expect(row(page, "use/c")).toBeVisible();
  await expect(row(page, "use/a")).toHaveCount(0);
  await expect(row(page, "use/c").getByTestId("mixed")).toHaveCount(0); // only the direct request is left
  await pick(page, "Alias", "use-direct");
  await expect(row(page, "use/c")).toContainText("use-direct");
  await pick(page, "Alias", "Any");
  await pick(page, "Router", "None (no router)");
  await expect(row(page, "use/c")).toBeVisible();
  await expect(row(page, "use/a")).toHaveCount(0);
  await pick(page, "Router", "use-router");
  await expect(row(page, "use/a")).toBeVisible();
  await expect(row(page, "use/c")).toHaveCount(0);
  await pick(page, "Router", "Via any router");
  await expect(row(page, "use/b")).toBeVisible();
  await expect(row(page, "use/c")).toHaveCount(0);

  // Nothing matches: an empty state, not an error.
  await pick(page, "Alias", "use-direct");
  await expect(page.getByText("No usage in this period.")).toBeVisible();
});

test("usage: loading, then a refusal (no permission) with a way to retry", async ({ page, request }) => {
  let refuse = true;
  await page.route("**/api/v1/usage**", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 400));
    if (refuse) await route.fulfill({ status: 403, json: { error: { code: "forbidden", message: "not allowed to read usage" } } });
    else await route.continue();
  });
  await signIn(page, request, "/usage");
  await expect(page.locator("[aria-busy=true]").first()).toBeVisible();
  await expect(page.getByTestId("state-error")).toContainText("not allowed to read usage");
  refuse = false;
  await page.getByRole("button", { name: "Retry" }).click();
  await expect(row(page, "use/a")).toBeVisible();
});

test("rollback: a 422 about missing strategies offers a forced confirm, other 422s show the error", async ({ page, request }) => {
  const up = await upstream(request, "rb-up");
  const latest = async () => Math.max(...(await items<{ version: number }>(request, "/config/versions")).map((v) => v.version));
  // Each change is published as a version shortly after it commits: wait for it before the next.
  const start = await latest();
  const m = await model(request, "rb/a", up.id);
  await expect.poll(latest).toBeGreaterThan(start);
  // A router whose strategy e2e-gw-b lacks, saved past the warning: it is in this version ...
  const before = await latest();
  const router = await post<any>(request, "/routers", { name: "rb-router", strategy: "priority_failover", members: [{ model_id: m.id, params: { priority: 1 } }], force: true });
  await expect.poll(latest).toBeGreaterThan(before);
  const withRouter = await latest();
  // ... and a later version.
  await model(request, "rb/b", up.id);
  await expect.poll(latest).toBeGreaterThan(withRouter);
  await signIn(page, request, "/config");

  // Another 422 is shown as it is, with no way to force it.
  await page.route("**/api/v1/config/rollback", async (route) => {
    await route.fulfill({ status: 422, json: { error: { code: "validation_error", message: "version: payload was compacted" } } });
  });
  await row(page, `v${withRouter}`).getByRole("button", { name: "Roll back" }).click();
  await dialog(page).getByRole("button", { name: "Roll back" }).click();
  await expect(dialog(page).getByTestId("rollback-problem")).toContainText("payload was compacted");
  await expect(dialog(page).getByRole("button", { name: "Roll back anyway" })).toHaveCount(0);
  await dialog(page).getByRole("button", { name: "Cancel" }).click();
  // Text that merely contains "send force" is not the missing-gateway refusal: a params error whose allowed
  // value is "send force", and a strategy error that is not about gateways.
  for (const message of ["params: mode: must be one of ['send force']", "strategy: no gateway reports strategy 'x'; send force", "params: connected gateways lack 'x' and would reject the whole config: g; send force to save anyway"]) {
    await page.unroute("**/api/v1/config/rollback");
    await page.route("**/api/v1/config/rollback", async (route) => {
      await route.fulfill({ status: 422, json: { error: { code: "validation_error", message } } });
    });
    await row(page, `v${withRouter}`).getByRole("button", { name: "Roll back" }).click();
    await dialog(page).getByRole("button", { name: "Roll back" }).click();
    await expect(dialog(page).getByTestId("rollback-problem")).toContainText(message.split(": ").slice(1).join(": "));
    await expect(dialog(page).getByRole("button", { name: "Roll back anyway" })).toHaveCount(0);
    await dialog(page).getByRole("button", { name: "Cancel" }).click();
  }
  await page.unroute("**/api/v1/config/rollback");

  // The real refusal: connected gateways lack the strategy.
  const sent: unknown[] = [];
  await page.route("**/api/v1/config/rollback", async (route) => {
    sent.push(route.request().postDataJSON());
    await route.continue();
  });
  // The rollback restores rb-router's strategy; make sure it is still in use, then roll back to the version before it was deleted.
  await request.delete(`/api/v1/routers/${router.id}`, { headers: admin });
  await row(page, `v${withRouter}`).getByRole("button", { name: "Roll back" }).click();
  await dialog(page).getByRole("button", { name: "Roll back" }).click();
  const problem = dialog(page).getByTestId("rollback-problem");
  await expect(problem).toContainText("e2e-gw-b");
  await expect(problem).toContainText("send force");
  await shot(page, "rollback-force");
  await dialog(page).getByRole("button", { name: "Roll back anyway" }).click();
  await expect(dialog(page)).toBeHidden();
  expect(sent).toEqual([{ version: withRouter }, { version: withRouter, force: true }]);
  expect((await items<any>(request, "/routers")).some((r) => r.name === "rb-router")).toBe(true);
});

test("dark mode and phone width", async ({ page, request }) => {
  await page.addInitScript(() => localStorage.setItem("lumewarp.theme", "dark"));
  await page.setViewportSize({ width: 390, height: 844 });
  await signIn(page, request, "/models");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  for (const [path, heading] of [["/models", "Models"], ["/aliases", "Aliases"], ["/routers", "Routers"], ["/usage", "Usage"], ["/config", "Config versions"]] as const) {
    await page.goto(path);
    await expect(page.getByRole("heading", { name: heading, exact: true })).toBeVisible();
    await expect(page.locator("body")).toHaveCSS("background-color", /rgb\(\s*(?:[0-3]?\d)\b/); // a dark page
    expect(await noOverflow(page), `${path} overflows at 390px`).toBe(true);
    await shot(page, `phone-dark${path.replaceAll("/", "-")}`);
  }
  // The router form is usable on a phone: it fits and can be submitted.
  await page.goto("/routers");
  await page.getByRole("button", { name: "New router" }).click();
  const d = dialog(page);
  await pick(d, "Strategy", "E2E tuned");
  // Once it has slid in, the drawer lies within the screen (subpixel layout).
  await expect
    .poll(async () => {
      const box = await d.boundingBox();
      return box!.x + box!.width;
    })
    .toBeLessThanOrEqual(390.5);
  expect(await noOverflow(page)).toBe(true);
  await d.getByRole("button", { name: "Create" }).scrollIntoViewIfNeeded();
  await shot(page, "phone-dark-router-form");
  await d.getByRole("button", { name: "Cancel" }).click();
  // Light, desktop.
  await page.addInitScript(() => localStorage.setItem("lumewarp.theme", "light"));
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/routers");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await shot(page, "desktop-light-routers");
});

test("every page shows an error state when its API fails", async ({ page, request }) => {
  await signIn(page, request, "/models");
  await page.route("**/api/v1/models**", (route) => route.fulfill({ status: 500, json: { error: { code: "internal", message: "models are down" } } }));
  for (const [path, message] of [["/models", "models are down"], ["/aliases", "models are down"], ["/routers", "models are down"]] as const) {
    await page.goto(path);
    await expect(page.getByTestId("state-error").first()).toContainText(message);
  }
});

test("a router whose stored settings no longer fit the strategy shows Config invalid", async ({ page, request }) => {
  await page.route("**/api/v1/routers?**", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.items[0] = { ...body.items[0], config_invalid: true, config_problems: ["params.max_in_flight: must be at least 1"] };
    await route.fulfill({ response, json: body });
  });
  await signIn(page, request, "/routers");
  const invalid = page.getByTestId("config-invalid");
  await expect(invalid).toContainText("Config invalid");
  await expect(invalid).toContainText("params.max_in_flight: must be at least 1");
  await shot(page, "routers-config-invalid");
});

test("empty states: nothing to show and nothing matches", async ({ page, request }) => {
  await page.route("**/api/v1/aliases**", (route) => route.fulfill({ json: { items: [], next_cursor: null } }));
  await signIn(page, request, "/aliases");
  await expect(page.getByText("No aliases yet.")).toBeVisible();
});

/** The bodies of the PATCH requests the page sends to routers, as the raw text (so `__proto__` can be looked for). */
function recordPatches(page: Page) {
  const sent: string[] = [];
  page.on("request", (r) => {
    if (r.method() === "PATCH" && /\/api\/v1\/routers\/[^/?]+$/.test(r.url())) sent.push(r.postData() ?? "");
  });
  return sent;
}
async function editRouter(page: Page, name: string) {
  await page.getByLabel("Search routers").fill(name);
  await row(page, name).getByRole("button", { name: "Edit" }).click();
}
const withoutName = ({ name: _name, updated_at: _updated, ...rest }: any) => rest;

test("saving a router sends only what changed: settings, hidden weights and split members stay as stored", async ({ page, request }) => {
  const up = await upstream(request, "rt-up");
  const [a, b, c] = [await model(request, "rt/a", up.id), await model(request, "rt/b", up.id), await model(request, "rt/c", up.id)];
  // Stored values a form must not tidy: an empty string, a list entry with spaces, an empty nested string.
  const params = { enabled: false, scope: "", regions: [" eu "], limits: {}, prefix: "", tuning: { priority: 2, note: "" } };
  const plain = await post<any>(request, "/routers", { name: "rt-plain", strategy: "e2e_presence", params, members: [{ model_id: a.id, weight: 20, params: { region: "" } }, { model_id: b.id, weight: 80, params: { region: " x " } }] });
  const split = await post<any>(request, "/routers", {
    name: "rt-split",
    strategy: "e2e_presence",
    params,
    traffic_split: [
      { name: "stable", weight: 90, members: [{ model_id: a.id, weight: 20, params: { region: "" } }] },
      { name: "canary", weight: 10, members: [{ model_id: b.id, weight: 80, params: {} }] },
    ],
  });
  // The page knows an older shape of the strategy: `prefix` and `tuning.note` are settings it cannot show.
  await page.route("**/api/v1/strategies", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    const schema = body.items.find((s: any) => s.name === "e2e_presence").params_schema;
    delete schema.properties.prefix;
    delete schema.properties.tuning.properties.note;
    await route.fulfill({ response, json: body });
  });
  const sent = recordPatches(page);
  await signIn(page, request, "/routers");

  for (const [stored, name] of [[plain, "rt-plain"], [split, "rt-split"]] as const) {
    const before = await get<any>(request, `/routers/${stored.id}`);
    await editRouter(page, name);
    await dialog(page).getByLabel("Name", { exact: true }).fill(`${name}-renamed`);
    await dialog(page).getByRole("button", { name: "Save" }).click();
    await expect(dialog(page)).toBeHidden();
    // Renaming sends the name and nothing else ...
    expect(JSON.parse(sent[sent.length - 1])).toEqual({ name: `${name}-renamed` });
    // ... so settings, weights (hidden by this strategy) and split members are exactly what they were.
    const after = await get<any>(request, `/routers/${stored.id}`);
    expect(after.name).toBe(`${name}-renamed`);
    expect(withoutName(after)).toEqual(withoutName(before));
  }
  expect((await get<any>(request, `/routers/${plain.id}`)).members.map((m: any) => m.weight)).toEqual([20, 80]);
  expect((await get<any>(request, `/routers/${split.id}`)).traffic_split.map((s: any) => s.members[0].weight)).toEqual([20, 80]);

  // Changing one setting sends the settings with everything else in them untouched (empty strings, the
  // spaces, the keys the form does not show, nested ones included) and no members.
  await editRouter(page, "rt-plain-renamed");
  await pick(dialog(page), "Enabled *", "On");
  await dialog(page).getByRole("button", { name: "Save" }).click();
  await expect(dialog(page)).toBeHidden();
  expect(JSON.parse(sent[sent.length - 1])).toEqual({ params: { ...params, enabled: true } });
  expect((await get<any>(request, `/routers/${plain.id}`)).params).toEqual({ ...params, enabled: true });

  // Adding a model sends the members: the ones already there keep their weights, the new one starts at 1.
  await editRouter(page, "rt-plain-renamed");
  await dialog(page).getByLabel("rt/c", { exact: true }).check();
  await dialog(page).getByRole("button", { name: "Save" }).click();
  await expect(dialog(page)).toBeHidden();
  expect(Object.keys(JSON.parse(sent[sent.length - 1]))).toEqual(["members"]);
  expect((await get<any>(request, `/routers/${plain.id}`)).members).toEqual([
    { model_id: a.id, weight: 20, params: { region: "" } },
    { model_id: b.id, weight: 80, params: { region: " x " } },
    { model_id: c.id, weight: 1, params: {} },
  ]);
});

test("schema presence: required values, empty values and optional objects follow JSON Schema", async ({ page, request }) => {
  const up = await upstream(request, "pres-up");
  const a = await model(request, "pres/a", up.id);
  await signIn(page, request, "/routers");
  await page.getByRole("button", { name: "New router" }).click();
  const d = dialog(page);
  await d.getByLabel("Name").fill("pres-router");
  await pick(d, "Strategy", "E2E presence");
  await d.getByLabel("pres/a", { exact: true }).check();
  await d.getByRole("button", { name: "Create" }).click();
  // Nothing is chosen yet: every required field says so (a boolean included), the optional object is left alone.
  await expect(d.getByText("Enabled is required: choose On or Off")).toBeVisible();
  await expect(d.getByText("Scope is required")).toBeVisible();
  await expect(d.getByText("Regions is required")).toBeVisible();
  await expect(d.getByText("Limits is required")).toBeVisible();
  await expect(d.getByText("Priority is required")).toHaveCount(0);
  await expect(d.getByTestId("schema-tuning")).toContainText("Not set");
  await shot(page, "router-form-presence");
  // `false`, "" (a legal enum value, labelled), the empty list and the empty object are values.
  await pick(d, "Enabled *", "Off");
  await pick(d, "Scope *", "(empty text)");
  await d.getByRole("group", { name: "Regions" }).getByRole("button", { name: "Use empty list" }).click();
  await d.getByRole("button", { name: "Add Limits" }).click();
  await d.getByRole("button", { name: "Create" }).click();
  await expect(d).toBeHidden();
  const stored = (await items<any>(request, "/routers")).find((r) => r.name === "pres-router");
  expect(stored.params).toEqual({ enabled: false, scope: "", regions: [], limits: {} });

  // An optional object with a required field of its own: asked for only once it is added.
  await editRouter(page, "pres-router");
  await d.getByRole("button", { name: "Add Tuning" }).click();
  await d.getByRole("button", { name: "Save" }).click();
  await expect(d.getByText("Priority is required")).toBeVisible();
  await d.getByLabel("Priority *", { exact: true }).fill("3");
  await d.getByRole("button", { name: "Save" }).click();
  await expect(d).toBeHidden();
  expect((await get<any>(request, `/routers/${stored.id}`)).params.tuning).toEqual({ priority: 3 });
  await editRouter(page, "pres-router");
  await d.getByRole("button", { name: "Remove Tuning" }).click();
  await d.getByRole("button", { name: "Save" }).click();
  await expect(d).toBeHidden();
  expect(Object.hasOwn((await get<any>(request, `/routers/${stored.id}`)).params, "tuning")).toBe(false);

  // An optional string: typed then emptied is "", which is not the same as unset.
  await editRouter(page, "pres-router");
  await d.getByLabel("Prefix", { exact: true }).fill("abc");
  await d.getByLabel("Prefix", { exact: true }).fill("");
  await d.getByRole("button", { name: "Save" }).click();
  await expect(d).toBeHidden();
  expect((await get<any>(request, `/routers/${stored.id}`)).params.prefix).toBe("");
  await editRouter(page, "pres-router");
  await d.getByRole("button", { name: "Unset Prefix" }).click();
  await d.getByRole("button", { name: "Save" }).click();
  await expect(d).toBeHidden();
  expect(Object.hasOwn((await get<any>(request, `/routers/${stored.id}`)).params, "prefix")).toBe(false);
  expect(a.id).toBeTruthy();
});

test("property names that are also Object.prototype members or __proto__ round-trip as data", async ({ page, request }) => {
  const up = await upstream(request, "proto-up");
  const a = await model(request, "proto/a", up.id);
  await signIn(page, request, "/routers");
  await page.getByRole("button", { name: "New router" }).click();
  const d = dialog(page);
  await d.getByLabel("Name").fill("proto-router");
  await pick(d, "Strategy", "E2E presence");
  await d.getByLabel("proto/a", { exact: true }).check();
  await pick(d, "Enabled *", "On");
  await pick(d, "Scope *", "wide");
  await d.getByRole("group", { name: "Regions" }).getByRole("button", { name: "Use empty list" }).click();
  await d.getByRole("button", { name: "Add Limits" }).click();
  // Optional and untouched: an inherited `constructor` must not read as a value or an error.
  await d.getByRole("button", { name: "Create" }).click();
  await expect(d.getByText("Must be text")).toHaveCount(0);
  await expect(d).toBeHidden();
  const created = (await items<any>(request, "/routers")).find((r) => r.name === "proto-router");
  expect(JSON.stringify(created.params)).not.toMatch(/constructor|toString|__proto__/);

  const sent = recordPatches(page);
  await editRouter(page, "proto-router");
  await d.getByLabel("Constructor", { exact: true }).fill("c");
  await d.getByLabel("To string", { exact: true }).fill("t");
  await d.getByRole("button", { name: "Add Proto" }).click();
  await d.getByLabel("Inner", { exact: true }).fill("i");
  await d.getByRole("button", { name: "Save" }).click();
  await expect(d).toBeHidden();
  const text = await (await request.get(`/api/v1/routers/${created.id}`, { headers: admin })).text();
  expect(text).toContain('"constructor":"c"');
  expect(text).toContain('"toString":"t"');
  expect(text).toContain('"__proto__":{"inner":"i"}');
  expect(sent[sent.length - 1]).toContain('"__proto__":{"inner":"i"}');
  // Reopened, the nested `__proto__` object is shown with its field, not lost into a prototype.
  await editRouter(page, "proto-router");
  await expect(d.getByLabel("Inner", { exact: true })).toHaveValue("i");
  await d.getByRole("button", { name: "Cancel" }).click();

  // The JSON editor keeps an own `__proto__` key too.
  const m = await model(request, "proto/b", up.id);
  const raw = await request.post("/api/v1/routers", {
    headers: { ...admin, "content-type": "application/json" },
    data: `{"name":"proto-json","strategy":"e2e_schemaless","params":{"__proto__":{"a":1},"constructor":2,"toString":3},"members":[{"model_id":"${m.id}"}]}`,
  });
  expect(raw.status(), await raw.text()).toBe(201);
  const jsonRouter = await raw.json();
  await page.goto("/routers");
  await editRouter(page, "proto-json");
  const editor = d.getByLabel("Settings (JSON)", { exact: true });
  const shown = await editor.inputValue();
  expect(shown).toContain('"__proto__"');
  expect(JSON.parse(shown).constructor).toBe(2);
  await editor.fill('{"__proto__": {"a": 1}, "constructor": 2, "toString": 3, "z": 4}');
  await d.getByRole("button", { name: "Save" }).click();
  await expect(d).toBeHidden();
  expect(sent[sent.length - 1]).toContain('"__proto__":{"a":1}');
  const stored = await (await request.get(`/api/v1/routers/${jsonRouter.id}`, { headers: admin })).text();
  expect(stored).toContain('"__proto__":{"a":1}');
  expect(stored).toContain('"constructor":2');
  expect(stored).toContain('"z":4');
  expect(a.id).toBeTruthy();
});

test("switching between strategies without a schema resets the JSON editors to what will be sent", async ({ page, request }) => {
  const up = await upstream(request, "sw-up");
  await model(request, "sw/a", up.id);
  await signIn(page, request, "/routers");
  await page.getByRole("button", { name: "New router" }).click();
  const d = dialog(page);
  await d.getByLabel("Name").fill("sw-router");
  await pick(d, "Strategy", "E2E schemaless");
  await d.getByLabel("sw/a", { exact: true }).check();
  const settings = d.getByLabel("Settings (JSON)", { exact: true });
  const memberSettings = d.getByLabel("Settings for sw/a (JSON)");
  await settings.fill('{"threshold": 3}');
  await memberSettings.fill('{"zone": "z1"}');

  // The other schemaless strategy starts empty, and the boxes say so.
  await pick(d, "Strategy", "E2E schemaless two");
  await expect(settings).toHaveValue("{}");
  await expect(memberSettings).toHaveValue("{}");
  const posts: any[] = [];
  page.on("request", (r) => {
    if (r.method() === "POST" && r.url().endsWith("/api/v1/routers")) posts.push(r.postDataJSON());
  });
  await pick(d, "Strategy", "E2E schemaless");
  await settings.fill("{ not json");
  await memberSettings.fill("{ also not");
  await expect(d.getByText(/JSON|Expected|Unexpected|position/).first()).toBeVisible();
  await d.getByRole("button", { name: "Create" }).click();
  await expect(d).toBeVisible();
  expect(posts).toEqual([]); // refused while the text is not JSON

  // Switching clears the problem together with the text: what is shown is what is sent.
  await pick(d, "Strategy", "E2E schemaless two");
  await expect(settings).toHaveValue("{}");
  await expect(memberSettings).toHaveValue("{}");
  await expect(d.getByText(/Expected|Unexpected|position/)).toHaveCount(0);
  await d.getByRole("button", { name: "Create" }).click();
  await expect(d).toBeHidden();
  expect(posts).toHaveLength(1);
  expect(posts[0].strategy).toBe("e2e_schemaless2");
  expect(posts[0].params).toEqual({});
  expect(posts[0].members[0].params).toEqual({});
});

test("usage filters: a catalog name is never a mode, whatever it is called", async ({ page, request }) => {
  const up = await upstream(request, "sent-up");
  const m = await model(request, "sent/a", up.id);
  const aliasNone = await post<any>(request, "/aliases", { name: "none", target: m.id });
  const aliasMarker = await post<any>(request, "/aliases", { name: "__none__", target: m.id });
  const routerHas = await post<any>(request, "/routers", { name: "__has__", members: [{ model_id: m.id }] });
  await signIn(page, request, "/usage");
  const query = (url: string) => new URL(url).searchParams;
  const next = async (action: () => Promise<unknown>, usage: (q: URLSearchParams) => boolean) => {
    const [req] = await Promise.all([page.waitForRequest((r) => r.url().includes("/api/v1/usage") && usage(query(r.url()))), action()]);
    return query(req.url());
  };

  for (const [alias, id] of [["none", aliasNone.id], ["__none__", aliasMarker.id]]) {
    const q = await next(() => pick(page, "Alias", alias), (p) => p.has("alias_id"));
    expect(q.get("alias_id")).toBe(id);
    expect([q.get("alias"), q.get("alias_missing")]).toEqual([null, null]);
  }
  let q = await next(() => pick(page, "Router", "__has__"), (p) => p.has("router_id"));
  expect(q.get("router_id")).toBe(routerHas.id);
  expect([q.get("router"), q.get("router_missing")]).toEqual([null, null]);

  // The modes are separate options with their own meaning.
  q = await next(() => pick(page, "Alias", "None (no alias)"), (p) => p.has("alias_missing"));
  expect([q.get("alias_missing"), q.get("alias_id")]).toEqual(["true", null]);
  q = await next(() => pick(page, "Alias", "Via any alias"), (p) => p.get("alias_missing") === "false");
  expect(q.get("alias_id")).toBeNull();
  q = await next(() => pick(page, "Router", "Via any router"), (p) => p.get("router_missing") === "false");
  expect(q.get("router_id")).toBeNull();
});

test("usage: the upstream model is a text filter, so a name nothing maps to any more still filters", async ({ page, request }) => {
  await signIn(page, request, "/usage");
  const box = page.getByLabel("Upstream model", { exact: true });
  // The catalog only suggests.
  await box.click();
  const suggestions = page.getByRole("listbox", { name: "Suggestions" });
  await expect(suggestions.getByRole("option", { name: "Qwen/UseA", exact: true })).toHaveCount(1);
  await expect(suggestions.getByRole("option", { name: "Org/Retired", exact: true })).toHaveCount(0);
  await expect(row(page, "use/retired")).toBeVisible();
  const [req] = await Promise.all([page.waitForRequest((r) => r.url().includes("/api/v1/usage") && r.url().includes("upstream_model=")), box.fill("Org/Retired")]);
  expect(new URL(req.url()).searchParams.get("upstream_model")).toBe("Org/Retired");
  await expect(row(page, "use/retired")).toBeVisible();
  await expect(row(page, "use/a")).toHaveCount(0);
  await box.fill("");
  await expect(row(page, "use/a")).toBeVisible();
});

test("screenshots", async ({ page, request }, info) => {
  test.skip(!SHOTS, "set E2E_SHOTS to a directory to save screenshots");
  info.setTimeout(180_000);
  const modes: [string, number, number, "light" | "dark"][] = [
    ["desktop-light", 1280, 900, "light"],
    ["desktop-dark", 1280, 900, "dark"],
    ["phone-light", 390, 844, "light"],
    ["phone-dark", 390, 844, "dark"],
  ];
  await signIn(page, request, "/models");
  for (const [label, width, height, theme] of modes) {
    await page.setViewportSize({ width, height });
    await page.evaluate((t) => localStorage.setItem("lumewarp.theme", t), theme);
    for (const [path, name] of [["/models", "models"], ["/aliases", "aliases"], ["/routers", "routers"], ["/usage", "usage"], ["/config", "config"]]) {
      await page.goto(path);
      await page.getByRole("heading").first().waitFor();
      await page.waitForTimeout(300);
      await shot(page, `${label}-${name}`);
    }
    await page.goto("/routers");
    await page.getByRole("button", { name: "New router" }).click();
    await pick(dialog(page), "Strategy", "E2E tuned");
    await dialog(page).getByLabel("Name").fill("shot");
    await page.waitForTimeout(300);
    await shot(page, `${label}-router-form`);
    await page.keyboard.press("Escape");
  }
});
