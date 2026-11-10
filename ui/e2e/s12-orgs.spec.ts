import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

import { deleteTeams, pick } from "./helpers";
import {
  Admin,
  memberKey,
  memberSession,
  ORG_ADMIN,
  ORG_ADMIN_EMAIL,
  rowOf,
  signIn,
  TEAM_ADMIN,
  TEAM_ADMIN_EMAIL,
  VIEWER,
  VIEWER_EMAIL,
} from "./selfService";

/*
 * The org layer: installation admins create an org, set its policy and assign org
 * admins; an org admin reads only its own org, creates and archives teams in it, and
 * cannot change the org policy. Team admins keep the member defaults and per-member
 * exceptions, and a member edits an own key's limits up to, but not above, the owner's
 * effective limit.
 */

let admin: Admin;

const orgRow = (page: Page, name: string) =>
  page.getByRole("row").filter({ has: page.getByRole("link", { name, exact: true }) });

/** Every fixture this spec made, so the installation is back to one org for the next test. */
async function cleanup(request: APIRequestContext) {
  await deleteTeams(request, admin.headers, "s12-");
  const orgs = await admin.get<{ items: { id: string; name: string }[] }>("/orgs?limit=200");
  for (const org of orgs.items.filter((row) => row.name.startsWith("s12-"))) {
    const response = await request.delete(`/api/v1/orgs/${org.id}`, { headers: admin.headers });
    expect(response.status(), `archive org ${org.name}: ${await response.text()}`).toBe(204);
  }
}

test.beforeEach(async ({ request }) => {
  admin = await Admin.login(request);
  await cleanup(request);
});

test.afterEach(async ({ request }) => {
  await cleanup(request);
});

async function signInInstallation(page: Page, request: APIRequestContext, path: string) {
  await page.context().addCookies((await request.storageState()).cookies);
  await page.goto(path);
}

test("single-org installation hides the org layer outside the Orgs page", async ({ page, request }) => {
  const listed = await admin.get<{ items: { id: string; name: string }[] }>("/orgs?limit=200");
  expect(listed.items).toHaveLength(1);
  const only = listed.items[0];
  // One team makes the table render its header row (an empty table has none).
  await admin.ensureTeam("s12-single-org-team", {}, only.id);

  // Teams: no org column while there is only one org.
  const teamsOrgs = page.waitForResponse((r) => new URL(r.url()).pathname === "/api/v1/orgs");
  await signInInstallation(page, request, "/teams");
  await teamsOrgs;
  await expect(page.getByRole("heading", { name: "Teams", level: 1 })).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "Org", exact: true })).toHaveCount(0);

  // Audit: no org filter either.
  const auditOrgs = page.waitForResponse((r) => new URL(r.url()).pathname === "/api/v1/orgs");
  await page.goto("/audit");
  await auditOrgs;
  await expect(page.getByRole("heading", { name: "Audit log", level: 1 })).toBeVisible();
  await expect(page.getByRole("combobox", { name: "Org", exact: true })).toHaveCount(0);

  // The Orgs page itself still shows it; that is the exception in the plan.
  await page.goto("/orgs");
  await expect(page.getByRole("heading", { name: "Orgs", level: 1 })).toBeVisible();
  await expect(orgRow(page, only.name)).toBeVisible();
});

