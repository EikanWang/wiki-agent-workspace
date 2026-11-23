// Markdown rendering (markdown-it, GFM-like, no raw HTML) + DOMPurify sanitization.
// Keep options aligned with backend/app/textmodel.py so both text models agree.
import MarkdownIt from "markdown-it";
import DOMPurify from "dompurify";
import { getWorkspace } from "./api";

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

// GitHub text (PR descriptions, reviews, comments): GitHub renders raw HTML (<details>, <img>, <!-- … -->) and
// links bare URLs. Allowed here and sanitized like everything else; wiki pages keep the stricter `md` above.
const ghMd: MarkdownIt = new MarkdownIt("default", { html: true, linkify: true, typographer: false, breaks: true });

/** Markdown from GitHub, rendered the way GitHub shows it, then sanitized. */
export function renderGithubMarkdown(src: string, repo?: string): string {
  const html = sanitizeHtml(ghMd.render(src || ""), "");
  return linkGithubRefs(html, repo);
}

const PR_URL = /^https:\/\/github\.com\/([\w.-]+)\/([\w.-]+)\/(?:pull|issues)\/(\d+)(?:\/(?:files|commits)?)?\/?(?:[?#].*)?$/;
const MENTION = /(^|[^\w`/@.-])@([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))(?![\w/-])/g;
const REF = /(^|[^\w/#&.-])((?:([\w.-]+)\/([\w.-]+))?#(\d+))(?![\w-])/g;

/**
 * As on GitHub: `#123` and `owner/repo#123` become links, and links to pull requests are marked (`data-pr`) so the
 * app can open them as PR tabs. `#N` may be an issue: GitHub redirects /issues/N to the PR when it is one.
 */
export function linkGithubRefs(html: string, repo?: string): string {
  const tpl = document.createElement("template");
  tpl.innerHTML = html;
  tpl.content.querySelectorAll("a[href]").forEach((a) => {
    const m = (a.getAttribute("href") || "").match(PR_URL);
    if (m) a.setAttribute("data-pr", `${m[1]}/${m[2]}/${m[3]}`);
  });
  const walker = document.createTreeWalker(tpl.content, NodeFilter.SHOW_TEXT);
  const texts: Text[] = [];
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const t = n as Text;
    if (!t.parentElement?.closest("a, code, pre") && /#\d|@\w/.test(t.data)) texts.push(t);
  }
  for (const t of texts) {
    // #123 / owner/repo#123 → pull request links; @login → the account's GitHub page (as GitHub renders them)
    const hits: { start: number; end: number; node: HTMLAnchorElement }[] = [];
    for (const m of t.data.matchAll(REF)) {
      const target = m[3] ? `${m[3]}/${m[4]}` : repo;
      if (!target) continue;
      const start = (m.index ?? 0) + m[1].length;
      const a = document.createElement("a");
      a.href = `https://github.com/${target}/issues/${m[5]}`;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.textContent = m[2];
      a.setAttribute("data-pr", `${target}/${m[5]}`);
      a.setAttribute("data-external", "1");
      hits.push({ start, end: start + m[2].length, node: a });
    }
    for (const m of t.data.matchAll(MENTION)) {
      const start = (m.index ?? 0) + m[1].length;
      if (hits.some((h) => start < h.end && start + m[2].length + 1 > h.start)) continue;
      const a = document.createElement("a");
      a.href = `https://github.com/${m[2]}`;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.className = "gh-user";
      a.textContent = `@${m[2]}`;
      a.setAttribute("data-external", "1");
      a.setAttribute("data-login", m[2]);
      hits.push({ start, end: start + m[2].length + 1, node: a });
    }
    if (!hits.length) continue;
    hits.sort((x, y) => x.start - y.start);
    const frag = document.createDocumentFragment();
    let last = 0;
    for (const h of hits) {
      if (h.start < last) continue;
      frag.append(t.data.slice(last, h.start), h.node);
      last = h.end;
    }
    frag.append(t.data.slice(last));
    t.replaceWith(frag);
  }
  return tpl.innerHTML;
}

/** A file of the server by its absolute path (images are served as images, SVG sandboxed). */
export const localRawUrl = (abs: string) => `/api/v1/local/raw?path=${encodeURIComponent(abs)}`;

const IMAGE_FILE = /\.(png|jpe?g|gif|webp|svg|bmp|ico|avif)$/i;

/** Is this link one to an image file (an absolute path on the server, or a web URL)? Its address to show it, else null. */
function imageSrcOf(href: string): string | null {
  const [p] = href.split(/[?#]/);
  if (!IMAGE_FILE.test(p)) return null;
  if (/^https?:\/\//i.test(href)) return href;
  if (p.startsWith("/") && !p.startsWith("//")) return localRawUrl(safeDecode(p));
  if (/^[a-z][a-z0-9+.-]*:/i.test(p) || p.startsWith("//")) return null;
  // a path inside the wiki
  const ws = getWorkspace();
  return `/api/v1/raw?path=${encodeURIComponent(resolveRelative("", p))}${ws ? `&workspace=${encodeURIComponent(ws)}` : ""}`;
}

/**
 * Agent answers show their images: links to image files (`[plot](/tmp/plot.png)`, or a path in `code` turned into a
 * link) get the picture under them, and a ```svg block is drawn (its source folded below). Each picture opens full
 * size in a new tab. `<img>` never runs an SVG's scripts, and the server sandboxes SVG files it serves.
 */
export function showImages(html: string): string {
  const tpl = document.createElement("template");
  tpl.innerHTML = html;
  const shown = new Set<string>();
  tpl.content.querySelectorAll("img[src]").forEach((img) => shown.add(img.getAttribute("src") || ""));
  const picture = (src: string, alt: string) => {
    const a = document.createElement("a");
    a.href = src;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.className = "msg-image";
    a.setAttribute("data-image", "1");
    a.title = `${alt} (open full size)`;
    const img = document.createElement("img");
    img.src = src;
    img.alt = alt;
    img.loading = "lazy";
    a.append(img);
    return a;
  };
  tpl.content.querySelectorAll("img[src]").forEach((img) => {
    if (img.closest("a")) return;
    const pic = picture(img.getAttribute("src") || "", img.getAttribute("alt") || "image");
    img.replaceWith(pic);
  });
  tpl.content.querySelectorAll("a[href]").forEach((a) => {
    if (a.closest("pre") || a.hasAttribute("data-image")) return;
    const src = imageSrcOf(a.getAttribute("href") || "");
    if (!src || shown.has(src)) return;
    shown.add(src);
    const label = (a.textContent || "").trim() || src;
    // a picture can't sit inside a heading or a table cell's line nicely: put it after the link's block-level parent line
    const pic = picture(src, label);
    pic.classList.add("msg-image-auto"); // hidden if it doesn't load (the link may not be to a file this side has)
    const block = a.closest("p, li, td, th, h1, h2, h3, h4, h5, h6, blockquote");
    if (block && !/^(TD|TH|LI)$/.test(block.tagName)) block.after(pic);
    else (block ?? a).append(pic);
  });
  tpl.content.querySelectorAll("pre > code.language-svg").forEach((code) => {
    const text = (code.textContent || "").trim();
    if (!/^(<\?xml[^>]*>\s*)?<svg[\s>]/i.test(text) || !/<\/svg>\s*$/i.test(text)) return; // still streaming, or not SVG
    const pre = code.parentElement!;
    const pic = picture(`data:image/svg+xml;charset=utf-8,${encodeURIComponent(text)}`, "SVG image");
    const src = document.createElement("details");
    const sum = document.createElement("summary");
    sum.textContent = "SVG source";
    pre.replaceWith(pic);
    src.append(sum, pre);
    pic.after(src);
  });
  tpl.content.querySelectorAll("img").forEach((img) => img.classList.add("msg-img"));
  return tpl.innerHTML;
}

export function renderMarkdown(src: string, docPath: string, opts: { imageUrl?: (rel: string) => string; absoluteImageUrl?: (abs: string) => string } = {}): string {
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
    if (opts.absoluteImageUrl && src.startsWith("/") && !src.startsWith("//")) {
      img.setAttribute("src", opts.absoluteImageUrl(safeDecode(src.split(/[?#]/)[0])));
      img.setAttribute("loading", "lazy");
      return;
    }
    if (opts.imageUrl) {
      img.setAttribute("src", opts.imageUrl(resolveRelative(dir, src)));
      img.setAttribute("loading", "lazy");
      return;
    }
    const ws = getWorkspace();
    img.setAttribute("src", `/api/v1/raw?path=${encodeURIComponent(resolveRelative(dir, src))}${ws ? `&workspace=${encodeURIComponent(ws)}` : ""}`);
    img.setAttribute("loading", "lazy");
  });
  markNew(tpl.content);
  return tpl.innerHTML;
}

export const NEW_MARK = "🆕";

/** A new thing's name as the server keeps its read marks (spaces collapsed, lower case). */
export function newKey(name: string): string {
  return name.replace(/`/g, "").split(/\s+/).join(" ").replace(/^[ :]+|[ :]+$/g, "").toLowerCase();
}

/**
 * What the text marks as new ("🆕", as the Repo Insights summaries and reports do) is highlighted: the mark becomes a
 * "NEW" badge, its bullet or paragraph is tinted and the bold name after it stands out; a heading with the mark
 * ("🆕 What's new") and its section, up to the next heading of the same level or higher, become one highlighted box.
 */
export function markNew(root: DocumentFragment | HTMLElement): void {
  markExt(root);
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const texts: Text[] = [];
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const t = n as Text;
    if (t.data.includes(NEW_MARK) && !t.parentElement?.closest("code, pre, .waw-new-badge")) texts.push(t);
  }
  if (!texts.length) return;
  const heads = new Set<HTMLElement>();
  for (const t of texts) {
    const parent = t.parentElement;
    const frag = document.createDocumentFragment();
    t.data.split(NEW_MARK).forEach((part, i) => {
      if (i) {
        const b = document.createElement("span");
        b.className = "waw-new-badge";
        b.title = "New to the project";
        b.textContent = "NEW";
        frag.append(b);
      }
      if (i && part.startsWith(" ")) part = part.slice(1);
      if (part) frag.append(part);
    });
    t.replaceWith(frag);
    const head = parent?.closest("h1, h2, h3, h4, h5, h6") as HTMLElement | null;
    if (head) {
      heads.add(head);
      continue;
    }
    parent?.closest("li, p, td")?.classList.add("waw-new");
  }
  root.querySelectorAll(".waw-new-badge").forEach((b) => {
    const next = b.nextSibling;
    if (next instanceof HTMLElement && /^(STRONG|B)$/.test(next.tagName)) {
      next.classList.add("waw-new-name");
      (b as HTMLElement).dataset.newName = newKey(next.textContent ?? "");
    }
  });
  for (const h of heads) {
    h.classList.add("waw-new-head");
    if (!/^H[2-6]$/.test(h.tagName) || !h.parentNode) continue;
    const level = Number(h.tagName[1]);
    const box = document.createElement("section");
    box.className = "waw-new-section";
    h.parentNode.insertBefore(box, h);
    for (let n: ChildNode | null = h; n; ) {
      const next: ChildNode | null = n.nextSibling;
      if (n !== h && n instanceof HTMLElement && /^H[1-6]$/.test(n.tagName) && Number(n.tagName[1]) <= level) break;
      box.append(n);
      n = next;
    }
  }
}

export const EXT_MARK = "➕";

/** An extension of something known ("➕ **name** *(extends …)*", from the new-things check): an "EXTENDS" badge, not counted as new. */
function markExt(root: DocumentFragment | HTMLElement): void {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const texts: Text[] = [];
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const t = n as Text;
    if (t.data.includes(EXT_MARK) && !t.parentElement?.closest("code, pre, h1, h2, h3, h4, h5, h6")) texts.push(t);
  }
  for (const t of texts) {
    const parent = t.parentElement;
    const frag = document.createDocumentFragment();
    t.data.split(EXT_MARK).forEach((part, i) => {
      if (i) {
        const b = document.createElement("span");
        b.className = "waw-ext-badge";
        b.title = "A new aspect of something seen before (not counted as new)";
        b.textContent = "EXTENDS";
        frag.append(b);
      }
      if (i && part.startsWith(" ")) part = part.slice(1);
      if (part) frag.append(part);
    });
    t.replaceWith(frag);
    const box = parent?.closest("li, p, td");
    if (box && !box.textContent?.includes(NEW_MARK)) box.classList.add("waw-ext");
  }
  root.querySelectorAll(".waw-ext-badge").forEach((b) => {
    const next = b.nextSibling;
    if (next instanceof HTMLElement && /^(STRONG|B)$/.test(next.tagName)) next.classList.add("waw-ext-name");
  });
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
