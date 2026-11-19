import { expect, test, type Page } from "@playwright/test";

/*
 * The Gateways page: one row per gateway process, the warnings that matter (different builds,
 * different shared-limits settings, NACKs, a quarantined config), the one-badge config priority,
 * deleting a disconnected row and per-gateway plugin support. serve.py seeds two connected gateways
 * and one disconnected one, sharing namespace prod.
 */

const BREAK_GLASS = "e2e-break-glass-password";

type GatewayRow = {
  gateway_id: string;
  connected: boolean;
  peer: string | null;
  gateway_version: string;
  capabilities: string[];
  acked_version: number | null;
  pending_version: number | null;
  last_nack_version: number | null;
  last_nack_code: string | null;
  last_nack_reason: string | null;
  connected_at: string | null;
  disconnected_at: string | null;
  last_seen_at: string | null;
  shared_limits_enabled: boolean;
  shared_limits_namespace: string;
};

async function signIn(page: Page) {
  const response = await page.request.post("/api/v1/session/break-glass", { data: { password: BREAK_GLASS } });
  expect(response.status()).toBe(204);
}

const rowOf = (page: Page, id: string) => page.getByRole("row").filter({ has: page.getByTestId(`gateway-${id}`) });

async function routeGateways(page: Page, body: { items: GatewayRow[]; next_cursor: string | null }, rows: GatewayRow[]) {
  await page.route("**/api/v1/gateways*", (route) => route.fulfill({ json: { ...body, items: rows } }));
}

async function routeVersions(page: Page, items: { version: number; quarantined: boolean }[]) {
  await page.route("**/api/v1/config/versions*", (route) => route.fulfill({ json: { items, next_cursor: null } }));
}

test("gateways: one row per process, a disconnected row is muted, and the fleet agrees", async ({ page }) => {
  await signIn(page);
  await page.goto("/gateways");
  const main = page.getByRole("main");
  for (const id of ["e2e-gw", "e2e-gw-b", "e2e-gw-old"]) {
    await expect(main.getByTestId(`gateway-${id}`)).toBeVisible();
  }
  await expect(main.locator('[data-testid^="gateway-e2e-"]')).toHaveCount(3);
  await expect(main.getByTestId("gateway-warnings")).toHaveCount(0);

  const connected = rowOf(page, "e2e-gw");
  await expect(connected).toContainText("Connected");
  await expect(connected).toContainText("Shared (prod)");
  await expect(main.getByTestId("config-e2e-gw")).toContainText(/v\d+/);

  const gone = rowOf(page, "e2e-gw-old");
  await expect(gone).toContainText("Disconnected");
  await expect(gone).toHaveClass(/text-muted-foreground/);
  // Gateways are not links: the page is the fleet, with no per-gateway detail route left.
  await expect(main.getByRole("link", { name: "e2e-gw", exact: true })).toHaveCount(0);
});

test("gateways: different builds and shared-limit settings warn; a disconnected gateway does not", async ({ page }) => {
  await signIn(page);
  const body = (await (await page.request.get("/api/v1/gateways?limit=200")).json()) as { items: GatewayRow[]; next_cursor: string | null };
  const rows = structuredClone(body.items);
  const a = rows.find((g) => g.gateway_id === "e2e-gw")!;
  const b = rows.find((g) => g.gateway_id === "e2e-gw-b")!;
  const old = rows.find((g) => g.gateway_id === "e2e-gw-old")!;
  a.connected = true;
  a.gateway_version = "0.1.0";
  a.shared_limits_enabled = true;
  a.shared_limits_namespace = "prod";
  b.connected = true;
  b.gateway_version = "0.2.0";
  b.shared_limits_enabled = false;
  b.shared_limits_namespace = "";
  old.connected = false;
  old.gateway_version = "0.0.9";
  old.shared_limits_enabled = true;
  old.shared_limits_namespace = "staging";
  await routeGateways(page, body, [a, b, old]);
  await page.goto("/gateways");

  const warnings = page.getByTestId("gateway-warnings");
  await expect(warnings).toContainText("Connected gateways run different versions: e2e-gw (0.1.0), e2e-gw-b (0.2.0).");
  await expect(warnings).toContainText("Connected gateways disagree on shared limits: e2e-gw (prod), e2e-gw-b (per gateway).");
  await expect(warnings).not.toContainText("staging");
});

