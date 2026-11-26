import { expect, test, type APIRequestContext, type Browser, type Page } from "@playwright/test";

import { pick } from "./helpers";
import { Admin, MEMBER, MEMBER_EMAIL, OUTSIDER } from "./selfService";

/*
 * Alert-driven status end to end: Alertmanager payloads from two gateways posted to the control plane put a banner in
 * front of the users who may call the named model and one firing incident ("2 gateways") in front of the admin; the
 * resolved payloads take the banner away and close the incident with its duration.
 */

const TOKEN = "e2e-alertmanager-token-0123456789abcdef";
const NAME = "use-direct";
const SUMMARY = "Responses are slow, we are on it";
const STARTS = Date.now() - 20 * 60_000;
const GATEWAYS = [
  { instance: "10.0.0.1:9466", fingerprint: "e2e-incident-fp-1", startsAt: new Date(STARTS).toISOString() },
  { instance: "10.0.0.2:9466", fingerprint: "e2e-incident-fp-2", startsAt: new Date(STARTS + 20_000).toISOString() },
];
const ENDS = new Date(STARTS + 5 * 60_000).toISOString();

function payload(status: "firing" | "resolved", gateway: (typeof GATEWAYS)[number]) {
  return {
    version: "4",
    alerts: [
      {
        status,
        labels: { alertname: "E2eModelDown", severity: "critical", requested: NAME, lumewarp_status: "down", instance: gateway.instance },
        annotations: { summary: "Model is down", description: "The e2e model stopped answering", lumewarp_summary: SUMMARY },
        startsAt: gateway.startsAt,
        endsAt: status === "resolved" ? ENDS : "0001-01-01T00:00:00Z",
        generatorURL: "http://prometheus.example/graph?g0.expr=up",
        fingerprint: gateway.fingerprint,
      },
    ],
  };
}

/** Every gateway reports the same outage, each in its own delivery. */
async function post(request: APIRequestContext, status: "firing" | "resolved") {
  for (const gateway of GATEWAYS) {
    const response = await request.post("/api/v1/alerts/alertmanager", { headers: { Authorization: `Bearer ${TOKEN}` }, data: payload(status, gateway) });
    expect(response.status(), await response.text()).toBe(200);
  }
}

/** The banner's row for a name: several names fold behind "N names affected", which is expanded first. */
async function bannerRow(page: Page) {
  const banner = page.getByTestId("status-banner");
  await expect(banner).toBeVisible();
  const toggle = banner.getByTestId("status-count");
  if ((await toggle.count()) > 0 && (await toggle.getAttribute("aria-expanded")) === "false") await toggle.click();
  return banner.getByTestId("status-row").filter({ hasText: NAME });
}

async function session(browser: Browser, token: string, path: string) {
  const context = await browser.newContext({ baseURL: test.info().project.use.baseURL });
  await context.addCookies([{ name: "lumewarp_session", value: token, url: test.info().project.use.baseURL! }]);
  const page = await context.newPage();
  await page.goto(path);
  return { context, page };
}

