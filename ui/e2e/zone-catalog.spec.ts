import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

import { createModel } from "./catalog-helpers";

/*
 * The catalog per zone: lists show the header zone, a record of another zone is still reachable by id and
 * switches the header, a model is "not deployed" where it has no deployment, a router or alias nobody can
 * serve in the zone is flagged, and ModelEdit deploys and undeploys the header zone's deployment.
 * The second zone is declared by serve.py (a pool of tokens); each test takes the first one not yet archived.
 */

const BREAK_GLASS = "e2e-break-glass-password";
// An archived zone is never revived, so every test takes the next unarchived zone of serve.py's pool.
let ZONE = "";
const admin: Record<string, string> = {};

test.beforeEach(async ({ request }) => {
  const response = await request.post("/api/v1/session/break-glass", { data: { password: BREAK_GLASS } });
  expect(response.status()).toBe(204);
  admin["X-Lumewarp-CSRF"] = (await (await request.get("/api/v1/session")).json()).csrf_token;
  const zones = ((await (await request.get("/api/v1/zones?include_archived=true&limit=200", { headers: admin })).json()) as { items: { id: string; archived_at: string | null }[] }).items;
  const free = zones.find((z) => /^zc-b\d+$/.test(z.id) && !z.archived_at);
  expect(free, "no unarchived zc-b zone left in serve.py's pool").toBeTruthy();
  ZONE = free!.id;
  const named = await request.patch(`/api/v1/zones/${ZONE}`, { headers: admin, data: { name: "Zone B", color: "violet" } });
  expect(named.status(), await named.text()).toBe(200);
});

test.afterEach(async ({ request }) => {
  for (const path of ["/aliases", "/routers", "/models", "/upstreams"]) {
    const rows = (await (await request.get(`/api/v1${path}?limit=200`, { headers: admin })).json()).items as { id: string; name: string }[];
    for (const r of rows) if (r.name.startsWith("zc-")) await request.delete(`/api/v1${path}/${r.id}`, { headers: admin });
  }
  await request.delete(`/api/v1/zones/${ZONE}`, { headers: admin });
});

async function post<T = { id: string; name: string }>(request: APIRequestContext, path: string, data: object): Promise<T> {
  const r = await request.post(`/api/v1${path}`, { headers: admin, data });
  expect(r.status(), await r.text()).toBe(201);
  return (await r.json()) as T;
}
const upstream = (request: APIRequestContext, name: string, zone?: string) =>
  post<{ id: string; name: string; zone: string }>(request, "/upstreams", { name, kind: "vllm", base_url: "http://127.0.0.1:9/v1", ...(zone ? { zone } : {}) });

/** Open `path` with the header already on `zone` (the choice a tab remembers). */
async function open(page: Page, request: APIRequestContext, path: string, zone: string) {
  await page.context().addCookies((await request.storageState()).cookies);
  await page.addInitScript((z) => {
    try {
      localStorage.setItem("lumewarp.zone", z);
    } catch {
      // unavailable: the default zone shows
    }
  }, zone);
  await page.goto(path);
  await expect(page.getByTestId("app-header")).toHaveAttribute("data-zone", zone);
}

test("an upstream of another zone is hidden from the list but opens by id and switches the header", async ({ page, request }) => {
  const a = await upstream(request, "zc-up-a");
  const b = await upstream(request, "zc-up-b", ZONE);
  expect(b.zone).toBe(ZONE);
  await open(page, request, "/upstreams", "default");
  const main = page.getByRole("main");
  await expect(main.getByRole("link", { name: "zc-up-a" })).toBeVisible();
  await expect(main.getByRole("link", { name: "zc-up-b" })).toHaveCount(0);

  // Zone B's list has it, and an empty list names the zone.
  await page.getByTestId("zone-switcher").click();
  await page.getByTestId(`zone-option-${ZONE}`).click();
  await expect(main.getByRole("link", { name: "zc-up-b" })).toBeVisible();
  await expect(main.getByRole("link", { name: "zc-up-a" })).toHaveCount(0);

  // A deep link to the record of the other zone switches the header to its zone.
  await page.getByTestId("zone-switcher").click();
  await page.getByTestId("zone-option-default").click();
  await expect(page.getByTestId("app-header")).toHaveAttribute("data-zone", "default");
  await page.goto(`/upstreams/${b.id}`);
  await expect(page.getByTestId("app-header")).toHaveAttribute("data-zone", ZONE);
  await expect(main.getByRole("heading", { name: "zc-up-b", level: 1 })).toBeVisible();
  await expect(main.getByText("Zone Zone B")).toBeVisible();
  void a;
});

