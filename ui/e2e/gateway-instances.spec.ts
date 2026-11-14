import { expect, test, type Page } from "@playwright/test";

/*
 * Gateways, one gateway's detail page and Plugins with several instances per gateway id. serve.py seeds e2e-gw
 * with two instances that share rate limits (namespace prod) and e2e-gw-b with one that counts its own; the
 * disagreement, the odd instance and the partly loaded plugin are not seeded (they would make plugins "missing"),
 * so those answer from routes built on the real ones.
 */

const BREAK_GLASS = "e2e-break-glass-password";

async function signIn(page: Page) {
  const response = await page.request.post("/api/v1/session/break-glass", { data: { password: BREAK_GLASS } });
  expect(response.status()).toBe(204);
}

type Row = Record<string, unknown> & { gateway_id: string; instances: Record<string, unknown>[] };

const rowOf = (page: Page, testId: string) => page.getByRole("row").filter({ has: page.getByTestId(testId) });

async function openGateway(page: Page, id: string) {
  await page.getByTestId(`gateway-link-${id}`).click();
  await expect(page.getByRole("heading", { name: id, level: 1 })).toBeVisible();
}

test("gateways: counts and shared limits in the list, instances with headers on the detail page", async ({ page }) => {
  await signIn(page);
  await page.goto("/gateways");
  const main = page.getByRole("main");
  await expect(main.getByTestId("instance-count-e2e-gw")).toHaveText("2/2 instances connected");
  await expect(main.getByTestId("instance-count-e2e-gw-b")).toHaveText("1/1 instances connected");
  await expect(rowOf(page, "instance-count-e2e-gw")).toContainText("Shared (prod)");
  await expect(rowOf(page, "instance-count-e2e-gw-b")).toContainText("Per instance");
  await expect(main.getByText("Mixed", { exact: true })).toHaveCount(0);
  await expect(main.getByText("Needs attention")).toHaveCount(0);

  await openGateway(page, "e2e-gw");
  const instances = main.getByRole("tabpanel", { name: "Instances" });
  for (const header of ["Instance", "State", "Version", "Applied config", "Connected since", "Last seen", "Peer"]) {
    await expect(instances.getByRole("columnheader", { name: header, exact: true })).toBeVisible();
  }
  await expect(instances.getByTestId("instance-e2e-gw-1")).toBeVisible();
  await expect(rowOf(page, "instance-e2e-gw-2")).toContainText("Connected");
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
  await expect(main.getByTestId("instance-count-e2e-gw")).toHaveText("2/3 instances connected");
  await expect(rowOf(page, "instance-count-e2e-gw")).toContainText("Mixed");
  await expect(rowOf(page, "instance-count-e2e-gw")).toContainText("Needs attention");

  await openGateway(page, "e2e-gw");
  await expect(main.getByTestId("gateway-warnings")).toContainText("Versions differ: e2e, e2e-next");
  await expect(main.getByText("Instances disagree on shared limits (prod, staging)")).toBeVisible();
  const gone = main.getByTestId("instance-e2e-gw-gone");
  await expect(gone).toHaveClass(/text-muted-foreground/);
  await expect(rowOf(page, "instance-e2e-gw-gone")).toContainText("Disconnected");
});

