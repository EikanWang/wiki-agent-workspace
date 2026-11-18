/** Keeping the place when an Office document switches between its Layout (PDF) and Text views: the text at the top
 * of one view is looked up in the other (compared on letters and digits only, as both render it differently). */

/** Where a view is: the text from its top on, and how far down it is (0–1, the fallback). */
export interface ViewPos {
  text: string;
  frac: number;
}

const SKIP = /[^\p{L}\p{N}]/u;

/** Letters and digits only, lowercased. */
export function squash(s: string): string {
  return s.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, "");
}

/** `squash` with the way back: `at[i]` is the index in `s` of the i-th kept character. */
export function squashMap(s: string): { text: string; at: number[] } {
  let text = "";
  const at: number[] = [];
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (SKIP.test(c)) continue;
    const l = c.toLowerCase();
    for (let k = 0; k < l.length; k++) at.push(i);
    text += l;
  }
  return { text, at };
}

/** Where (squashed) `needle` starts in `hay`: the longest piece of it found (from its start, or a little later
 * when its first words are not in this view, e.g. a page header), the occurrence nearest `near`; -1 if none. */
export function findPart(hay: string, needle: string, near = 0): number {
  for (const skip of [0, 24, 60, 120]) {
    if (skip && skip + 12 > needle.length) break;
    let tried = "";
    for (const len of [80, 48, 28, 14]) {
      const part = needle.slice(skip, skip + len);
      if (part.length < (skip ? 12 : Math.min(12, Math.max(4, needle.length))) || part === tried) continue;
      tried = part;
      let best = -1;
      for (let i = hay.indexOf(part), n = 0; i >= 0 && n < 200; i = hay.indexOf(part, i + 1), n++) {
        if (best < 0 || Math.abs(i - near) < Math.abs(best - near)) best = i;
      }
      if (best >= 0) return Math.max(0, best - skip);
    }
  }
  return -1;
}
