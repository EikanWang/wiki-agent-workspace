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
    const ws = getWorkspace();
    img.setAttribute("src", `/api/v1/raw?path=${encodeURIComponent(resolveRelative(dir, src))}${ws ? `&workspace=${encodeURIComponent(ws)}` : ""}`);
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
