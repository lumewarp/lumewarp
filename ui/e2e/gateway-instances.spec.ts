import { expect, test, type Page } from "@playwright/test";

/*
 * Gateways and Plugins with several instances per gateway id. serve.py seeds e2e-gw with two instances that share
 * rate limits (namespace prod) and e2e-gw-b with one that counts its own; the disagreement and the partly loaded
 * plugin are not seeded (they would make plugins "missing"), so those two answer from routes built on the real ones.
 */

const BREAK_GLASS = "e2e-break-glass-password";

async function signIn(page: Page) {
  const response = await page.request.post("/api/v1/session/break-glass", { data: { password: BREAK_GLASS } });
  expect(response.status()).toBe(204);
}

type Row = Record<string, unknown> & { gateway_id: string; instances: Record<string, unknown>[] };

test("gateways: instance counts, the shared badge, per-instance rows and muted disconnected ones", async ({ page }) => {
  await signIn(page);
  await page.goto("/gateways");
  const main = page.getByRole("main");
  await expect(main.getByTestId("instance-count-e2e-gw")).toHaveText("2/2 instances connected");
  await expect(main.getByTestId("instance-count-e2e-gw-b")).toHaveText("1/1 instances connected");
  await expect(main.getByRole("row").filter({ has: page.getByTestId("instance-count-e2e-gw") }).first()).toContainText("Shared (prod)");
  await expect(main.getByRole("row").filter({ has: page.getByTestId("instance-count-e2e-gw-b") })).toContainText("Per instance");
  await expect(main.getByText("Instances disagree on shared limits")).toHaveCount(0);

  await main.getByRole("button", { name: "Show instances of e2e-gw", exact: true }).click();
  const table = main.getByTestId("instances-e2e-gw");
  await expect(table.getByTestId("instance-e2e-gw-1")).toBeVisible();
  await expect(table.getByTestId("instance-e2e-gw-2")).toContainText("Connected");
});

test("gateways: instances that disagree on the namespace or the version raise a warning, a disconnected one is muted", async ({ page }) => {
  await signIn(page);
  const real = await (await page.request.get("/api/v1/gateways")).json();
  const items = (real.items ?? real) as Row[];
  const gw = structuredClone(items.find((g) => g.gateway_id === "e2e-gw")!);
  gw.instances[1] = { ...gw.instances[1], shared_limits_namespace: "staging", gateway_version: "e2e-next" };
  gw.instances.push({ ...gw.instances[0], instance_id: "e2e-gw-gone", connected: false, shared_limits_enabled: false, shared_limits_namespace: "" });
  gw.gateway_version = "e2e, e2e-next";
  await page.route("**/api/v1/gateways*", (route) => route.fulfill({ json: Array.isArray(real) ? [gw] : { ...real, items: [gw] } }));
  await page.goto("/gateways");
  const main = page.getByRole("main");
  await expect(main.getByText("Instances disagree on shared limits")).toBeVisible();
  await expect(main.getByText("Versions differ")).toBeVisible();
  await expect(main.getByTestId("instance-count-e2e-gw")).toHaveText("2/3 instances connected");
  await main.getByRole("button", { name: "Show instances of e2e-gw", exact: true }).click();
  await expect(main.getByTestId("instance-e2e-gw-gone")).toHaveClass(/text-muted-foreground/);
  await expect(main.getByTestId("instance-e2e-gw-gone")).toContainText("Disconnected");
  await expect(main.getByText("Instances disagree on shared limits (prod, staging)")).toBeVisible();
});

