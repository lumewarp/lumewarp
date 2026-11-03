/** UTC-day helpers. Usage is bucketed by UTC day, so every range and label here is UTC too. */
const DAY_MS = 86_400_000;

export const isoDay = (date: Date) => date.toISOString().slice(0, 10);

export const dayStart = (day: string) => `${day}T00:00:00Z`;

/** The UTC day `count` days after `day` (negative for before). */
export const shiftDay = (day: string, count: number) => isoDay(new Date(Date.parse(dayStart(day)) + count * DAY_MS));

/** Every UTC day from `from` to `to`, both inclusive; empty when `to` is before `from`. */
export function daysBetween(from: string, to: string): string[] {
  const out: string[] = [];
  const end = Date.parse(dayStart(to));
  for (let t = Date.parse(dayStart(from)); t <= end; t += DAY_MS) out.push(isoDay(new Date(t)));
  return out;
}
