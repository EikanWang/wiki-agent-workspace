import { useEffect, useRef } from "react";
import { WorkspaceInfo } from "../api";
import { FileIcon } from "./FileIcon";
import { PullRequestIcon } from "./Icons";

/** An open document: which root folder and which path inside it. */
export interface DocTab {
  ws: string;
  path: string;
  /** pull-request tabs: "#123 Title" (shown instead of the file name) */
  title?: string;
}

/** `ws` of pull-request tabs (path "owner/repo#123"); they are not tied to a folder. */
export const PR_TABS = "github";

export const sameTab = (a: DocTab | null | undefined, b: DocTab | null | undefined) => !!a && !!b && a.ws === b.ws && a.path === b.path;

const KIND_BY_EXT: Record<string, string> = {
  md: "markdown", markdown: "markdown", mdx: "markdown", pdf: "pdf", docx: "word", doc: "word", odt: "word", rtf: "word",
  pptx: "slides", ppt: "slides", pps: "slides", ppsx: "slides", odp: "slides", ipynb: "notebook",
  vsdx: "diagram", vsd: "diagram", vsdm: "diagram", odg: "diagram",
  png: "image", jpg: "image", jpeg: "image", gif: "image", svg: "image", webp: "image",
  py: "code", ts: "code", tsx: "code", js: "code", c: "code", cc: "code", cpp: "code", h: "code", hpp: "code", rs: "code", go: "code", java: "code",
  sh: "shell", bash: "shell", json: "data", yaml: "data", yml: "data", toml: "data", csv: "data",
};
const fileName = (p: string) => p.split("/").pop() || p;
const parentName = (p: string) => {
  const parts = p.split("/");
  return parts.length > 1 ? parts[parts.length - 2] : "";
};

/**
 * Add `tab` (or keep it where it is). A new tab goes right after `after` (the tab you were reading), like a
 * browser. `renamedFrom` replaces an existing tab in place (the document was relinked to another path).
 */
export function withTab(tabs: DocTab[], tab: DocTab, after: DocTab | null, renamedFrom: DocTab | null = null): DocTab[] {
  if (renamedFrom && !sameTab(renamedFrom, tab)) {
    const i = tabs.findIndex((t) => sameTab(t, renamedFrom));
    if (i >= 0) {
      const out = tabs.filter((t) => !sameTab(t, tab));
      out.splice(out.findIndex((t) => sameTab(t, renamedFrom)), 1, tab);
      return out;
    }
  }
  if (tabs.some((t) => sameTab(t, tab))) return tabs;
  const i = after ? tabs.findIndex((t) => sameTab(t, after)) : -1;
  const out = [...tabs];
  out.splice(i >= 0 ? i + 1 : out.length, 0, tab);
  return out;
}

/** Key of the open tabs of a named workspace (each workspace has its own tabs, like a VS Code window). */
export const tabsKey = (named: string) => `waw.docTabs.${named}`;

/** Open tabs of named workspace `named`; `legacy`: fall back to the tabs saved before named workspaces existed. */
export function loadTabs(named: string = localStorage.getItem("waw.namedWorkspace") ?? "", legacy = true): DocTab[] {
  try {
    const raw = localStorage.getItem(tabsKey(named)) ?? (legacy ? localStorage.getItem("waw.docTabs") : null);
    const v = JSON.parse(raw || "[]");
    return Array.isArray(v) ? v.filter((t) => t && typeof t.ws === "string" && typeof t.path === "string") : [];
  } catch {
    return [];
  }
}