test("gateways: one instance sharing is a disagreement; an empty namespace is not invented; pending and NACK are labelled; no instances yet", async ({ page }) => {
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
  busy.acked_version = 3;
  busy.pending_version = 7;
  busy.last_nack_version = 6;
  busy.instances = [{ ...busy.instances[0], instance_id: "busy-1", acked_version: 3, pending_version: 7, last_nack_version: 6 }];
  const empty = { ...structuredClone(base), gateway_id: "e2e-empty", instances: [] as Record<string, unknown>[] };
  const all = [mixed, bare, busy, empty];
  await page.route("**/api/v1/gateways*", (route) => route.fulfill({ json: Array.isArray(real) ? all : { ...real, items: all } }));
  await page.goto("/gateways");
  const main = page.getByRole("main");
  await expect(rowOf(page, "instance-count-e2e-gw")).toContainText("Mixed");
  await expect(rowOf(page, "instance-count-e2e-gw")).toContainText("Needs attention");
  await expect(rowOf(page, "instance-count-e2e-bare")).toContainText("Shared");
  await expect(rowOf(page, "instance-count-e2e-bare")).not.toContainText("Shared (");
  await expect(main.getByTestId("instance-count-e2e-empty")).toHaveText("No instances yet");
  await expect(main.getByTestId("gateway-link-e2e-empty")).toBeVisible();
  await expect(rowOf(page, "instance-count-e2e-busy")).toContainText("v3");
  await expect(rowOf(page, "instance-count-e2e-busy")).toContainText("NACK v6");

  await openGateway(page, "e2e-gw");
  await expect(main.getByText("Instances disagree on shared limits (prod)")).toBeVisible();

  await page.goto("/gateways/e2e-busy");
  const instances = main.getByRole("tabpanel", { name: "Instances" });
  for (const header of ["Applied config", "Last seen", "Peer"]) {
    await expect(instances.getByRole("columnheader", { name: header, exact: true })).toBeVisible();
  }
  const item = rowOf(page, "instance-busy-1");
  await expect(item).toContainText("v3");
  await expect(item).toContainText("pending v7");
  await expect(item).toContainText("NACK v6");
  await expect(item.getByTestId("peer-busy-1")).not.toHaveText("—");
  await expect(item.getByTestId("last-seen-busy-1")).not.toHaveText("—");

  await page.goto("/gateways/e2e-empty");
  await expect(main.getByText("No instances yet.")).toBeVisible();
});

test("gateways: the odd instance out is marked, and each seeded instance has its own peer", async ({ page }) => {
  await signIn(page);
  const real = await (await page.request.get("/api/v1/gateways")).json();
  const items = (real.items ?? real) as Row[];
  const gw = structuredClone(items.find((g) => g.gateway_id === "e2e-gw")!);
  gw.instances.push({ ...gw.instances[0], instance_id: "e2e-gw-3", gateway_version: "e2e-next", shared_limits_namespace: "staging" });
  gw.gateway_version = "e2e, e2e-next";
  await page.route("**/api/v1/gateways*", (route) => route.fulfill({ json: Array.isArray(real) ? [gw] : { ...real, items: [gw] } }));
  await page.goto("/gateways/e2e-gw");
  const main = page.getByRole("main");
  await expect(main.getByTestId("odd-instance-e2e-gw-3")).toHaveText("Out of step");
  await expect(main.getByTestId("odd-instance-e2e-gw-1")).toHaveCount(0);
  await expect(main.getByTestId("odd-instance-e2e-gw-2")).toHaveCount(0);
  await expect(main.getByTestId("gateway-warnings")).toContainText("Out of step with most connected instances (version or shared limits): e2e-gw-3.");

  // An instance that reported no build version is not out of step for that alone.
  gw.instances[2] = { ...gw.instances[0], instance_id: "e2e-gw-3", gateway_version: "" };
  gw.gateway_version = "e2e";
  await page.reload();
  await expect(main.getByTestId("instance-e2e-gw-3")).toBeVisible();
  await expect(main.getByTestId("odd-instance-e2e-gw-3")).toHaveCount(0);
  await expect(main.getByTestId("gateway-warnings")).toHaveCount(0);

  await page.unroute("**/api/v1/gateways*");
  await page.goto("/gateways/e2e-gw");
  const peer = async (id: string) => page.getByTestId(`peer-${id}`).textContent();
  const one = await peer("e2e-gw-1");
  const two = await peer("e2e-gw-2");
  expect(one).toBeTruthy();
  expect(one).not.toBe(two);
});

