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

/** True while the previous secret of a rotated, usable key still authenticates. */
export function inGrace(key: Key) {
  return keyUsable(key) && !!key.previous_key_expires_at && new Date(key.previous_key_expires_at) > new Date();
}

export function graceText(key: Key) {
  return `Old secret works until ${fmtDateTime(key.previous_key_expires_at!)}`;
}

const suspended = (key: Key) => key.lifecycle === "suspended";

function graceEnd(key: Key, graceSeconds: number, now: Date) {
  const deadline = new Date(now.getTime() + graceSeconds * 1000);
  return key.expires_at && new Date(key.expires_at) < deadline ? new Date(key.expires_at) : deadline;
}

/**
 * The rotate confirmation. It never promises that a secret "works" for a suspended or expired key:
 * rotating does not change the lifecycle or the expiry, and it says what does (Resume, or edit the
 * expiry). It also warns when an older grace is about to be dropped by a second rotation.
 */
export function rotationConfirmation(key: Key, graceSeconds: number, now = new Date()) {
  const parts: string[] = [];
  if (suspended(key)) {
    parts.push("This key is suspended: rotating does not resume it. Use Resume to make a new secret usable.");
  } else if (isExpired(key)) {
    parts.push("This key has expired: rotating does not extend its expiry. Edit the expiry to make a new secret usable.");
  }
  if (inGrace(key)) {
    parts.push(
      `This key already has a previous secret in grace until ${fmtDateTime(key.previous_key_expires_at!)}. ` +
        "Rotating now ends that grace immediately: the older secret stops working right away.",
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

/** The success dialog: the new secret's real state, not just its grace window. */
export function rotationNote(key: Key) {
  if (suspended(key)) {
    const kept =
      key.previous_key_expires_at && new Date(key.previous_key_expires_at) > new Date()
        ? ` The previous secret is kept until ${fmtDateTime(key.previous_key_expires_at)}.`
        : "";
    return `The new secret will not work until this key is resumed. Rotating does not resume it; use Resume.${kept}`;
  }
  if (isExpired(key)) {
    return "The new secret is still expired. Rotating does not extend the expiry; edit it to make the new secret usable.";
  }
  return inGrace(key)
    ? `The previous secret works until ${fmtDateTime(key.previous_key_expires_at!)}.`
    : "The previous secret stopped working immediately.";
}

/** The row note. Never claims a secret works for a suspended or expired key. */
export function rotationRowNote(key: Key) {
  if (inGrace(key)) return graceText(key);
  if (suspended(key) && key.previous_key_expires_at && new Date(key.previous_key_expires_at) > new Date()) {
    return `Previous secret kept until ${fmtDateTime(key.previous_key_expires_at)}; the key is suspended.`;
  }
  return null;
}
