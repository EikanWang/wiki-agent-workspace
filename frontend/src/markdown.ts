// Markdown rendering (markdown-it, GFM-like, no raw HTML) + DOMPurify sanitization.
// Keep options aligned with backend/app/textmodel.py so both text models agree.
import MarkdownIt from "markdown-it";
import DOMPurify from "dompurify";

/** GitHub-style heading slug (github-slugger): lowercase, drop punctuation, each space -> "-". */
export function slugify(text: string): string {
  let s = text.trim().toLowerCase();
  s = s.replace(/[^\p{L}\p{N}\p{M}\s_-]/gu, "");
  s = s.replace(/ /g, "-");
  return s || "section";
}

const md: MarkdownIt = new MarkdownIt("commonmark", { html: false, linkify: false, typographer: false });
md.enable(["table", "strikethrough"]);

md.renderer.rules.heading_open = (tokens, idx, options, env, self) => {
  const inline = tokens[idx + 1];
  const text = (inline.children || []).filter((t) => t.type === "text" || t.type === "code_inline").map((t) => t.content).join("");
  let slug = slugify(text);
  const counts: Record<string, number> = (env.slugs = env.slugs || {});
  const n = counts[slug] || 0;
  counts[slug] = n + 1;
  if (n) slug = `${slug}-${n}`;
  tokens[idx].attrSet("id", slug);
  return self.renderToken(tokens, idx, options);
};

// Relative links stay inside the workspace; absolute http(s)/mailto links open externally.
const SAFE_PROTOCOLS = /^(https?:|mailto:|#|\/|\.{0,2}\/|[^:]*$)/i;

export function renderMarkdown(src: string, docPath: string): string {
  const html = md.render(src, {});
  const clean = DOMPurify.sanitize(html, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ["style", "script", "iframe", "object", "embed", "form", "input", "button", "svg", "math"],
    FORBID_ATTR: ["style", "onerror", "onload"],
    ALLOW_DATA_ATTR: false,
  });
  const tpl = document.createElement("template");
  tpl.innerHTML = clean;
  const dir = docPath.includes("/") ? docPath.slice(0, docPath.lastIndexOf("/") + 1) : "";
  tpl.content.querySelectorAll("a[href]").forEach((a) => {
    const href = a.getAttribute("href") || "";
    if (!SAFE_PROTOCOLS.test(href)) {
      a.removeAttribute("href");
      return;
    }
    if (/^https?:/i.test(href) || /^mailto:/i.test(href)) {
      a.setAttribute("target", "_blank");
      a.setAttribute("rel", "noopener noreferrer");
      a.setAttribute("data-external", "1");
    } else if (href.startsWith("#")) {
      a.setAttribute("data-anchor", safeDecode(href.slice(1)));
    } else {
      const [pathPart, frag] = href.split("#");
      a.setAttribute("data-internal", resolveRelative(dir, pathPart));
      if (frag) a.setAttribute("data-anchor", safeDecode(frag));
    }
  });
  tpl.content.querySelectorAll("img[src]").forEach((img) => {
    const src = img.getAttribute("src") || "";
    if (/^https?:/i.test(src) || src.startsWith("data:image/")) return;
    if (/^[a-z]+:/i.test(src)) {
      img.removeAttribute("src");
      return;
    }
    img.setAttribute("src", `/api/v1/raw?path=${encodeURIComponent(resolveRelative(dir, src))}`);
    img.setAttribute("loading", "lazy");
  });
  return tpl.innerHTML;
}

function safeDecode(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}

/** Sanitize server-rendered HTML (text/source as code block, .docx converted by mammoth) with the same rules. */
export function sanitizeHtml(html: string, docPath: string): string {
  const clean = DOMPurify.sanitize(html, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ["style", "script", "iframe", "object", "embed", "form", "input", "button", "svg", "math"],
    FORBID_ATTR: ["style", "onerror", "onload"],
    ALLOW_DATA_ATTR: false,
  });
  const tpl = document.createElement("template");
  tpl.innerHTML = clean;
  tpl.content.querySelectorAll("a[href]").forEach((a) => {
    const href = a.getAttribute("href") || "";
    if (!SAFE_PROTOCOLS.test(href)) a.removeAttribute("href");
    else if (/^https?:/i.test(href) || /^mailto:/i.test(href)) {
      a.setAttribute("target", "_blank");
      a.setAttribute("rel", "noopener noreferrer");
    }
  });
  tpl.content.querySelectorAll("img[src]").forEach((img) => {
    const src = img.getAttribute("src") || "";
    if (!(src.startsWith("data:image/") || /^https?:/i.test(src))) img.removeAttribute("src"); // docx images arrive as data URIs
  });
  void docPath;
  return tpl.innerHTML;
}

export function resolveRelative(dir: string, href: string): string {
  const [pathPart] = href.split("#");
  const parts = (dir + safeDecode(pathPart)).split("/");
  const out: string[] = [];
  for (const p of parts) {
    if (p === "" || p === ".") continue;
    if (p === "..") out.pop();
    else out.push(p);
  }
  return out.join("/");
}
