/**
 * A line-by-line diff for display, hand-written: no dependency, no markup, plain text out.
 *
 * Both sides are untrusted upstream data (an MCP tool's canonical JSON), so nothing here parses more than
 * lines and nothing is ever interpreted. The longest common subsequence is computed with a size guard: past
 * it the caller is told so instead of the page hanging on a hostile document.
 */

export type DiffLine = { kind: "same" | "added" | "removed"; text: string };
/** A run of unchanged lines left out of a condensed diff. */
export type DiffRow = DiffLine | { kind: "gap"; count: number };

/** The limit on `lines(a) * lines(b)` (4 million cells, about 16 MB of table). */
const MAX_CELLS = 4_000_000;

/** Pretty-printed JSON, or the text unchanged when it is not JSON. */
export function prettyJson(text: string): string {
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

export const linesOf = (text: string): string[] => text.replace(/\r\n?/g, "\n").split("\n");

/** `before` → `after`, line by line; null when the pair is too large to diff here. */
export function lineDiff(before: string, after: string): DiffLine[] | null {
  const a = linesOf(before);
  const b = linesOf(after);
  if (a.length * b.length > MAX_CELLS) return null;
  const width = b.length + 1;
  const table = new Uint32Array((a.length + 1) * width);
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      table[i * width + j] =
        a[i] === b[j] ? table[(i + 1) * width + j + 1] + 1 : Math.max(table[(i + 1) * width + j], table[i * width + j + 1]);
    }
  }
  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      out.push({ kind: "same", text: a[i] });
      i++;
      j++;
    } else if (table[(i + 1) * width + j] >= table[i * width + j + 1]) {
      out.push({ kind: "removed", text: a[i++] });
    } else {
      out.push({ kind: "added", text: b[j++] });
    }
  }
  while (i < a.length) out.push({ kind: "removed", text: a[i++] });
  while (j < b.length) out.push({ kind: "added", text: b[j++] });
  return out;
}

/**
 * The diff with long unchanged runs folded into one `gap` row, keeping `context` lines either side of a
 * change (3 lines of unchanged JSON around a change is enough to see which field moved).
 */
export function condense(diff: DiffLine[], context = 3): DiffRow[] {
  const keep = new Array<boolean>(diff.length).fill(false);
  diff.forEach((line, index) => {
    if (line.kind === "same") return;
    for (let i = Math.max(0, index - context); i <= Math.min(diff.length - 1, index + context); i++) keep[i] = true;
  });
  const out: DiffRow[] = [];
  let skipped = 0;
  diff.forEach((line, index) => {
    if (keep[index]) {
      if (skipped) {
        out.push({ kind: "gap", count: skipped });
        skipped = 0;
      }
      out.push(line);
    } else {
      skipped++;
    }
  });
  if (skipped) out.push({ kind: "gap", count: skipped });
  return out;
}

/** How many lines a diff adds and removes. */
export function diffCounts(diff: DiffLine[]): { added: number; removed: number } {
  return {
    added: diff.filter((line) => line.kind === "added").length,
    removed: diff.filter((line) => line.kind === "removed").length,
  };
}