test("installation admin creates an org, sets its policy and assigns an org admin", async ({ page, request }) => {
  const name = `s12-alpha-${Date.now()}`;
  await signInInstallation(page, request, "/orgs");
  await page.getByRole("button", { name: "New org" }).click();
  const create = page.getByRole("dialog");
  await create.getByLabel("Name", { exact: true }).fill(name);
  await create.getByLabel("Requests per minute").fill("111");
  await create.getByLabel("Monthly budget (USD)").fill("5");
  await create.getByRole("button", { name: "Create" }).click();
  await expect(create).toHaveCount(0);
  await expect(orgRow(page, name)).toBeVisible();

  const listed = await admin.get<{ items: { id: string; name: string; rpm_limit: number | null; budget_nano_usd: number | null }[] }>("/orgs?limit=200");
  const created = listed.items.find((org) => org.name === name)!;
  expect(created.rpm_limit).toBe(111);
  expect(created.budget_nano_usd).toBe(5_000_000_000);

  await orgRow(page, name).getByRole("link", { name, exact: true }).click();
  await expect(page.getByRole("heading", { name, level: 1 })).toHaveText(name);
  await page.getByRole("button", { name: "Edit org" }).click();
  const edit = page.getByRole("dialog");
  await edit.getByLabel("Monthly budget (USD)").fill("7");
  await edit.getByRole("button", { name: "Save" }).click();
  await expect(edit).toHaveCount(0);
  const updated = await admin.get<{ budget_nano_usd: number | null }>(`/orgs/${created.id}`);
  expect(updated.budget_nano_usd).toBe(7_000_000_000);

  await page.getByRole("button", { name: "Add org admin" }).click();
  const assign = page.getByRole("dialog");
  await pick(assign, "User", ORG_ADMIN_EMAIL);
  await assign.getByRole("button", { name: "Add org admin" }).click();
  await expect(assign).toHaveCount(0);
  await expect(page.getByRole("row").filter({ hasText: ORG_ADMIN_EMAIL })).toBeVisible();
  const admins = await admin.get<{ items: { email: string }[] }>(`/orgs/${created.id}/admins`);
  expect(admins.items.map((row) => row.email)).toContain(ORG_ADMIN_EMAIL);

  // A second org makes the org layer appear; one org is the hidden case. The team makes
  // the table render its headers.
  await admin.post("/teams", { name: "s12-alpha-team", org_id: created.id });
  const teamsOrgs = page.waitForResponse((r) => new URL(r.url()).pathname === "/api/v1/orgs");
  await page.goto("/teams");
  await teamsOrgs;
  await expect(page.getByRole("row").filter({ hasText: "s12-alpha-team" })).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "Org", exact: true })).toBeVisible();
  const auditOrgs = page.waitForResponse((r) => new URL(r.url()).pathname === "/api/v1/orgs");
  await page.goto("/audit");
  await auditOrgs;
  await expect(page.getByRole("combobox", { name: "Org", exact: true })).toBeVisible();
  const filtered = page.waitForRequest(
    (r) => new URL(r.url()).pathname === "/api/v1/audit" && new URL(r.url()).searchParams.get("org_id") === created.id,
  );
  await pick(page, "Org", name);
  await filtered;
  await expect(page.getByRole("combobox", { name: "Org" })).toContainText(name);
});

