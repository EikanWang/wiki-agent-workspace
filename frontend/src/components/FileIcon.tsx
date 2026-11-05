// Small inline SVG icon set for the file tree. Each kind has a distinct shape *and* colour so files are
// recognisable even when long names hide the extension. Keep it dependency-free.
import { FileItem } from "../api";

const COLORS: Record<string, string> = {
  dir: "#e0a800",
  dir_open: "#e0a800",
  markdown: "#339cff",
  image: "#9b59b6",
  pdf: "#e02e2a",
  notebook: "#f37626",
  code: "#00a240",
  shell: "#2f8f6f",
  data: "#8a6d3b",
  text: "#6c757d",
  archive: "#b06f00",
  binary: "#555",
  file: "#8a8f98",
  symlink: "#17a2b8",
  broken_link: "#e02e2a",
};

export const KIND_LABEL: Record<string, string> = {
  dir: "Folder",
  markdown: "Markdown document",
  image: "Image",
  pdf: "PDF",
  notebook: "Jupyter notebook",
  code: "Source code",
  shell: "Script",
  data: "Data / configuration",
  text: "Text",
  archive: "Archive",
  binary: "Binary",
  file: "File",
  broken_link: "Broken symbolic link",
};

function Svg({ children, title }: { children: React.ReactNode; title: string }) {
  return (
    <svg className="fi" viewBox="0 0 16 16" width="16" height="16" aria-label={title} role="img">
      {children}
    </svg>
  );
}

export function FileIcon({ item, open = false }: { item: Pick<FileItem, "type" | "kind" | "symlink" | "escapes">; open?: boolean }) {
  const kind = item.type === "dir" ? "dir" : item.kind;
  const c = COLORS[kind] ?? COLORS.file;
  const title = KIND_LABEL[kind] ?? kind;
  let body: React.ReactNode;
  switch (kind) {
    case "dir":
      body = open ? (
        <path d="M1.5 4.5h4l1.5-1.5h7.5v2h-9l-1.2 7.5H1.5z M3 7.5h12l-1.3 5.5H1.8z" fill={c} />
      ) : (
        <path d="M1.5 3.5h4.5l1.5 1.5h7v8.5h-13z" fill={c} />
      );
      break;
    case "markdown":
      body = (
        <>
          <rect x="1.5" y="3.5" width="13" height="9" rx="1.2" fill="none" stroke={c} strokeWidth="1.3" />
          <path d="M3.5 10.5v-5l2 2.3 2-2.3v5M11 5.5v5m-1.6-1.6 1.6 1.6 1.6-1.6" fill="none" stroke={c} strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
        </>
      );
      break;
    case "image":
      body = (
        <>
          <rect x="1.5" y="2.5" width="13" height="11" rx="1.2" fill="none" stroke={c} strokeWidth="1.3" />
          <circle cx="5.2" cy="6" r="1.3" fill={c} />
          <path d="M2.5 12.5l3.5-4 2.5 3 2-2 3 3z" fill={c} />
        </>
      );
      break;
    case "pdf":
      body = (
        <>
          <path d="M3.5 1.5h6l3 3v10h-9z" fill="none" stroke={c} strokeWidth="1.3" strokeLinejoin="round" />
          <text x="4.2" y="12" fontSize="5.5" fontWeight="700" fill={c} fontFamily="sans-serif">PDF</text>
        </>
      );
      break;
    case "notebook":
      body = (
        <>
          <rect x="2.5" y="1.5" width="11" height="13" rx="1.2" fill="none" stroke={c} strokeWidth="1.3" />
          <path d="M5 5h6M5 8h6M5 11h4" stroke={c} strokeWidth="1.3" strokeLinecap="round" />
        </>
      );
      break;
    case "code":
      body = <path d="M5.5 4.5 2 8l3.5 3.5M10.5 4.5 14 8l-3.5 3.5M9.5 3 6.5 13" fill="none" stroke={c} strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />;
      break;
    case "shell":
      body = (
        <>
          <rect x="1.5" y="2.5" width="13" height="11" rx="1.2" fill="none" stroke={c} strokeWidth="1.3" />
          <path d="M4 6l2.5 2L4 10M8 10h4" fill="none" stroke={c} strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
        </>
      );
      break;
    case "data":
      body = (
        <>
          <ellipse cx="8" cy="4" rx="5.5" ry="2" fill="none" stroke={c} strokeWidth="1.3" />
          <path d="M2.5 4v8c0 1.1 2.5 2 5.5 2s5.5-.9 5.5-2V4M2.5 8c0 1.1 2.5 2 5.5 2s5.5-.9 5.5-2" fill="none" stroke={c} strokeWidth="1.3" />
        </>
      );
      break;
    case "archive":
      body = (
        <>
          <rect x="2.5" y="3.5" width="11" height="10" rx="1" fill="none" stroke={c} strokeWidth="1.3" />
          <path d="M2.5 6h11M8 6v3M7 9h2v2H7z" fill="none" stroke={c} strokeWidth="1.3" />
        </>
      );
      break;
    case "binary":
      body = (
        <>
          <path d="M3.5 1.5h6l3 3v10h-9z" fill="none" stroke={c} strokeWidth="1.3" strokeLinejoin="round" />
          <text x="4" y="12" fontSize="5" fontWeight="700" fill={c} fontFamily="monospace">01</text>
        </>
      );
      break;
    case "broken_link":
      body = (
        <>
          <path d="M6 10 4 12a2.2 2.2 0 0 1-3-3l2-2M10 6l2-2a2.2 2.2 0 0 1 3 3l-2 2" fill="none" stroke={c} strokeWidth="1.4" strokeLinecap="round" />
          <path d="M6.5 6.5l3 3" stroke={c} strokeWidth="1.4" strokeLinecap="round" />
          <path d="M11 11l3 3M14 11l-3 3" stroke={c} strokeWidth="1.3" strokeLinecap="round" />
        </>
      );
      break;
    case "text":
    default:
      body = (
        <>
          <path d="M3.5 1.5h6l3 3v10h-9z" fill="none" stroke={c} strokeWidth="1.3" strokeLinejoin="round" />
          <path d="M5.5 7h5M5.5 9.5h5M5.5 12h3" stroke={c} strokeWidth="1.1" strokeLinecap="round" />
        </>
      );
  }
  return (
    <span className="fi-wrap" title={title}>
      <Svg title={title}>{body}</Svg>
      {item.symlink && kind !== "broken_link" && (
        <svg className="fi-badge" viewBox="0 0 10 10" width="9" height="9" aria-label="Symbolic link" role="img">
          <circle cx="5" cy="5" r="4.6" fill="var(--bg)" />
          <path d="M3 7l4-4M4 3h3v3" fill="none" stroke={COLORS.symlink} strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      )}
    </span>
  );
}

export function formatSize(n: number | null | undefined): string {
  if (n === null || n === undefined) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10240 ? 1 : 0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}