test("plugins: a gateway appears once however many instances report the plugin, with a note on a partial load", async ({ page }) => {
  await signIn(page);
  await page.goto("/plugins");
  const main = page.getByRole("main");
  const weighted = main.getByRole("row").filter({ hasText: "weighted" }).first();
  // e2e-gw has two instances, both with the built-in: one entry, no partial note.
  await expect(weighted.getByText("e2e-gw", { exact: true })).toHaveCount(1);
  await expect(weighted.getByTestId("plugin-gateway-e2e-gw")).toHaveAttribute("href", "/gateways/e2e-gw");
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

test("gateway detail: a token is created with the gateway id prefilled and can be revoked", async ({ page }) => {
  await signIn(page);
  await page.goto("/gateways/e2e-gw");
  await page.getByRole("button", { name: "New token", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "New gateway token" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText("e2e-gw", { exact: true })).toBeVisible();
  // The id is fixed by the page: there is no input to change it.
  await expect(dialog.getByLabel("Gateway id")).toHaveCount(0);
  await dialog.getByLabel("Label").fill("revoke-me");
  await dialog.getByRole("button", { name: "Create token" }).click();

  const secret = page.getByRole("dialog", { name: "Gateway token created" });
  await expect(secret).toBeVisible();
  await secret.getByRole("button", { name: /saved it/ }).click();
  await expect(secret).toHaveCount(0);

  // Creating a token shows the Tokens tab, where the new one is.
  await expect(page.getByRole("tab", { name: "Tokens", exact: true })).toHaveAttribute("aria-selected", "true");
  const tokens = page.getByRole("tabpanel", { name: "Tokens" });
  const row = tokens.getByRole("row").filter({ hasText: "revoke-me" });
  await expect(row).toContainText("Active");
  await row.getByRole("button", { name: "Revoke" }).click();
  const confirm = page.getByRole("alertdialog");
  await expect(confirm).toContainText("The gateway using this token can no longer connect");
  await confirm.getByRole("button", { name: "Revoke", exact: true }).click();
  await expect(confirm).toHaveCount(0);
  await expect(row).toContainText("Revoked");
  await expect(row.getByRole("button", { name: "Revoke" })).toHaveCount(0);
});

test("gateway detail: an unknown id is a not-found page with a way back", async ({ page }) => {
  await signIn(page);
  await page.goto("/gateways/e2e-no-such-gateway");
  const main = page.getByRole("main");
  await expect(main.getByText("No gateway with this id")).toBeVisible();
  await main.getByRole("link", { name: "All gateways" }).first().click();
  await expect(page).toHaveURL(/\/gateways$/);
});

test("gateway tokens: a server error stays in the fixed-id dialog; the list dialog reopens empty", async ({ page }) => {
  await signIn(page);
  await page.route("**/api/v1/gateways", (route) =>
    route.request().method() === "POST" ? route.fulfill({ status: 500, json: { detail: "token store unavailable" } }) : route.fallback(),
  );
  await page.goto("/gateways/e2e-gw");
  await page.getByRole("button", { name: "New token", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "New gateway token" });
  await dialog.getByLabel("Label").fill("fails");
  await dialog.getByRole("button", { name: "Create token" }).click();
  await expect(dialog.getByRole("alert")).toBeVisible();
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await page.getByRole("button", { name: "New token", exact: true }).click();
  await expect(dialog.getByLabel("Label")).toHaveValue("");
  await expect(dialog.getByRole("alert")).toHaveCount(0);
  await dialog.getByRole("button", { name: "Cancel" }).click();

  await page.goto("/gateways");
  await page.getByRole("button", { name: "New gateway token" }).click();
  await dialog.getByLabel("Gateway id").fill("typed-id");
  await dialog.getByLabel("Label").fill("typed-label");
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await page.getByRole("button", { name: "New gateway token" }).click();
  await expect(dialog.getByLabel("Gateway id")).toHaveValue("");
  await expect(dialog.getByLabel("Label")).toHaveValue("");
});
