import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

/*
 * The model catalog UI through the real control plane (e2e/serve.py): Models, Aliases, Routers with
 * schema-driven strategy forms, the permission picker, My keys, Usage and Rollback.
 *
 * serve.py seeds two connected gateways (e2e-gw has every strategy, e2e-gw-b lacks `priority_failover`),
 * a small catalog (`use-*`, `use/*`) and usage events across every path a request can take.
 * Everything else is created here through the forms or the API. This file is named to sort after `smoke.spec.ts`
 * on purpose: that test lists the first page of routers and models without searching.
 */

const BREAK_GLASS = "e2e-break-glass-password";
const VIEWER = "e2e-viewer-session-0123456789abcdef";
const SHOTS = process.env.E2E_SHOTS;
const admin: Record<string, string> = {};

const dialog = (page: Page) => page.getByRole("dialog");
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
    await d.getByLabel("Upstream", { exact: true }).selectOption({ label: "cat-up" });
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
  await dialog(page).getByLabel("Points to").selectOption({ label: "cat/alpha (model)" });
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
  await d.getByLabel("Strategy").selectOption("weighted");
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
  await dialog(page).getByRole("button", { name: "Delete" }).click();
  const blocked = dialog(page).getByTestId("delete-blocked");
  await expect(blocked).toContainText("alias cat-alias");
  await expect(blocked).toContainText("router cat-router");
  await dialog(page).getByRole("button", { name: "Got it" }).click();
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
  await dialog(page).getByLabel("Points to").selectOption({ label: "cat-router (router)" });
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
  await dialog(page).getByRole("button", { name: "Delete" }).click();
  await expect(dialog(page).getByTestId("delete-blocked")).toContainText("alias cat-alias2");
  await dialog(page).getByRole("button", { name: "Got it" }).click();
});

