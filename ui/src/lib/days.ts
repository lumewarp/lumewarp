/** UTC-day helpers, and local date-time helpers for the range pickers (`datetime-local`, browser zone). */
const DAY_MS = 86_400_000;
/** The API refuses a range longer than 400 days; a day list is capped at the same place. */
const MAX_DAYS = 400;

/** Midnight UTC of an ISO day, or null when the string is empty or not a day. */
const at = (day: string) => {
  const time = Date.parse(`${day}T00:00:00Z`);
  return Number.isNaN(time) ? null : time;
};

export const isoDay = (date: Date) => date.toISOString().slice(0, 10);

/** `day` as the API's `from`/`to`: "" when the field is empty, which leaves the query disabled. */
export const dayStart = (day: string) => (at(day) === null ? "" : `${day}T00:00:00Z`);

/** The UTC day `count` days after `day` (negative for before); "" for an invalid day. */
export const shiftDay = (day: string, count: number) => {
  const time = at(day);
  return time === null ? "" : isoDay(new Date(time + count * DAY_MS));
};

/**
 * Every UTC day from `from` to `to`, both inclusive. Empty when either day is missing, when `to` is
 * before `from`, or when the range is longer than the API allows: a year like 0002 is not 740,000 bars.
 */
export function daysBetween(from: string, to: string): string[] {
  const start = at(from);
  const end = at(to);
  if (start === null || end === null || end < start || end - start >= MAX_DAYS * DAY_MS) return [];
  const days: string[] = [];
  for (let time = start; time <= end; time += DAY_MS) days.push(isoDay(new Date(time)));
  return days;
}

/** The browser's IANA time zone, for labelling local times. */
export const browserTimeZone = () => Intl.DateTimeFormat().resolvedOptions().timeZone;

const pad = (n: number) => String(n).padStart(2, "0");

/** An instant as a `datetime-local` input value (`YYYY-MM-DDTHH:MM`, local time); "" when invalid. */
export function toLocalInput(instant: Date | string): string {
  const date = typeof instant === "string" ? new Date(instant) : instant;
  if (Number.isNaN(date.getTime())) return "";
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** A `datetime-local` value (local time) as an ISO instant; "" when empty or invalid. */
export function fromLocalInput(value: string): string | "" {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toISOString();
}

/** Local midnight of `day` up to the next local midnight (half-open). */
export function localDayBounds(day: string): { from: Date; to: Date } {
  const [year, month, date] = day.split("-").map(Number);
  return { from: new Date(year, month - 1, date), to: new Date(year, month - 1, date + 1) };
}
