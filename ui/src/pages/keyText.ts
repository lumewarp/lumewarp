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

function localDay(date: Date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

/** The local calendar day of an instant, as a date input's value. Never slice a UTC string. */
export function localDayOf(iso: string | null | undefined) {
  return iso ? localDay(new Date(iso)) : "";
}

/**
 * The last day a date input may offer: a chosen day is sent as 23:59:59 local, and the server's limit
 * is exactly `now + lifetime`. If the end of the deadline's own local day is after that instant, the
 * last acceptable day is the one before it.
 */
export function lastExpiryDay(lifetimeSeconds: number, now = new Date()) {
  const deadline = new Date(now.getTime() + lifetimeSeconds * 1000);
  const day = new Date(deadline.getTime());
  day.setHours(0, 0, 0, 0);
  for (let i = 0; i < 2; i++) {
    const end = new Date(day.getTime());
    end.setHours(23, 59, 59, 0);
    if (end <= deadline) return localDay(day);
    day.setDate(day.getDate() - 1);
  }
  return localDay(day);
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

export function isExpired(key: Key) {
  return !!key.expires_at && new Date(key.expires_at) <= new Date();
}

/** An active, unexpired key: the only state in which a secret can authenticate. */
export function keyUsable(key: Key) {
  return key.lifecycle === "active" && !isExpired(key);
}

/** A previous secret the server still retains, independent of whether the key can authenticate now. */
export function hasPreviousSecret(key: Key) {
  return !!key.previous_key_expires_at && new Date(key.previous_key_expires_at) > new Date();
}

/** True while the previous secret of a rotated, usable key still authenticates. */
export function inGrace(key: Key) {
  return keyUsable(key) && hasPreviousSecret(key);
}

export function graceText(key: Key) {
  return `Old secret works until ${fmtDateTime(key.previous_key_expires_at!)}`;
}

const suspended = (key: Key) => key.lifecycle === "suspended";
const adminSuspended = (key: Key) => key.suspended_reason === "admin";

/** One sentence per condition: suspension and expiry are checked separately and additively. */
function suspensionText(key: Key) {
  if (!suspended(key)) return null;
  if (adminSuspended(key)) {
    return "This key was suspended by an administrator: an administrator must resume it.";
  }
  if (key.suspended_reason === "owner_deactivated") {
    return "This key is suspended because its owner is inactive: the account must be reactivated before it can be resumed.";
  }
  return "This key is suspended: rotating does not resume it. Use Resume to make a new secret usable.";
}

function expiryText(key: Key) {
  return isExpired(key)
    ? "This key has expired: rotating does not extend its expiry. Edit the expiry to make a new secret usable."
    : null;
}

function graceEnd(key: Key, graceSeconds: number, now: Date) {
  const deadline = new Date(now.getTime() + graceSeconds * 1000);
  return key.expires_at && new Date(key.expires_at) < deadline ? new Date(key.expires_at) : deadline;
}

/**
 * The rotate confirmation. Suspension and expiry are separate, additive conditions; rotating changes
 * neither. The older-grace warning uses the server's retained previous secret, so a suspended key is
 * told its retention ends at once without claiming the older secret works now.
 */
export function rotationConfirmation(key: Key, graceSeconds: number, now = new Date()) {
  const parts: string[] = [];
  for (const text of [suspensionText(key), expiryText(key)]) if (text) parts.push(text);
  if (hasPreviousSecret(key)) {
    const until = fmtDateTime(key.previous_key_expires_at!);
    parts.push(
      keyUsable(key)
        ? `This key already has a previous secret in grace until ${until}. Rotating now ends that grace immediately: the older secret stops working right away.`
        : `This key still has a previous secret kept until ${until}. Rotating now ends that retention immediately: the older secret will not be available if the key is resumed.`,
    );
  }
  if (graceSeconds === 0) {
    parts.push("The old secret stops working right away.");
  } else if (keyUsable(key)) {
    parts.push(`The old secret keeps working until ${fmtDateTime(graceEnd(key, graceSeconds, now).toISOString())}, or the key's expiry if sooner.`);
  } else if (suspended(key)) {
    parts.push(
      `The old secret is kept until ${fmtDateTime(graceEnd(key, graceSeconds, now).toISOString())}, ` +
        "but neither secret can be used while the key is suspended.",
    );
  } else {
    parts.push("The old secret has already expired with the key.");
  }
  return parts.join(" ");
}

/** The success dialog: every condition that keeps the new secret unusable, not just the grace window. */
export function rotationNote(key: Key) {
  const parts: string[] = [];
  if (suspended(key)) {
    parts.push(
      adminSuspended(key)
        ? "The new secret will not work until an administrator resumes this key."
        : "The new secret will not work until this key is resumed. Rotating does not resume it; use Resume.",
    );
  }
  if (isExpired(key)) {
    parts.push("The new secret is still expired. Rotating does not extend the expiry; edit it to make the new secret usable.");
  }
  if (parts.length) {
    const kept = hasPreviousSecret(key) ? ` The previous secret is kept until ${fmtDateTime(key.previous_key_expires_at!)}.` : "";
    return parts.join(" ") + kept;
  }
  return inGrace(key)
    ? `The previous secret works until ${fmtDateTime(key.previous_key_expires_at!)}.`
    : "The previous secret stopped working immediately.";
}

/** The row note. Never claims a secret works for a suspended or expired key. */
export function rotationRowNote(key: Key) {
  if (inGrace(key)) return graceText(key);
  if (suspended(key) && hasPreviousSecret(key)) {
    return `Previous secret kept until ${fmtDateTime(key.previous_key_expires_at!)}; ${
      adminSuspended(key) ? "an administrator must resume the key" : "the key is suspended"
    }.`;
  }
  return null;
}