test("gateways: enabled on one instance only is a disagreement; an empty namespace is not invented; pending and NACK are labelled; no instances yet", async ({ page }) => {
  await signIn(page);
  const real = await (await page.request.get("/api/v1/gateways")).json();
  const items = (real.items ?? real) as Row[];
  const base = items.find((g) => g.gateway_id === "e2e-gw")!;
  const mixed = structuredClone(base);
  mixed.instances[1] = { ...mixed.instances[1], shared_limits_enabled: false, shared_limits_namespace: "" };
  const bare = structuredClone(base);
  bare.gateway_id = "e2e-bare";
  bare.instances = bare.instances.map((i, n) => ({ ...i, instance_id: `bare-${n}`, shared_limits_namespace: "" }));
  const busy = structuredClone(base);
  busy.gateway_id = "e2e-busy";
  busy.instances = [{ ...busy.instances[0], instance_id: "busy-1", acked_version: 3, pending_version: 7, last_nack_version: 6 }];
  const empty = { ...structuredClone(base), gateway_id: "e2e-empty", instances: [] as Record<string, unknown>[] };
  const all = [mixed, bare, busy, empty];
  await page.route("**/api/v1/gateways*", (route) => route.fulfill({ json: Array.isArray(real) ? all : { ...real, items: all } }));
  await page.goto("/gateways");
  const main = page.getByRole("main");
  const row = (id: string) => main.getByRole("row").filter({ has: page.getByTestId(`instance-count-${id}`) });
  await expect(row("e2e-gw")).toContainText("Instances disagree on shared limits (prod)");
  await expect(row("e2e-bare")).toContainText("Shared");
  await expect(row("e2e-bare")).not.toContainText("Shared (");
  await expect(main.getByTestId("instance-count-e2e-empty")).toHaveText("No instances yet");
  await expect(main.getByRole("button", { name: /instances of e2e-empty/ })).toHaveCount(0);

  await main.getByRole("button", { name: "Show instances of e2e-busy", exact: true }).click();
  const item = main.getByTestId("instance-busy-1");
  await expect(item).toContainText("Applied config: v3");
  await expect(item).toContainText("pending v7");
  await expect(item).toContainText("NACK v6");
  await expect(item).toContainText("Peer:");
  await expect(item).toContainText("Last seen:");
});

test("gateways: each seeded instance has its own peer", async ({ page }) => {
  await signIn(page);
  await page.goto("/gateways");
  const main = page.getByRole("main");
  await main.getByRole("button", { name: "Show instances of e2e-gw", exact: true }).click();
  const one = await main.getByTestId("instance-e2e-gw-1").textContent();
  const two = await main.getByTestId("instance-e2e-gw-2").textContent();
  const peer = (t: string | null) => /Peer: (\S+)/.exec(t ?? "")?.[1];
  expect(peer(one)).toBeTruthy();
  expect(peer(one)).not.toBe(peer(two));
});

test("plugins: a gateway appears once however many instances report the plugin, with a note on a partial load", async ({ page }) => {
  await signIn(page);
  await page.goto("/plugins");
  const main = page.getByRole("main");
  const weighted = main.getByRole("row").filter({ hasText: "weighted" }).first();
  // e2e-gw has two instances, both with the built-in: one entry, no partial note.
  await expect(weighted.getByText("e2e-gw", { exact: true })).toHaveCount(1);
  await expect(main.getByTestId("partial-e2e-gw")).toHaveCount(0);

  const real = await (await page.request.get("/api/v1/plugins")).json();
  const items = (real.items ?? real) as { name: string; gateways: { gateway_id: string; instance_id: string }[] }[];
  const body = structuredClone(items);
  const target = body.find((p) => p.name === "weighted")!;
  target.gateways = target.gateways.filter((g) => g.instance_id !== "e2e-gw-2");
  await page.route("**/api/v1/plugins*", (route) => route.fulfill({ json: Array.isArray(real) ? body : { ...real, items: body } }));
  await page.goto("/plugins");
  await expect(main.getByTestId("partial-e2e-gw").first()).toHaveText("(1/2 instances)");
});

test("plugins: an offline instance's report does not count, and a failed /gateways query says so", async ({ page }) => {
  await signIn(page);
  const real = await (await page.request.get("/api/v1/plugins")).json();
  const items = (real.items ?? real) as { name: string; gateways: { gateway_id: string; instance_id: string; connected: boolean }[] }[];
  const body = structuredClone(items);
  const target = body.find((p) => p.name === "weighted")!;
  // e2e-gw-1 has it, e2e-gw-2 does not, and a stale offline instance still reports it.
  target.gateways = [
    ...target.gateways.filter((g) => g.instance_id === "e2e-gw-1"),
    { ...target.gateways[0], gateway_id: "e2e-gw", instance_id: "e2e-gw-old", connected: false },
    ...target.gateways.filter((g) => g.gateway_id !== "e2e-gw"),
  ];
  await page.route("**/api/v1/plugins*", (route) => route.fulfill({ json: Array.isArray(real) ? body : { ...real, items: body } }));
  await page.goto("/plugins");
  const main = page.getByRole("main");
  await expect(main.getByTestId("partial-e2e-gw").first()).toHaveText("(1/2 instances)");

  await page.route("**/api/v1/gateways*", (route) => route.fulfill({ status: 500, json: { detail: "boom" } }));
  await page.goto("/plugins");
  await expect(main.getByTestId("instances-unknown").first()).toBeVisible();
});
