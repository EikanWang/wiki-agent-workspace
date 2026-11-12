/** Times of questions and answers: always shown in UTC+8 (China Standard Time; no daylight saving), e.g.
 *  "2026-11-12 15:03:21 UTC+8". */
export function utc8(iso: string | number | Date | null | undefined, seconds = true): string {
  if (iso === null || iso === undefined || iso === "") return "";
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return "";
  const d = new Date(t.getTime() + 8 * 3600_000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}${seconds ? `:${p(d.getUTCSeconds())}` : ""} UTC+8`;
}
