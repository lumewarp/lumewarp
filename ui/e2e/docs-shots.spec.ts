import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { join } from "node:path";

import { Admin, MEMBER, SHOTS_TOKEN, signIn } from "./selfService";

/*
 * Screenshots for the manual (docs/public/screenshots/), regenerated with docs/scripts/screenshots.sh.
 * Skipped unless E2E_DOCS_SHOTS names the output directory, so `pnpm test:e2e` never runs it. With
 * that variable serve.py seeds a small, plausible installation instead of the specs' fixtures (one
 * team, three models on a vLLM upstream, a router, two weeks of traffic); this spec photographs the
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
    const row = (await admin.get<{ items: Row[] }>(`${path}?limit=200`)).items.find((item) => item.name === name);
    expect(row, `${name} is seeded`).toBeTruthy();
    return row!;
  };
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

  // The installation admin.
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
  await shot(page, "usage");
  await page.goto("/audit");
  await expect(page.getByRole("heading", { name: "Audit log" })).toBeVisible();
  await shot(page, "audit");
  await page.goto("/gateways");
  await expect(page.getByRole("heading", { name: "Gateways", level: 1 })).toBeVisible();
  await expect(page.getByTestId("instance-count-gw-1")).toHaveText("3/3 instances connected");
  await shot(page, "gateways");
  await page.goto("/gateways/gw-1");
  await expect(page.getByRole("heading", { name: "gw-1", level: 1 })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Instances", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByTestId("instance-gw-1-1")).toBeVisible();
  await shot(page, "gateway-detail");
  await page.goto("/config");
  await expect(page.getByRole("heading", { name: "Config versions" })).toBeVisible();
  await shot(page, "config-versions");
});
