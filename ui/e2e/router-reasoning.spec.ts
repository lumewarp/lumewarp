import { expect, test, type APIRequestContext } from "@playwright/test";

import { control, pick, shot } from "./helpers";
import { signIn, VIEWER } from "./selfService";

/* A router's default reasoning level: only levels every member accepts, saved, reloaded, and offered in the Playground. */

const BREAK_GLASS = "e2e-break-glass-password";
const admin: Record<string, string> = {};

test.beforeEach(async ({ request }) => {
  expect((await request.post("/api/v1/session/break-glass", { data: { password: BREAK_GLASS } })).status()).toBe(204);
  admin["X-Lumewarp-CSRF"] = (await (await request.get("/api/v1/session")).json()).csrf_token;
});

test.afterEach(async ({ request }) => {
  for (const path of ["/routers", "/models", "/upstreams"]) {
    const rows = (await (await request.get(`/api/v1${path}?limit=200`, { headers: admin })).json()).items as { id: string; name: string }[];
    for (const r of rows) if (r.name.startsWith("rr-")) await request.delete(`/api/v1${path}/${r.id}`, { headers: admin });
  }
});

async function mappedModel(request: APIRequestContext, upstreamId: string, name: string, levels: string[]) {
  const fragments = Object.fromEntries(levels.map((l) => [l, { chat_template_kwargs: { enable_thinking: l !== "none" } }]));
  const response = await request.post("/api/v1/models", {
    headers: admin,
    data: { model_type: "llm", name, upstream_id: upstreamId, upstream_model: "qwen", reasoning: { mode: "mapped", fragments, default_level: null } },
  });
  expect(response.status()).toBe(201);
  return (await response.json()) as { id: string };
}

test("router reasoning: the default level is limited to the shared levels and persists", async ({ page, request }) => {
  const up = await (await request.post("/api/v1/upstreams", { headers: admin, data: { name: "rr-up", kind: "vllm", base_url: "http://127.0.0.1:9/v1" } })).json();
  const a = await mappedModel(request, up.id, "rr-a", ["none", "low"]);
  const b = await mappedModel(request, up.id, "rr-b", ["low", "high"]);
  const created = await request.post("/api/v1/routers", {
    headers: admin,
    data: {
      name: "rr-router",
      strategy: "weighted",
      params: {},
      members: [
        { model_id: a.id, weight: 1, params: {} },
        { model_id: b.id, weight: 1, params: {} },
      ],
    },
  });
  expect(created.status()).toBe(201);
  const router = await created.json();
  expect(router.accepted_reasoning_levels).toEqual(["low"]);

  await page.context().addCookies((await request.storageState()).cookies);
  await page.goto(`/routers/${router.id}`);
  const main = page.locator("#main");
  const level = await control(main, "Default level");
  await level.click();
  const popup = page.locator(`[id="${await level.getAttribute("aria-controls")}"]`);
  await expect(popup.getByRole("option")).toHaveText(["Not set", "low"]);
  await page.keyboard.press("Escape");
  await pick(main, "Default level", "low");
  await shot(page, "router-reasoning");
  await main.getByRole("button", { name: "Save" }).click();
  await expect(page).toHaveURL(/\/routers$/);

  const stored = await (await request.get(`/api/v1/routers/${router.id}`, { headers: admin })).json();
  expect(stored.reasoning_default_level).toBe("low");
  await page.goto(`/routers/${router.id}`);
  await expect(await control(main, "Default level")).toContainText("low");

  // The Playground offers the level picker for the router, defaulting to the router's level.
  // The e2e control plane has no gateway, so the Playground API is answered here with what the saved router implies.
  await page.route(
    (url) => url.pathname === "/api/v1/playground",
    (route) =>
      route.fulfill({
        json: {
          enabled: true,
          keys: [{ id: "11111111-1111-4111-8111-111111111111", name: "rr-key", display_prefix: "lw_rr01", team_id: "22222222-2222-4222-8222-222222222222", team_name: "rr-team", models: ["rr-router"], owner_kind: "user", service_account_name: null }],
          reasoning: { "rr-router": { levels: stored.accepted_reasoning_levels, default_level: stored.reasoning_default_level } },
        },
      }),
  );
  await signIn(page, VIEWER, "/playground");
  const effort = await control(page.locator("#main"), "Reasoning effort");
  await expect(effort).toContainText("Model default (low)");
  await effort.click();
  await expect(page.getByRole("option", { name: "low", exact: true })).toBeVisible();
});