test("a firing alert shows to the users who can call the model, the admin opens the incident, resolving clears the banner", async ({ browser, request, page }) => {
  const admin = await Admin.login(request);
  const team = await admin.ensureTeam("incidents-team");
  await admin.putMember(team.id, MEMBER_EMAIL);
  // Only a bearer token is accepted: no token is a 401.
  const refused = await request.post("/api/v1/alerts/alertmanager", { data: payload("firing", GATEWAYS[0]) });
  expect(refused.status()).toBe(401);

  await post(request, "firing");
  // Idempotent: the same deliveries again are still one incident.
  await post(request, "firing");

  const member = await session(browser, MEMBER, "/my-keys");
  const row = await bannerRow(member.page);
  await expect(row).toContainText("down");
  await expect(row).toContainText("since");
  await expect(member.page.getByTestId("status-banner")).toContainText(SUMMARY);
  // A member gets no link to an incident, and no admin text.
  await expect(member.page.getByTestId("status-incident-link")).toHaveCount(0);
  await expect(member.page.getByTestId("status-banner")).not.toContainText("The e2e model stopped answering");
  await member.context.close();

  const outsider = await session(browser, OUTSIDER, "/my-keys");
  await expect(outsider.page.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(outsider.page.getByTestId("status-banner")).toHaveCount(0);
  await outsider.context.close();

  // The admin (break-glass) sees the row and follows it to the incident.
  await page.context().addCookies((await request.storageState()).cookies);
  await page.goto("/usage");
  const adminRow = await bannerRow(page);
  await expect(adminRow).toBeVisible();
  await adminRow.getByTestId("status-incident-link").click();
  const detail = page.getByTestId("incident-detail");
  await expect(detail.getByRole("heading", { name: "Model is down", level: 1 })).toBeVisible();
  await expect(detail.getByTestId("incident-header")).toContainText("Firing");
  await expect(detail.getByTestId("incident-duration")).toContainText("Firing for");
  await expect(detail).toContainText("The e2e model stopped answering");
  await expect(detail).toContainText("E2eModelDown");
  await expect(detail).toContainText("2 gateways");
  await expect(detail.getByTestId("incident-targets")).toContainText(NAME);
  await expect(detail.getByTestId("incident-impact")).toContainText(SUMMARY);
  await expect(detail.getByTestId("incident-affected")).toContainText(NAME);
  await expect(detail.getByTestId("incident-timeline")).toContainText("10.0.0.2:9466");
  await expect(detail.getByRole("link", { name: "Open in Prometheus" })).toHaveAttribute("href", /prometheus\.example/);
  await expect(detail.getByTestId("incident-traffic")).toBeVisible();
  await expect(detail.getByTestId("incident-window")).toBeVisible();
  // The per-minute chart switches between requests, TTFT and context.
  const metric = detail.getByTestId("incident-metric");
  await metric.getByRole("tab", { name: "TTFT" }).click();
  await expect(metric.getByRole("tab", { name: "TTFT" })).toHaveAttribute("aria-selected", "true");
  await metric.getByRole("tab", { name: "Context" }).click();
  await expect(metric.getByRole("tab", { name: "Context" })).toHaveAttribute("aria-selected", "true");

  // The list shows one firing row for both gateways, filterable by status, impact and search.
  await page.getByRole("link", { name: "Incidents", exact: true }).first().click();
  await expect(page.getByRole("heading", { name: "Incidents", level: 1 })).toBeVisible();
  const listRow = page.getByRole("row").filter({ hasText: "E2eModelDown" });
  const link = listRow.getByTestId("incident-link");
  await expect(link).toHaveCount(1);
  await expect(page.getByTestId("incidents-firing")).toContainText("E2eModelDown");
  await expect(listRow.getByTestId("incident-reporters")).toHaveText("2 gateways");
  await pick(page, "Status", "Resolved");
  await expect(link).toHaveCount(0);
  await pick(page, "Status", "Firing");
  await expect(link).toHaveCount(1);
  await pick(page, "Impact", "Admin only");
  await expect(link).toHaveCount(0);
  await pick(page, "Impact", "User-visible");
  await expect(link).toHaveCount(1);
  await page.getByLabel("Search").fill("no-such-incident");
  await expect(link).toHaveCount(0);
  await page.getByLabel("Search").fill(NAME);
  await expect(link).toHaveCount(1);

  await post(request, "resolved");

  const resolved = await session(browser, MEMBER, "/my-keys");
  await expect(resolved.page.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(resolved.page.getByTestId("status-banner")).toHaveCount(0);
  await resolved.context.close();

  // The Resolved list does not poll; a reload shows the resolved row.
  await page.reload();
  await page.getByRole("row").filter({ hasText: "E2eModelDown" }).getByTestId("incident-link").click();
  await expect(page.getByTestId("incident-header")).toContainText("Resolved");
  await expect(page.getByTestId("incident-duration")).toContainText("Resolved after 5 min");
  await page.reload();
  await expect(page.getByTestId("status-banner")).toHaveCount(0);
});
