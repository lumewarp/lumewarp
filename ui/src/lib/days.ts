/** UTC-day helpers. Usage is bucketed by UTC day, so every range and label here is UTC too. */
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