test("org admin sees only its org, creates and archives a team there, and cannot edit the org policy", async ({ page, browser }) => {
  const own = await admin.post<{ id: string; name: string }>("/orgs", { name: "s12-org-a" });
  const foreign = await admin.post<{ id: string; name: string }>("/orgs", { name: "s12-org-b" });
  await admin.putOrgAdmin(own.id, ORG_ADMIN_EMAIL);
  await admin.post("/teams", { name: "s12-b-team", org_id: foreign.id });

  await signIn(page, ORG_ADMIN, "/orgs");
  await expect(page.getByRole("heading", { name: "Orgs", level: 1 })).toBeVisible();
  await expect(orgRow(page, own.name)).toBeVisible();
  // The other org is not in the list at all.
  await expect(page.getByRole("link", { name: foreign.name, exact: true })).toHaveCount(0);

  await orgRow(page, own.name).getByRole("link", { name: own.name, exact: true }).click();
  await expect(page.getByRole("heading", { name: own.name, level: 1 })).toBeVisible();
  // Org policy is read-only for an org admin.
  await expect(page.getByRole("button", { name: "Edit org" })).toHaveCount(0);
  await expect(page.getByText(/org policy is read-only for an org admin/i)).toBeVisible();

  await page.getByRole("button", { name: "New team" }).click();
  const create = page.getByRole("dialog");
  await create.getByLabel("Name", { exact: true }).fill("s12-a-team");
  await create.getByLabel("Requests per minute").fill("25");
  await create.getByRole("button", { name: "Create" }).click();
  await expect(create).toHaveCount(0);
  // The detail has both a Teams table (a link) and a usage table (no link) with this name.
  const teamRow = () => page.getByRole("row").filter({ has: page.getByRole("link", { name: "s12-a-team", exact: true }) });
  await expect(teamRow()).toBeVisible();
  const teams = await admin.get<{ items: { id: string; name: string; org_id: string }[] }>("/teams?limit=200");
  expect(teams.items.find((team) => team.name === "s12-a-team")?.org_id).toBe(own.id);

  await teamRow().getByRole("button", { name: "Archive" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "Archive" }).click();
  await expect(teamRow()).toHaveCount(0);

  // The archived team disappears from the Teams list too, and the foreign org's team
  // was never readable.
  await page.getByRole("link", { name: "Teams", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Teams", level: 1 })).toBeVisible();
  await expect(page.getByText("s12-a-team", { exact: false })).toHaveCount(0);
  await expect(page.getByText("s12-b-team", { exact: false })).toHaveCount(0);

  // The API refuses the policy write, not only the missing button.
  const { context, csrf } = await memberSession(browser, ORG_ADMIN);
  try {
    const denied = await context.request.patch(`/api/v1/orgs/${own.id}`, {
      headers: { "X-Lumewarp-CSRF": csrf },
      data: { budget_nano_usd: 1 },
    });
    expect(denied.status()).toBe(403);
  } finally {
    await context.close();
  }
});

test("org admin is listed in its org's teams by inheritance and owns a key there", async ({ page }) => {
  const own = await admin.post<{ id: string; name: string }>("/orgs", { name: "s12-org-inherit" });
  await admin.putOrgAdmin(own.id, ORG_ADMIN_EMAIL);
  const team = await admin.post<{ id: string; name: string }>("/teams", { name: "s12-inherit-team", org_id: own.id });

  // The members list shows the org admin as an inherited admin, with nothing to edit.
  await signIn(page, ORG_ADMIN, `/teams/${team.id}`);
  await page.getByRole("tab", { name: "Members", exact: true }).click();
  const row = rowOf(page, ORG_ADMIN_EMAIL);
  await expect(row).toContainText("inherit");
  await expect(row.getByRole("button", { name: "Edit", exact: true })).toHaveCount(0);
  await expect(row.getByRole("button", { name: "Delete", exact: true })).toHaveCount(0);

  // My keys lists the team, marked as inherited, and creates a key in it without a membership.
  await page.goto("/my-keys");
  const section = page.getByTestId("team-s12-inherit-team");
  await expect(section).toContainText("inherit");
  await section.getByRole("button", { name: "New key" }).click();
  const form = page.getByRole("dialog", { name: "New key in s12-inherit-team" });
  await form.getByLabel("Name").fill("s12-inherited");
  await form.getByRole("button", { name: "Create key" }).click();
  const secret = page.getByRole("dialog", { name: "API key created" });
  await expect(secret.getByTestId("secret-value")).toHaveText(/^lw-/);
  const keys = await admin.get<{ items: { name: string; owner_kind: string }[] }>(`/teams/${team.id}/keys`);
  expect(keys.items.map((key) => [key.name, key.owner_kind])).toContainEqual(["s12-inherited", "user"]);
});

test("team admin cannot edit team totals but edits member defaults and a member exception", async ({ page }) => {
  const team = await admin.ensureTeam("s12-settings");
  await admin.patch(`/teams/${team.id}`, { rpm_limit: 100 });
  await admin.putMember(team.id, TEAM_ADMIN_EMAIL, "admin");
  await admin.putMember(team.id, VIEWER_EMAIL);

  await signIn(page, TEAM_ADMIN, `/teams/${team.id}`);
  await page.getByRole("tab", { name: "Settings", exact: true }).click();
  await expect(page.getByRole("tabpanel")).toContainText("100 rpm");
  await page.getByRole("button", { name: "Edit team" }).click();
  const settings = page.getByRole("dialog");
  // The team's own total is read-only for a team admin.
  await expect(settings.getByLabel("Requests per minute", { exact: true })).toHaveCount(0);
  await expect(settings.getByLabel("Monthly budget (USD)")).toHaveCount(0);
  await settings.getByLabel("Member default requests per minute").fill("7");
  await settings.getByRole("button", { name: "Save" }).click();
  await expect(settings).toHaveCount(0);
  const saved = await admin.get<{ member_rpm_default: number | null }>(`/teams/${team.id}`);
  expect(saved.member_rpm_default).toBe(7);
  await expect(page.getByRole("tabpanel")).toContainText("7 requests/min");

  // A per-member exception overrides the default.
  await page.getByRole("tab", { name: "Members", exact: true }).click();
  await rowOf(page, VIEWER_EMAIL).getByRole("button", { name: "Edit", exact: true }).click();
  const member = page.getByRole("dialog");
  await member.getByLabel("Requests per minute", { exact: true }).fill("5");
  await member.getByRole("button", { name: "Save" }).click();
  await expect(member).toHaveCount(0);
  const members = await admin.get<{ items: { email: string; rpm_limit: number | null }[] }>(`/teams/${team.id}/members?limit=200`);
  expect(members.items.find((row) => row.email === VIEWER_EMAIL)?.rpm_limit).toBe(5);
});

test("a service account is deactivated and reactivated by a team admin", async ({ page }) => {
  const team = await admin.ensureTeam("s12-service");
  await admin.putMember(team.id, TEAM_ADMIN_EMAIL, "admin");
  await admin.post(`/teams/${team.id}/service-accounts`, { name: "s12-agent" });
  await signIn(page, TEAM_ADMIN, `/teams/${team.id}`);
  await page.getByRole("tab", { name: "Service accounts", exact: true }).click();
  const row = page.getByRole("row").filter({ hasText: "s12-agent" });

  await row.getByRole("button", { name: "Deactivate" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "Deactivate" }).click();
  await expect(row.getByText("Deactivated", { exact: true })).toBeVisible();
  const off = await admin.get<{ items: { name: string; is_active: boolean }[] }>(`/teams/${team.id}/service-accounts?limit=200`);
  expect(off.items.find((account) => account.name === "s12-agent")?.is_active).toBe(false);

  await row.getByRole("button", { name: "Reactivate" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "Reactivate" }).click();
  await expect(row.getByText("Active", { exact: true })).toBeVisible();
  const on = await admin.get<{ items: { name: string; is_active: boolean }[] }>(`/teams/${team.id}/service-accounts?limit=200`);
  expect(on.items.find((account) => account.name === "s12-agent")?.is_active).toBe(true);
});

test("a member edits an own key's limits within the cap and sees the 422 above it", async ({ page, browser }) => {
  const team = await admin.ensureTeam("s12-keylimits");
  await admin.patch(`/teams/${team.id}`, { member_rpm_default: 5 });
  await admin.putMember(team.id, VIEWER_EMAIL);
  const created = await memberKey(browser, VIEWER, team.id, "s12-limit-key");

  await signIn(page, VIEWER, "/my-keys");
  const section = page.getByTestId("team-s12-keylimits");
  const row = rowOf(section, "s12-limit-key");
  await row.getByRole("button", { name: "Edit", exact: true }).click();
  const edit = page.getByRole("dialog");
  // The owner's effective limit is the cap shown in the form.
  await expect(edit.getByText("At most 5 (the owner's effective limit).", { exact: false })).toBeVisible();
  await edit.getByLabel("Requests per minute").fill("5");
  await edit.getByRole("button", { name: "Save" }).click();
  await expect(edit).toHaveCount(0);
  const saved = await admin.get<{ rpm_limit: number | null }>(`/keys/${created.key.id}`);
  expect(saved.rpm_limit).toBe(5);

  await row.getByRole("button", { name: "Edit", exact: true }).click();
  const above = page.getByRole("dialog");
  await above.getByLabel("Requests per minute").fill("9");
  await above.getByRole("button", { name: "Save" }).click();
  await expect(above.getByText(/rpm_limit: must be <= owner value 5 \(got 9\)/)).toBeVisible();
  await expect(above).toBeVisible();
  await above.getByRole("button", { name: "Cancel" }).click();
  await expect(above).toHaveCount(0);
});
