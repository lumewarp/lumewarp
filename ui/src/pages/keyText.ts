import { ApiError, type Key } from "../api";
import { fmtDateTime } from "../components/Bits";

/** A team's member-key lifetime, in plain words. */
export function fmtLifetime(seconds: number | null | undefined) {
  if (seconds === null || seconds === undefined) return "No limit";
  const days = seconds / 86_400;
  if (Number.isInteger(days)) return `${days} day${days === 1 ? "" : "s"}`;
  const hours = seconds / 3_600;
  if (Number.isInteger(hours)) return `${hours} hour${hours === 1 ? "" : "s"}`;
  return `${seconds} seconds`;
}

/** A date input's day, as the instant the key stops working (end of that day, local time). */
export function expiryFromDay(day: string) {
  return day ? new Date(`${day}T23:59:59`).toISOString() : null;
}

/**
 * The API's own refusal message, with the wait a 429 asked for. Nothing is invented: the server says why,
 * and the UI only adds the `Retry-After` seconds it sent.
 */
export function refusalText(error: unknown) {
  if (error instanceof ApiError) {
    return error.retryAfter ? `${error.message} Try again in ${error.retryAfter} seconds.` : error.message;
  }
  return error instanceof Error ? error.message : String(error);
}

/** True while the previous secret of a rotated key still authenticates. */
export function inGrace(key: Key) {
  return !!key.previous_key_expires_at && new Date(key.previous_key_expires_at) > new Date();
}

export function graceText(key: Key) {
  return `Old secret works until ${fmtDateTime(key.previous_key_expires_at!)}`;
}

/** What a rotation left behind, for the dialog that shows the new secret once. */
export function rotationNote(key: Key) {
  return inGrace(key)
    ? `The previous secret works until ${fmtDateTime(key.previous_key_expires_at!)}.`
    : "The previous secret stopped working immediately.";
}