test("gateways: a NACK is named with its code and reason, a quarantine is zone-wide, and an empty namespace is reported", async ({ page }) => {
  await signIn(page);
  const body = (await (await page.request.get("/api/v1/gateways?limit=200")).json()) as { items: GatewayRow[]; next_cursor: string | null };
  const rows = structuredClone(body.items);
  const a = rows.find((g) => g.gateway_id === "e2e-gw")!;
  const b = rows.find((g) => g.gateway_id === "e2e-gw-b")!;
  const old = rows.find((g) => g.gateway_id === "e2e-gw-old")!;
  a.connected = true;
  a.shared_limits_enabled = true;
  a.shared_limits_namespace = "prod";
  a.acked_version = 3;
  a.pending_version = null;
  a.last_nack_version = 6;
  a.last_nack_code = "NACK_CODE_INVALID_CONFIG";
  a.last_nack_reason = "snapshot failed validation.";
  b.connected = true;
  b.shared_limits_enabled = true;
  b.shared_limits_namespace = "";
  b.last_nack_version = null;
  old.connected = false;
  old.last_nack_version = 2;
  old.last_nack_code = "NACK_CODE_INVALID_CONFIG";
  old.last_nack_reason = "old gateway";
  await routeGateways(page, body, [a, b, old]);
  await routeVersions(page, [
    { version: 6, quarantined: true },
    { version: 5, quarantined: false },
  ]);
  await page.goto("/gateways");

  const warnings = page.getByTestId("gateway-warnings");
  await expect(warnings).toContainText("e2e-gw NACKed config v6 (NACK_CODE_INVALID_CONFIG): snapshot failed validation.");
  await expect(warnings).toContainText("Config v6 is quarantined and withheld from every gateway in this zone.");
  await expect(warnings).toContainText("e2e-gw-b (no namespace reported)");
  await expect(warnings).not.toContainText("e2e-gw-old");

  // Quarantine outranks the same gateway's NACK badge.
  await expect(page.getByTestId("config-e2e-gw")).toContainText("quarantined v6");
  await expect(page.getByTestId("config-e2e-gw")).not.toContainText("NACK");

  // A newer version supersedes the quarantine: it is no longer withheld, only the NACK remains.
  await routeVersions(page, [
    { version: 7, quarantined: false },
    { version: 6, quarantined: true },
  ]);
  await page.reload();
  await expect(warnings).toContainText("e2e-gw NACKed config v6");
  await expect(warnings).not.toContainText("quarantined");
  await expect(page.getByTestId("config-e2e-gw")).toContainText("NACK v6");
});

test("gateways: the config column shows one badge by quarantine > NACK > pending > behind", async ({ page }) => {
  await signIn(page);
  const body = (await (await page.request.get("/api/v1/gateways?limit=200")).json()) as { items: GatewayRow[]; next_cursor: string | null };
  const base = body.items.find((g) => g.gateway_id === "e2e-gw")!;
  const make = (id: string, fields: Partial<GatewayRow>): GatewayRow => ({ ...structuredClone(base), gateway_id: id, connected: true, ...fields });
  const rows = [
    make("gw-quarantined", { acked_version: 5, pending_version: 7, last_nack_version: 5, last_nack_code: "NACK_CODE_INVALID_CONFIG" }),
    make("gw-nack", { acked_version: 3, pending_version: 7, last_nack_version: 4, last_nack_code: "NACK_CODE_INVALID_CONFIG" }),
    make("gw-pending", { acked_version: 3, pending_version: 7, last_nack_version: null, last_nack_code: null, last_nack_reason: null }),
    make("gw-behind", { acked_version: 3, pending_version: null, last_nack_version: null, last_nack_code: null, last_nack_reason: null }),
  ];
  await routeGateways(page, body, rows);
  await routeVersions(page, [
    { version: 5, quarantined: true },
    { version: 4, quarantined: false },
  ]);
  await page.goto("/gateways");

  await expect(page.getByTestId("config-gw-quarantined")).toContainText("quarantined v5");
  await expect(page.getByTestId("config-gw-quarantined")).not.toContainText("NACK");
  await expect(page.getByTestId("config-gw-quarantined")).not.toContainText("pending");
  await expect(page.getByTestId("config-gw-quarantined")).not.toContainText("behind");

  await expect(page.getByTestId("config-gw-nack")).toContainText("NACK v4");
  await expect(page.getByTestId("config-gw-nack")).not.toContainText("pending");
  await expect(page.getByTestId("config-gw-nack")).not.toContainText("behind");

  await expect(page.getByTestId("config-gw-pending")).toContainText("pending v7");
  await expect(page.getByTestId("config-gw-pending")).not.toContainText("behind");

  await expect(page.getByTestId("config-gw-behind")).toContainText("behind v5");
});