test("reference strategy: form from its descriptor, weight hidden, Save anyway for a gateway that lacks it", async ({ page, request }) => {
  const up = await upstream(request, "pf-up");
  const one = await model(request, "pf/one", up.id);
  const two = await model(request, "pf/two", up.id);
  await signIn(page, request, "/routers");

  await page.getByRole("button", { name: "New router" }).click();
  const d = dialog(page);
  await d.getByLabel("Name").fill("pf-router");
  await d.getByLabel("Strategy").selectOption("priority_failover");
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
  await d.getByLabel("Strategy").selectOption("e2e_tuned");
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

  await d.getByLabel("Mode", { exact: true }).selectOption("relaxed");
  await d.getByLabel("Skip unpriced models").check();
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

  await d.getByLabel("Strategy").selectOption("e2e_weird");
  await expect(d.getByTestId("strategy-settings-json")).toContainText("outside what the form can show");
  await d.getByLabel("Strategy").selectOption("e2e_schemaless");
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
  await d.getByLabel("Strategy").selectOption("e2e_hostile");
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
  const options = await d.getByLabel("Pick").locator("option").allTextContents();
  expect(options.some((o) => o.includes("<img"))).toBe(true);
  await d.getByLabel("Pick").selectOption({ index: 1 });
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
  await expect(d.getByLabel("Add fallback model")).toHaveValue("");
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

test("My keys shows what a new key can call; viewers get no write controls", async ({ request, browser }) => {
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
  await member.goto("/keys");
  await member.getByRole("button", { name: "New key" }).click();
  const d = dialog(member);
  await d.getByLabel("Team").selectOption({ label: "Mine team" });
  const names = d.getByTestId("callable-names");
  await expect(names).toContainText("mine/b");
  await expect(names).not.toContainText("mine/a"); // the member's own list narrows the team's
  await expect(names.getByRole("checkbox")).toHaveCount(0); // read-only
  await expect(names.locator("input")).toHaveCount(0);
  await shot(member, "my-keys-new-key");
  await d.getByLabel("Name").fill("viewer-key");
  await d.getByRole("button", { name: "Create key" }).click();
  await expect(d.locator(".secret-value")).toBeVisible();
  await d.getByRole("button", { name: /saved it/ }).click();

  // A plain member may read the catalog but not change it: write controls are disabled.
  for (const [path, button] of [["/models", "New model"], ["/aliases", "New alias"], ["/routers", "New router"]] as const) {
    await member.goto(path);
    await expect(member.getByRole("button", { name: button })).toBeDisabled();
  }
  await member.goto("/models");
  await member.getByLabel("Search models").fill("mine/a");
  await expect(member.getByRole("row").filter({ hasText: "mine/a" }).getByRole("button", { name: "Edit" })).toBeDisabled();
  await expect(member.getByRole("row").filter({ hasText: "mine/a" }).getByRole("button", { name: "Delete" })).toBeDisabled();
  await context.close();
});

test("usage: grouped by model by default, with how each request was reached, and filters", async ({ page, request }) => {
  await signIn(page, request, "/usage");
  await expect(page.getByLabel("Group by")).toHaveValue("model");
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
  await page.getByLabel("Group by").selectOption("alias");
  await expect(page.getByRole("columnheader", { name: "Served by" })).toBeVisible();
  await expect(row(page, "use-alias")).toContainText("use/a");
  await expect(row(page, "use-alias")).toContainText("use/b");
  await expect(row(page, "none")).toContainText("use/c");
  await page.getByLabel("Group by").selectOption("router");
  await expect(row(page, "use-router")).toContainText("use/a");
  await expect(row(page, "none")).toContainText("use/c");
  await page.getByLabel("Group by").selectOption("upstream_model");
  await expect(row(page, "Qwen/UseA")).toContainText("use/a");
  await expect(row(page, "Qwen/UseA")).toContainText("use/b");
  await page.getByLabel("Group by").selectOption("model");

  // Filters: model, upstream model, alias (including none), router (including none).
  await page.getByLabel("Model", { exact: true }).selectOption("use/b");
  await expect(row(page, "use/b")).toBeVisible();
  await expect(row(page, "use/a")).toHaveCount(0);
  await page.getByLabel("Model", { exact: true }).selectOption("");
  await page.getByLabel("Upstream model", { exact: true }).selectOption("Other/UseC");
  await expect(row(page, "use/c")).toBeVisible();
  await expect(row(page, "use/a")).toHaveCount(0);
  await page.getByLabel("Upstream model", { exact: true }).selectOption("");
  await page.getByLabel("Alias", { exact: true }).selectOption({ label: "None (no alias)" });
  await expect(row(page, "use/c")).toBeVisible();
  await expect(row(page, "use/a")).toHaveCount(0);
  await expect(row(page, "use/c").getByTestId("mixed")).toHaveCount(0); // only the direct request is left
  await page.getByLabel("Alias", { exact: true }).selectOption("use-direct");
  await expect(row(page, "use/c")).toContainText("use-direct");
  await page.getByLabel("Alias", { exact: true }).selectOption("");
  await page.getByLabel("Router", { exact: true }).selectOption({ label: "None (no router)" });
  await expect(row(page, "use/c")).toBeVisible();
  await expect(row(page, "use/a")).toHaveCount(0);
  await page.getByLabel("Router", { exact: true }).selectOption("use-router");
  await expect(row(page, "use/a")).toBeVisible();
  await expect(row(page, "use/c")).toHaveCount(0);
  await page.getByLabel("Router", { exact: true }).selectOption({ label: "Via any router" });
  await expect(row(page, "use/b")).toBeVisible();
  await expect(row(page, "use/c")).toHaveCount(0);

  // Nothing matches: an empty state, not an error.
  await page.getByLabel("Alias", { exact: true }).selectOption("use-direct");
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
  for (const [path, heading] of [["/models", "Models"], ["/aliases", "Aliases"], ["/routers", "Routers"], ["/usage", "Usage"], ["/keys", "My keys"], ["/config", "Config versions"]] as const) {
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
  await d.getByLabel("Strategy").selectOption("e2e_tuned");
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
    for (const [path, name] of [["/models", "models"], ["/aliases", "aliases"], ["/routers", "routers"], ["/usage", "usage"], ["/keys", "keys"], ["/config", "config"]]) {
      await page.goto(path);
      await page.getByRole("heading").first().waitFor();
      await page.waitForTimeout(300);
      await shot(page, `${label}-${name}`);
    }
    await page.goto("/routers");
    await page.getByRole("button", { name: "New router" }).click();
    await dialog(page).getByLabel("Strategy").selectOption("e2e_tuned");
    await dialog(page).getByLabel("Name").fill("shot");
    await page.waitForTimeout(300);
    await shot(page, `${label}-router-form`);
    await page.keyboard.press("Escape");
  }
});