test("a new upstream is created in the header zone, named in the title", async ({ page, request }) => {
  await open(page, request, "/upstreams/new", ZONE);
  const main = page.getByRole("main");
  await expect(main.getByRole("heading", { name: "New upstream in Zone B", level: 1 })).toBeVisible();
  await main.getByLabel("Name", { exact: true }).fill("zc-made-in-b");
  await main.getByLabel("Base URL").fill("http://127.0.0.1:9/v1");
  await main.getByRole("button", { name: "Create" }).click();
  await expect(page).toHaveURL(/\/upstreams$/);
  const stored = ((await (await request.get("/api/v1/upstreams?limit=200", { headers: admin })).json()).items as { name: string; zone: string }[]).find((u) => u.name === "zc-made-in-b");
  expect(stored?.zone).toBe(ZONE);
});

test("a model deployed only in one zone is 'not deployed' in the other, and a router/alias without a member is flagged", async ({ page, request }) => {
  const up = await upstream(request, "zc-up");
  const m = await createModel(request, admin, { name: "zc-only-a", upstream_id: up.id, upstream_model: "m" });
  const router = await post(request, "/routers", { name: "zc-router", strategy: "weighted", params: {}, members: [{ model_id: m.id, weight: 1, params: {} }] });
  const alias = await post(request, "/aliases", { name: "zc-alias", target: m.id });

  await open(page, request, "/models", "default");
  const main = page.getByRole("main");
  await expect(page.getByTestId("deployments-zc-only-a")).toContainText("default");
  await expect(page.getByTestId("not-deployed-zc-only-a")).toHaveCount(0);

  await open(page, request, "/models", ZONE);
  await expect(page.getByTestId("not-deployed-zc-only-a")).toContainText("not deployed in Zone B");

  await page.goto("/routers");
  await expect(page.getByRole("row").filter({ hasText: "zc-router" }).getByTestId("not-available")).toContainText("not available in Zone B");
  await page.goto(`/routers/${router.id}`);
  await expect(main.getByTestId("not-available-warning")).toContainText("Not available in Zone B");

  await page.goto("/aliases");
  await expect(page.getByRole("row").filter({ hasText: "zc-alias" }).getByTestId("not-available")).toBeVisible();
  await page.goto(`/aliases/${alias.id}`);
  await expect(main.getByTestId("not-available-warning")).toBeVisible();

  // In the zone where it is deployed nothing is flagged.
  await open(page, request, "/routers", "default");
  await expect(page.getByRole("row").filter({ hasText: "zc-router" }).getByTestId("not-available")).toHaveCount(0);
});

test("ModelEdit deploys and undeploys the model in the header zone, and lists other zones read-only", async ({ page, request }) => {
  const upA = await upstream(request, "zc-dep-a");
  const upB = await upstream(request, "zc-dep-b", ZONE);
  const m = await createModel(request, admin, { name: "zc-dep", upstream_id: upA.id, upstream_model: "m-a" });
  const deployments = async () => ((await (await request.get("/api/v1/models?limit=200", { headers: admin })).json()).items as { name: string; deployments: { zone: string; upstream_id: string; upstream_model: string }[] }[]).find((x) => x.name === "zc-dep")!.deployments;

  await open(page, request, `/models/${m.id}`, ZONE);
  const main = page.getByRole("main");
  await expect(main.getByTestId("not-deployed")).toBeVisible();
  // Zone A's deployment is listed read-only, with a way to switch to it.
  await expect(main.getByText("Deployments in other zones", { exact: true })).toBeVisible();
  await expect(main.getByText("zc-dep-a/m-a")).toBeVisible();

  await main.getByRole("switch", { name: "Deploy in this zone" }).click();
  // Only this zone's upstreams are offered.
  await main.getByRole("combobox", { name: "Upstream", exact: true }).click();
  await expect(page.getByRole("option", { name: "zc-dep-a" })).toHaveCount(0);
  await page.getByRole("option", { name: "zc-dep-b" }).click();
  await main.getByRole("combobox", { name: "Upstream model", exact: true }).fill("m-b");
  await page.getByRole("option", { name: "Use “m-b” (custom)" }).click();
  await main.getByRole("button", { name: "Save" }).click();
  await expect(page).toHaveURL(/\/models$/);
  expect((await deployments()).map((d) => [d.zone, d.upstream_model]).sort()).toEqual([["default", "m-a"], [ZONE, "m-b"]]);
  void upB;

  // Undeploy again.
  await page.goto(`/models/${m.id}`);
  await expect(page.getByTestId("app-header")).toHaveAttribute("data-zone", ZONE);
  await main.getByRole("switch", { name: "Deploy in this zone" }).click();
  await main.getByRole("button", { name: "Save" }).click();
  await expect(page).toHaveURL(/\/models$/);
  expect((await deployments()).map((d) => d.zone)).toEqual(["default"]);

  // Switching to another zone's deployment from the list.
  await page.goto(`/models/${m.id}`);
  await main.getByRole("button", { name: "Switch to zone default" }).click();
  await expect(page.getByTestId("app-header")).toHaveAttribute("data-zone", "default");
  await expect(main.getByText("Deployment in default", { exact: true })).toBeVisible();
});