/** Tab strip of open documents, above the reader (like the terminal tabs). */
export default function DocTabs({
  tabs,
  active,
  workspaces,
  onActivate,
  onClose,
  onReorder,
}: {
  tabs: DocTab[];
  active: DocTab | null;
  workspaces: WorkspaceInfo[];
  onActivate: (t: DocTab) => void;
  onClose: (t: DocTab) => void;
  /** move `moved` to where `target` is */
  onReorder: (moved: DocTab, target: DocTab) => void;
}) {
  const strip = useRef<HTMLDivElement>(null);
  const dragFrom = useRef<number | null>(null);
  const multi = workspaces.filter((w) => w.available).length > 1;
  const wsName = (id: string) => workspaces.find((w) => w.id === id)?.name ?? "";

  // keep the active tab in view when the strip overflows
  useEffect(() => {
    strip.current?.querySelector<HTMLElement>(".doc-tab.active")?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [active?.ws, active?.path, tabs.length]);

  if (!tabs.length) return null;
  // same file name twice: show the parent folder to tell them apart; same path in two roots: the root's name
  const names = new Map<string, number>();
  tabs.forEach((t) => names.set(fileName(t.path), (names.get(fileName(t.path)) ?? 0) + 1));
  const paths = new Map<string, number>();
  tabs.forEach((t) => paths.set(t.path, (paths.get(t.path) ?? 0) + 1));

  return (
    <div className="doc-tabs" role="tablist" aria-label="Open documents" ref={strip} data-testid="doc-tabs">
      {tabs.map((t, i) => {
        const isActive = sameTab(t, active);
        const isPr = t.ws === PR_TABS;
        const isChat = !isPr && t.path.startsWith(".waw-chat/");
        const name = isPr ? t.title ?? `#${t.path.split("#")[1]}` : isChat ? `Chat: ${t.title ?? fileName(t.path).replace(/-[0-9a-f]{6}\.md$/, "")}` : fileName(t.path);
        const hint = isPr ? t.path.split("#")[0] : (paths.get(t.path) ?? 0) > 1 ? wsName(t.ws) : (names.get(name) ?? 0) > 1 ? parentName(t.path) || wsName(t.ws) : "";
        const ext = name.includes(".") ? name.slice(name.lastIndexOf(".") + 1).toLowerCase() : "";
        const kind = KIND_BY_EXT[ext] ?? "text";
        return (
          <div
            key={`${t.ws}\u0000${t.path}`}
            className={`doc-tab ${isActive ? "active" : ""}`}
            role="tab"
            aria-selected={isActive}
            tabIndex={0}
            title={`${isPr ? `Pull request ${t.path}${t.title ? `\n${t.title}` : ""}` : `${multi ? `${wsName(t.ws)} › ` : ""}${t.path}`}\nMiddle-click or × to close · drag to reorder`}
            data-testid="doc-tab"
            data-path={t.path}
            data-ws={t.ws}
            draggable
            onClick={() => !isActive && onActivate(t)}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                if (!isActive) onActivate(t);
              }
            }}
            onAuxClick={(e) => {
              if (e.button === 1) {
                e.preventDefault();
                onClose(t);
              }
            }}
            onMouseDown={(e) => {
              if (e.button === 1) e.preventDefault(); // no autoscroll cursor on middle-click
            }}
            onDragStart={(e) => {
              dragFrom.current = i;
              e.dataTransfer.effectAllowed = "move";
            }}
            onDragOver={(e) => {
              if (dragFrom.current !== null) e.preventDefault();
            }}
            onDrop={(e) => {
              e.preventDefault();
              if (dragFrom.current !== null && dragFrom.current !== i) onReorder(tabs[dragFrom.current], t);
              dragFrom.current = null;
            }}
            onDragEnd={() => {
              dragFrom.current = null;
            }}
          >
            {isPr ? <PullRequestIcon size={14} /> : <FileIcon item={{ type: "file", kind }} />}
            <span className="doc-tab-name">{name}</span>
            {hint && <span className="doc-tab-hint">{hint}</span>}
            <button
              className="tab-x"
              onClick={(e) => {
                e.stopPropagation();
                onClose(t);
              }}
              title="Close"
              aria-label={`Close ${name}`}
              data-testid="doc-tab-close"
              tabIndex={-1}
            >
              ×
            </button>
          </div>
        );
      })}
    </div>
  );
}
