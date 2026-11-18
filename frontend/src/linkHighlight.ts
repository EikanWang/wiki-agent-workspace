/** Where a link led: a rectangle drawn around it for a moment (color and time: Appearance settings). */

/** How long the rectangle stays, from the `--link-hl-ms` the app sets (0: no rectangle). */
export function linkHighlightMs(el: Element): number {
  const v = Number.parseInt(getComputedStyle(el).getPropertyValue("--link-hl-ms"), 10);
  return Number.isFinite(v) && v >= 0 ? v : 1000;
}

/** A heading and its section: the siblings that follow it up to the next heading of the same or a higher level. */
export function sectionOf(heading: HTMLElement): HTMLElement[] {
  const level = /^H([1-6])$/.exec(heading.tagName)?.[1];
  if (!level) return [heading];
  const out = [heading];
  for (let n = heading.nextElementSibling as HTMLElement | null; n; n = n.nextElementSibling as HTMLElement | null) {
    const l = /^H([1-6])$/.exec(n.tagName)?.[1];
    if (l && Number(l) <= Number(level)) break;
    out.push(n);
  }
  return out;
}

/** Draw the rectangle around `els` inside `scroller` (positioned, scrolling); at most a screen high, so it is seen whole. */
export function flashRect(scroller: HTMLElement, els: HTMLElement[]) {
  const ms = linkHighlightMs(scroller);
  if (!ms || !els.length) return;
  const s = scroller.getBoundingClientRect();
  let top = Infinity, left = Infinity, right = -Infinity, bottom = -Infinity;
  for (const e of els) {
    const r = e.getBoundingClientRect();
    if (!r.width && !r.height) continue;
    top = Math.min(top, r.top); left = Math.min(left, r.left); right = Math.max(right, r.right); bottom = Math.max(bottom, r.bottom);
  }
  if (top === Infinity) return;
  const pad = 4;
  const height = Math.min(bottom - top, scroller.clientHeight - 32) + 2 * pad;
  scroller.querySelectorAll(":scope > .waw-link-rect").forEach((n) => n.remove());
  const box = document.createElement("div");
  box.className = "waw-link-rect";
  box.dataset.testid = "link-highlight";
  Object.assign(box.style, {
    top: `${top - s.top + scroller.scrollTop - pad}px`,
    left: `${left - s.left + scroller.scrollLeft - pad}px`,
    width: `${right - left + 2 * pad}px`,
    height: `${height}px`,
    animationDuration: `${ms}ms`,
  });
  scroller.appendChild(box);
  window.setTimeout(() => box.remove(), ms);
}
