import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { join } from "node:path";

import { Admin, MEMBER, SHOTS_TOKEN, signIn } from "./selfService";

/*
 * Screenshots for the manual (docs/public/screenshots/), regenerated with docs/scripts/screenshots.sh.
 * Skipped unless E2E_DOCS_SHOTS names the output directory, so `pnpm test:e2e` never runs it. With
 * that variable serve.py seeds a small, plausible installation instead of the specs' fixtures (one
 * team, two zones with upstreams and deployments, three global models, a router, and two weeks of
 * traffic in both zones); this spec photographs the
 * pages the manual describes, at one desktop size, light theme, without scrolling.
 */

const OUT = process.env.E2E_DOCS_SHOTS;
test.skip(!OUT, "E2E_DOCS_SHOTS is not set");
test.use({ viewport: { width: 1280, height: 800 }, colorScheme: "light" });

/** `height` makes room for a long form: a full-page capture would cut the sticky sidebar and footer. */
async function shot(page: Page, name: string, height = 800) {
  await page.setViewportSize({ width: 1280, height });
  // Fonts, query refetches and enter animations settle before the picture.
  await page.waitForLoadState("networkidle");
  await page.waitForTimeout(400);
  await page.screenshot({ path: join(OUT!, `${name}.png`) });
}

type Row = { id: string; name: string };

/** The ids of what serve.py seeded in docs mode. */
async function seeded(request: APIRequestContext) {
  const admin = await Admin.login(request);
  const find = async (path: string, name: string) => {
    const deadline = Date.now() + 30_000;
    let items: Row[] = [];
    let row: Row | undefined;
    while (!row && Date.now() < deadline) {
      items = (await admin.get<{ items: Row[] }>(`${path}?limit=200`)).items;
      row = items.find((item) => item.name === name);
      if (!row) await new Promise<void>((resolve) => setTimeout(resolve, 100));
    }
    expect(row, `${name} is seeded at ${path}; got ${JSON.stringify(items)}`).toBeTruthy();
    return row!;
  };
  // serve.py writes the gateway rows, the shots user's admin role and the traffic in its last
  // transaction, after the catalog: wait for the rows so the pictures see a finished seed.
  const deadline = Date.now() + 60_000;
  let gateways: unknown[] = [];
  while (gateways.length === 0 && Date.now() < deadline) {
    gateways = (await admin.get<{ items: unknown[] }>("/gateways?zone=default&limit=200")).items;
    if (gateways.length === 0) await new Promise<void>((resolve) => setTimeout(resolve, 200));
  }
  expect(gateways.length, "serve.py seeded the default zone's gateways").toBeGreaterThan(0);
  return {
    team: await find("/teams", "search"),
    upstream: await find("/upstreams", "vllm-gpu-a"),
    router: await find("/routers", "chat"),
  };
}

test("manual screenshots", async ({ page, request, browser }) => {
  const { team, upstream, router } = await seeded(request);

  // Signed out.
  await page.goto("/login");
  await expect(page.getByRole("button").first()).toBeVisible();
  await shot(page, "login");

  // The installation admin first: creating the member's key below publishes one more config
  // version, and the Gateways picture is of the fleet as the fixture seeded it.
  await signIn(page, SHOTS_TOKEN, `/teams/${team.id}`);
  await expect(page.getByRole("heading", { name: "search", exact: true })).toBeVisible();
  await shot(page, "team");
  await page.goto("/models");
  await expect(page.getByRole("cell", { name: "llama-3.1-8b", exact: true })).toBeVisible();
  await shot(page, "models");
  await page.goto(`/upstreams/${upstream.id}`);
  await expect(page.getByRole("heading", { name: "vllm-gpu-a", exact: true })).toBeVisible();
  await shot(page, "upstream-edit", 1400);
  await page.goto(`/routers/${router.id}`);
  await expect(page.getByRole("heading", { name: "chat", exact: true })).toBeVisible();
  await shot(page, "router-edit", 1300);
  await page.goto("/usage");
  await expect(page.getByRole("heading", { name: "Usage" })).toBeVisible();
  await page.getByLabel("Zone", { exact: true }).click();
  await page.getByRole("option", { name: "All zones", exact: true }).click();
  await shot(page, "usage");
  await page.getByTestId("usage-lens").getByRole("button", { name: "Cost", exact: true }).click();
  await expect(page.getByTestId("insight-spend")).toBeVisible();
  await shot(page, "usage-cost");
  await page.getByTestId("usage-lens").getByRole("button", { name: "Usage", exact: true }).click();
  // The seeded traffic spans two weeks; the page defaults to 24 hours, which may be an empty window.
  const to = new Date().toISOString();
  const from = new Date(Date.now() - 14 * 86_400_000).toISOString();
  await page.goto(`/requests?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`);
  await expect(page.getByRole("heading", { name: "Requests" })).toBeVisible();
  await page.getByLabel("Zone", { exact: true }).click();
  await page.getByRole("option", { name: "All zones", exact: true }).click();
  await expect(page.getByTestId("request-chain").first()).toBeVisible();
  await shot(page, "requests");
  await page.getByTestId("request-requested").getByText("chat", { exact: true }).first().click();
  await expect(page.getByTestId("request-timeline")).toBeVisible();
  await shot(page, "request-detail");
  await page.keyboard.press("Escape");
  await page.goto("/audit");
  await expect(page.getByRole("heading", { name: "Audit log" })).toBeVisible();
  await shot(page, "audit");
  await page.getByTestId("zone-switcher").click();
  await expect(page.getByTestId("zone-option-prod")).toBeVisible();
  await shot(page, "zone-switcher");
  await page.keyboard.press("Escape");
  await page.goto("/zones");
  await expect(page.getByRole("heading", { name: "Zones", level: 1 })).toBeVisible();
  await expect(page.getByText("Production", { exact: true })).toBeVisible();
  await shot(page, "zones");
  await page.goto("/gateways");
  await expect(page.getByRole("heading", { name: "Gateways", level: 1 })).toBeVisible();
  await expect(page.getByTestId("gateway-gw-a-7f3c1d2e")).toBeVisible();
  const gatewayRow = page.getByRole("row").filter({ has: page.getByTestId("gateway-gw-a-7f3c1d2e") });
  await expect(gatewayRow).toContainText(/v\d+/);
  await shot(page, "gateways");
  await page.goto("/config");
  await expect(page.getByRole("heading", { name: "Config versions" })).toBeVisible();
  await shot(page, "config-versions");

  // A member: their own keys, then the Playground.
  const member = await browser.newPage({ viewport: { width: 1280, height: 800 }, colorScheme: "light" });
  await signIn(member, MEMBER, "/my-keys");
  await expect(member.getByTestId("team-search")).toBeVisible();
  await member.getByTestId("team-search").getByRole("button", { name: "New key" }).click();
  const form = member.getByRole("dialog", { name: "New key in search" });
  await form.getByLabel("Name").fill("laptop");
  await shot(member, "my-keys-create");
  await form.getByRole("button", { name: "Create key" }).click();
  const secret = member.getByRole("dialog", { name: "API key created" });
  await expect(secret.getByTestId("secret-value")).toBeVisible();
  await shot(member, "my-keys-secret");
  await secret.getByRole("button", { name: /saved it/ }).click();
  await expect(secret).toHaveCount(0);
  await shot(member, "my-keys");
  await member.goto("/playground");
  await expect(member.getByRole("heading", { name: "Playground" })).toBeVisible();
  await shot(member, "playground");
  await member.close();
});
