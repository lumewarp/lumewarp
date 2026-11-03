import { expect, test, type APIRequestContext, type Browser, type Locator, type Page } from "@playwright/test";

/** The users and cookie sessions serve.py seeds for the self-service specs. */
export const BREAK_GLASS = "e2e-break-glass-password";
export const VIEWER = "e2e-viewer-session-0123456789abcdef";
export const MEMBER = "e2e-member-session-0123456789abcdef";
export const TEAM_ADMIN = "e2e-teamadmin-session-0123456789abcdef";
export const OUTSIDER = "e2e-outsider-session-0123456789abcdef";
export const SHOTS_TOKEN = "e2e-shots-session-0123456789abcdef";
export const VIEWER_EMAIL = "viewer@e2e.test";
export const MEMBER_EMAIL = "member@e2e.test";
export const TEAM_ADMIN_EMAIL = "teamadmin@e2e.test";
export const OUTSIDER_EMAIL = "outsider@e2e.test";
export const SHOTS_EMAIL = "shots@e2e.test";
export const WEEK = 604_800;

export type TeamRow = { id: string; name: string };
type UserRow = { id: string; email: string };

/** The control plane as break-glass, for fixtures the UI specs need. */
export class Admin {
  headers: Record<string, string> = {};

  private constructor(private request: APIRequestContext) {}

  static async login(request: APIRequestContext) {
    const response = await request.post("/api/v1/session/break-glass", { data: { password: BREAK_GLASS } });
    expect(response.status()).toBe(204);
    const session = await (await request.get("/api/v1/session")).json();
    const admin = new Admin(request);
    admin.headers = { "X-Lumewarp-CSRF": session.csrf_token };
    return admin;
  }

  async get<T>(path: string): Promise<T> {
    const response = await this.request.get(`/api/v1${path}`, { headers: this.headers });
    expect(response.status(), await response.text()).toBe(200);
    return (await response.json()) as T;
  }

  async post<T>(path: string, data: object): Promise<T> {
    const response = await this.request.post(`/api/v1${path}`, { headers: this.headers, data });
    expect(response.status(), await response.text()).toBe(201);
    return (await response.json()) as T;
  }

  async patch<T>(path: string, data: object): Promise<T> {
    const response = await this.request.patch(`/api/v1${path}`, { headers: this.headers, data });
    expect(response.status(), await response.text()).toBe(200);
    return (await response.json()) as T;
  }

  async user(email: string): Promise<UserRow> {
    const users = await this.get<{ items: UserRow[] }>("/users?limit=200");
    const user = users.items.find((item) => item.email === email);
    expect(user, `${email} exists`).toBeTruthy();
    return user!;
  }

  /** Idempotent: a test may run alone or after the one that first made the team. */
  async ensureTeam(
    name: string,
    limits: { max_keys_per_member?: number; max_member_key_lifetime_seconds?: number } = {},
  ): Promise<TeamRow> {
    const teams = await this.get<{ items: TeamRow[] }>("/teams?limit=200");
    const found = teams.items.find((team) => team.name === name);
    if (found) return found;
    return this.post<TeamRow>("/teams", { name, ...limits });
  }

  async putMember(teamId: string, email: string, role: "admin" | "member" = "member") {
    const user = await this.user(email);
    const response = await this.request.put(`/api/v1/teams/${teamId}/members/${user.id}`, {
      headers: this.headers,
      data: { role },
    });
    expect(response.ok(), await response.text()).toBe(true);
  }

  async removeMember(teamId: string, email: string) {
    const user = await this.user(email);
    const response = await this.request.delete(`/api/v1/teams/${teamId}/members/${user.id}`, { headers: this.headers });
    expect(response.status(), await response.text()).toBe(204);
  }
}

/** A cookie session's API context: the only way a spec can act as a member (no password, no OIDC). */
export async function memberSession(browser: Browser, token: string) {
  const baseURL = test.info().project.use.baseURL!;
  const context = await browser.newContext({ baseURL });
  await context.addCookies([{ name: "lumewarp_session", value: token, url: baseURL }]);
  const session = await (await context.request.get("/api/v1/session")).json();
  expect(session.authenticated).toBe(true);
  return { context, csrf: session.csrf_token as string };
}

export type CreatedKey = { secret: string; key: { id: string; display_prefix: string } };

export async function memberKey(
  browser: Browser,
  token: string,
  teamId: string,
  name: string,
  expiresAt?: string,
): Promise<CreatedKey> {
  const { context, csrf } = await memberSession(browser, token);
  try {
    const response = await context.request.post(`/api/v1/teams/${teamId}/keys`, {
      headers: { "X-Lumewarp-CSRF": csrf },
      data: expiresAt ? { name, expires_at: expiresAt } : { name },
    });
    expect(response.status(), await response.text()).toBe(201);
    return (await response.json()) as CreatedKey;
  } finally {
    await context.close();
  }
}

export async function memberRotate(browser: Browser, token: string, keyId: string, graceSeconds: number) {
  const { context, csrf } = await memberSession(browser, token);
  try {
    const response = await context.request.post(`/api/v1/keys/${keyId}/rotate`, {
      headers: { "X-Lumewarp-CSRF": csrf },
      data: { grace_seconds: graceSeconds },
    });
    expect(response.status(), await response.text()).toBe(200);
    return (await response.json()) as CreatedKey;
  } finally {
    await context.close();
  }
}

export async function memberUpdate(browser: Browser, token: string, keyId: string, body: object) {
  const { context, csrf } = await memberSession(browser, token);
  try {
    const response = await context.request.patch(`/api/v1/keys/${keyId}`, {
      headers: { "X-Lumewarp-CSRF": csrf },
      data: body,
    });
    expect(response.status(), await response.text()).toBe(200);
    return (await response.json()) as { id: string };
  } finally {
    await context.close();
  }
}

export async function signIn(page: Page, token: string, path = "/") {
  await page.context().addCookies([{ name: "lumewarp_session", value: token, url: test.info().project.use.baseURL! }]);
  await page.goto(path);
}

export const isoDay = (date: Date) => date.toISOString().slice(0, 10);
export const daysFromNow = (days: number) => new Date(Date.now() + days * 86_400_000);
export const rowOf = (scope: Page | Locator, name: string) => scope.getByRole("row").filter({ hasText: name });