test("gateways: only a disconnected row can be deleted, and a gateway that reconnected answers 409 in the dialog", async ({ page }) => {
  await signIn(page);
  await page.goto("/gateways");
  await expect(page.getByRole("tab")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /gateway token/i })).toHaveCount(0);
  await expect(rowOf(page, "e2e-gw").getByRole("button")).toHaveCount(0);

  // The row raced to connected on the server: the dialog says so and the row stays.
  await page.route("**/api/v1/gateways/default/e2e-gw-old", (route) =>
    route.request().method() === "DELETE"
      ? route.fulfill({ status: 409, json: { error: { code: "conflict", message: "gateway e2e-gw-old is connected" } } })
      : route.fallback(),
  );
  await rowOf(page, "e2e-gw-old").getByRole("button", { name: "Delete e2e-gw-old" }).click();
  const confirm = page.getByRole("alertdialog");
  await expect(confirm).toContainText("Delete the disconnected gateway");
  await confirm.getByRole("button", { name: "Delete", exact: true }).click();
  await expect(confirm.getByTestId("delete-blocked")).toContainText("gateway e2e-gw-old is connected");
  await confirm.getByRole("button", { name: "Got it" }).click();
  await expect(confirm).toHaveCount(0);
  await expect(page.getByTestId("gateway-e2e-gw-old")).toBeVisible();

  // A successful delete is mocked so the seeded fleet stays for the specs that follow; the page asks for the right row.
  const deleted: string[] = [];
  await page.unroute("**/api/v1/gateways/default/e2e-gw-old");
  await page.route("**/api/v1/gateways/default/e2e-gw-old", (route) => {
    if (route.request().method() !== "DELETE") return route.fallback();
    deleted.push(route.request().url());
    return route.fulfill({ status: 204 });
  });
  await rowOf(page, "e2e-gw-old").getByRole("button", { name: "Delete e2e-gw-old" }).click();
  await confirm.getByRole("button", { name: "Delete", exact: true }).click();
  await expect(confirm).toHaveCount(0);
  expect(deleted).toHaveLength(1);
});

test("plugins: support is per gateway, missing lists the connected gateway without it, and ids are plain text", async ({ page }) => {
  await signIn(page);
  await page.goto("/plugins");
  const main = page.getByRole("main");
  const hmac = main.getByRole("row").filter({ hasText: "hmac_signature" });
  await expect(hmac).toContainText("missing: e2e-gw-b");
  await expect(hmac.getByTestId("plugin-gateway-e2e-gw")).toHaveText("e2e-gw");
  await expect(hmac.getByTestId("plugin-missing-e2e-gw-b")).toHaveText("e2e-gw-b");
  await expect(hmac.getByTestId("plugin-gateway-e2e-gw-b")).toHaveCount(0);
  await expect(hmac.getByRole("link")).toHaveCount(0);

  const weighted = main.getByRole("row").filter({ hasText: "weighted" }).first();
  await expect(weighted).toContainText("2 / 2");
  await expect(weighted.getByTestId("plugin-gateway-e2e-gw")).toBeVisible();
  await expect(weighted.getByTestId("plugin-gateway-e2e-gw-b")).toBeVisible();
  await expect(main.getByText(/instances/i)).toHaveCount(0);
});
