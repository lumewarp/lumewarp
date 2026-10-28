import { ApiError } from "./api/errors";

const KEY = "lumewarp.token";

export function getToken(): string | null {
  try {
    return sessionStorage.getItem(KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string) {
  try {
    sessionStorage.setItem(KEY, token);
  } catch {
    /* sessionStorage unavailable: the session just won't survive a reload */
  }
}

export function clearToken() {
  try {
    sessionStorage.removeItem(KEY);
  } catch {
    /* ignore */
  }
}

let csrfToken: string | null = null;

export function setCsrfToken(token: string | null) {
  csrfToken = token;
}

export function getCsrfToken() {
  return csrfToken;
}

export function clearCsrfToken() {
  csrfToken = null;
}

let onUnauthorized: () => void = () => {};

export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn;
}

/** Called by API clients on a 401: drop the token and send the user back to login. */
export function handleUnauthorized(): never {
  clearToken();
  clearCsrfToken();
  onUnauthorized();
  throw new ApiError(401, "Session expired");
}
