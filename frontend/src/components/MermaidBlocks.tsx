import { useEffect } from "react";

/** ```mermaid blocks in rendered Markdown anywhere in the app (agent answers, documents, PR text …) are drawn as
 * diagrams: sequence, state, flowchart, class … The drawing is shown as an <img> (so it opens in the picture dialog
 * and no script in it runs), with its source folded below. A block that doesn't parse (or is still streaming) stays code. */
const drawn = new Map<string, string | null>(); // source → SVG (null: doesn't parse)
let mermaidMod: Promise<typeof import("mermaid").default> | null = null;
let seq = 0;

function load() {
  mermaidMod ??= import("mermaid").then((m) => {
    m.default.initialize({ startOnLoad: false, securityLevel: "strict", theme: "default", flowchart: { htmlLabels: false } });
    return m.default;
  });
  return mermaidMod;
}

async function draw(src: string): Promise<string | null> {
  if (drawn.has(src)) return drawn.get(src)!;
  const mermaid = await load();
  let svg: string | null = null;
  try {
    if (await mermaid.parse(src, { suppressErrors: true })) svg = (await mermaid.render(`waw-mermaid-${++seq}`, src)).svg;
  } catch {
    svg = null;
  }
  document.getElementById(`dwaw-mermaid-${seq}`)?.remove(); // mermaid's scratch element after a failure
  drawn.set(src, svg);
  return svg;
}

function upgrade(code: HTMLElement) {
  const pre = code.parentElement;
  if (!pre || pre.dataset.mermaid) return;
  const src = (code.textContent || "").trim();
  if (!src) return;
  pre.dataset.mermaid = "1";
  void draw(src).then((svg) => {
    if (!svg || !pre.isConnected) return;
    const box = document.createElement("div");
    box.className = "mermaid-diagram";
    box.setAttribute("data-testid", "mermaid-diagram");
    const img = document.createElement("img");
    img.alt = "Diagram";
    img.className = "msg-img";
    img.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
    const det = document.createElement("details");
    const sum = document.createElement("summary");
    sum.textContent = "Diagram source";
    pre.replaceWith(box);
    det.append(sum, pre);
    box.append(img, det);
  });
}

export default function MermaidBlocks() {
  useEffect(() => {
    const scan = (root: ParentNode) => root.querySelectorAll<HTMLElement>(".markdown-body pre > code.language-mermaid").forEach(upgrade);
    scan(document);
    let queued = false;
    const obs = new MutationObserver(() => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => { queued = false; scan(document); });
    });
    obs.observe(document.body, { childList: true, subtree: true });
    return () => obs.disconnect();
  }, []);
  return null;
}
