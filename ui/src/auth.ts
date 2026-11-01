import { ApiError } from "./api/errors";

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

/** Called by API clients on a 401: clear CSRF and send the user back to login. */
export function handleUnauthorized(): never {
  clearCsrfToken();
  onUnauthorized();
  throw new ApiError(401, "Session expired");
}
