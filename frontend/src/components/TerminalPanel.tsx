import { useCallback, useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { ApiError, Bootstrap, TerminalInfo, del, get, getCsrf, patch, post } from "../api";
import { decodeBase64Utf8, writeClipboard } from "../clipboard";

export type ConnState = "connecting" | "connected" | "detached" | "exited" | "error" | "closed";

/** Live terminal views, so the toolbar can send keys the browser would otherwise swallow (Ctrl+T, Ctrl+W, …). */
interface TermHandle {
  send: (bytes: Uint8Array) => void;
  focus: () => void;
  appCursor: () => boolean;
  selection: () => string;
}

/** Where copied terminal text came from: the browser-side xterm selection, or tmux / a program via OSC 52. */
export type CopySource = "selection" | "tmux";

const isMac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);
const handles = new Map<string, TermHandle>();

/** One-shot modifiers armed from the toolbar (for keys/combos you cannot type, and for touch keyboards). */
export interface StickyMods {
  ctrl: boolean;
  alt: boolean;
}

/** Bytes a terminal expects for Ctrl+<key> (Ctrl+letter = 0x01…0x1a, Ctrl+@/[/\\/]/^/_ and Ctrl+Space too). */
export function ctrlByte(ch: string): number | null {
  if (ch === " " || ch === "@" || ch === "2") return 0x00;
  if (ch === "?") return 0x7f;
  const c = ch.toUpperCase().charCodeAt(0);
  if (c >= 0x40 && c <= 0x5f) return c - 0x40; // @A-Z[\]^_
  return null;
}

function applyMods(data: string, mods: StickyMods): string | null {
  // only a single typed character is combined with the armed modifier(s)
  if ([...data].length !== 1) return null;
  let out = data;
  if (mods.ctrl) {
    const b = ctrlByte(data);
    if (b === null) return null;
    out = String.fromCharCode(b);
  }
  if (mods.alt) out = "\x1b" + out;
  return out;
}

export default function TerminalPanel({
  terminals,
  info,
  activeId,
  onActive,
  onChanged,
  visible,
  workspaceRoot,
  fontSize = 13,
}: {
  terminals: TerminalInfo[];
  info: Bootstrap["terminal_info"] | null;
  activeId: string | null;
  onActive: (id: string | null) => void;
  onChanged: () => Promise<void>;
  visible: boolean;
  workspaceRoot: string;
  fontSize?: number;
}) {
  const [error, setError] = useState<string | null>(null);
  const [history, setHistory] = useState<{ id: string; text: string; note: string } | null>(null);
  const [states, setStates] = useState<Record<string, ConnState>>({});
  const [mods, setMods] = useState<StickyMods>({ ctrl: false, alt: false });
  const modsRef = useRef<StickyMods>(mods);
  modsRef.current = mods;
  const [showKeys, setShowKeys] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);
  const [fullscreen, setFullscreen] = useState(false);
  const [kbLocked, setKbLocked] = useState(false);
  const kbLockSupported = typeof navigator !== "undefined" && !!(navigator as any).keyboard?.lock;
  // copy feedback: a short "Copied" toast, or (when the browser refused the clipboard write) a one-click retry
  const [copyNote, setCopyNote] = useState<{ text: string; ok: boolean; hint?: string } | null>(null);
  const copyTimer = useRef<number | undefined>(undefined);
  const showCopyNote = (n: { text: string; ok: boolean; hint?: string }) => {
    window.clearTimeout(copyTimer.current);
    setCopyNote(n);
    if (n.ok || n.hint) copyTimer.current = window.setTimeout(() => setCopyNote(null), n.hint ? 4000 : 1800);
  };
  const lastCopied = useRef("");
  const copy = useCallback(async (text: string, _source: CopySource) => {
    if (!text) return;
    lastCopied.current = text;
    // OSC 52 from tmux arrives over the WebSocket, usually still within the mouse gesture's activation window;
    // when the browser refuses anyway, the toast offers a one-click "Copy now" (a fresh user gesture).
    const ok = await writeClipboard(text);
    showCopyNote({ text, ok });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const copyActiveSelection = () => {
    const sel = activeId ? handles.get(activeId)?.selection() ?? "" : "";
    if (sel) void copy(sel, "selection");
    else if (lastCopied.current) void copy(lastCopied.current, "selection"); // re-copy the last terminal copy
    else showCopyNote({ text: "", ok: false, hint: `Select text first: drag in the terminal (tmux selection, copied on release) or ${isMac ? "Option" : "Shift"}+drag (browser selection).` });
    if (activeId) handles.get(activeId)?.focus();
  };

  useEffect(() => {
    const onChange = () => {
      const fs = document.fullscreenElement === panelRef.current && !!panelRef.current;
      setFullscreen(fs);
      if (!fs) {
        (navigator as any).keyboard?.unlock?.();
        setKbLocked(false);
      }
      // re-fit after the size change
      if (activeId) window.setTimeout(() => handles.get(activeId)?.focus(), 50);
    };
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, [activeId]);

  const toggleFullscreen = async () => {
    try {
      if (document.fullscreenElement) {
        await document.exitFullscreen();
        return;
      }
      await panelRef.current?.requestFullscreen();
      // In full screen, Chromium lets a page capture reserved shortcuts (Ctrl+T, Ctrl+W, Ctrl+N, …).
      if (kbLockSupported) {
        await (navigator as any).keyboard.lock();
        setKbLocked(true);
      }
    } catch (e) {
      setError(`Full screen / keyboard capture not available: ${(e as Error).message}`);
    }
  };

  const sendKey = (bytes: string) => {
    if (!activeId) return;
    const h = handles.get(activeId);
    if (!h) return;
    h.send(new TextEncoder().encode(bytes));
    h.focus();
  };
  const sendArrow = (dir: "A" | "B" | "C" | "D") => {
    if (!activeId) return;
    const h = handles.get(activeId);
    if (!h) return;
    sendKey((h.appCursor() ? "\x1bO" : "\x1b[") + dir);
  };

  const create = async () => {
    setError(null);
    try {
      const t = await post<TerminalInfo>("/terminals", { cwd: workspaceRoot });
      await onChanged();
      onActive(t.id);
    } catch (e) {
      setError((e as ApiError).message);
    }
  };

  const close = async (t: TerminalInfo) => {
    // A live terminal has running processes: ask once. An exited/unavailable one has nothing to kill: just dismiss it.
    if (t.state === "live" && !window.confirm(`Close terminal "${t.label}"? This terminates its shell and any running processes. Disconnecting the browser never closes terminals.`)) return;
    try {
      await del(`/terminals/${t.id}`);
      await onChanged();
      if (activeId === t.id) onActive(null);
    } catch (e) {
      setError((e as ApiError).message);
    }
  };

  const clearExited = async () => {
    try {
      for (const t of terminals.filter((x) => x.state !== "live")) await del(`/terminals/${t.id}`);
      await onChanged();
    } catch (e) {
      setError((e as ApiError).message);
    }
  };

  const rename = async (t: TerminalInfo) => {
    const label = window.prompt("Terminal label", t.label);
    if (!label || label === t.label) return;
    await patch(`/terminals/${t.id}`, { label });
    await onChanged();
  };

  const move = async (t: TerminalInfo, dir: -1 | 1) => {
    const idx = terminals.findIndex((x) => x.id === t.id);
    const other = terminals[idx + dir];
    if (!other) return;
    await patch(`/terminals/${t.id}`, { tab_order: other.tab_order });
    await patch(`/terminals/${other.id}`, { tab_order: t.tab_order });
    await onChanged();
  };

  const showHistory = async (t: TerminalInfo) => {
    try {
      const h = await get<{ text: string; note: string }>(`/terminals/${t.id}/history?lines=${info?.history_limit ?? 10000}`);
      setHistory({ id: t.id, text: h.text, note: h.note });
    } catch (e) {
      setError((e as ApiError).message);
    }
  };

  const active = terminals.find((t) => t.id === activeId) ?? null;

  return (
    <div className={`terminal-panel ${fullscreen ? "fullscreen" : ""}`} data-testid="terminal-panel" ref={panelRef}>
      <div className="term-tabs">
        {terminals.map((t) => (
          <span key={t.id} className={`term-tab ${t.id === activeId ? "active" : ""} ${t.state}`} onClick={() => onActive(t.id)} onDoubleClick={() => void rename(t)} title={`${t.cwd} · tmux ${t.tmux_session}`} data-testid="terminal-tab" role="tab">
            <span className={`dot ${states[t.id] ?? t.state}`} />
            {t.label}
            {t.state !== "live" && <span className="muted small"> ({t.state})</span>}
            {t.state !== "live" && (
              <button className="tab-x" onClick={(e) => { e.stopPropagation(); void close(t); }} title="Dismiss this exited terminal" data-testid="terminal-dismiss" aria-label="Dismiss">×</button>
            )}
          </span>
        ))}
        <button className="btn small" onClick={() => void create()} disabled={!info?.tmux_available} title={info?.install_hint ?? "Create a new tmux-backed terminal"} data-testid="terminal-new">
          + New terminal
        </button>
        {terminals.some((t) => t.state !== "live") && (
          <button className="btn small" onClick={() => void clearExited()} title="Remove all exited terminals from the list" data-testid="terminal-clear-exited">Clear exited</button>
        )}
        <span className="spacer" />
        {active && (
          <>
            <button className="btn small" onClick={() => void move(active, -1)} title="Move tab left">◂</button>
            <button className="btn small" onClick={() => void move(active, 1)} title="Move tab right">▸</button>
            <button className={`btn small toggle ${mods.ctrl ? "on" : ""}`} onClick={() => { setMods((m) => ({ ...m, ctrl: !m.ctrl })); handles.get(active.id)?.focus(); }} aria-pressed={mods.ctrl} title="Sticky Ctrl: the next key you type is sent with Ctrl (e.g. Ctrl, then T = Ctrl+T)" data-testid="term-sticky-ctrl">Ctrl</button>
            <button className={`btn small toggle ${mods.alt ? "on" : ""}`} onClick={() => { setMods((m) => ({ ...m, alt: !m.alt })); handles.get(active.id)?.focus(); }} aria-pressed={mods.alt} title="Sticky Alt/Meta: the next key you type is sent with Alt" data-testid="term-sticky-alt">Alt</button>
            <span className="keys-anchor">
              <button className={`btn small toggle ${showKeys ? "on" : ""}`} onClick={() => setShowKeys((v) => !v)} title="Send keys the browser reserves (Ctrl+T, Ctrl+W, …) and special keys" data-testid="term-keys">Keys ▾</button>
              {showKeys && (
                <div className="keys-menu" data-testid="term-keys-menu" onMouseDown={(e) => e.preventDefault()}>
                  <div className="muted small">Reserved by the browser in a normal tab:</div>
                  <div className="keys-row">
                    {["T", "W", "N", "Q", "R", "L", "J", "K"].map((k) => (
                      <button key={k} className="btn small" onClick={() => sendKey(String.fromCharCode(ctrlByte(k)!))} data-testid={`term-key-ctrl-${k.toLowerCase()}`}>Ctrl+{k}</button>
                    ))}
                  </div>
                  <div className="muted small">Common:</div>
                  <div className="keys-row">
                    <button className="btn small" onClick={() => sendKey("\x03")}>Ctrl+C</button>
                    <button className="btn small" onClick={() => sendKey("\x04")}>Ctrl+D</button>
                    <button className="btn small" onClick={() => sendKey("\x1a")}>Ctrl+Z</button>
                    <button className="btn small" onClick={() => sendKey("\x02")} title="tmux prefix of the app's tmux server">Ctrl+B</button>
                    <button className="btn small" onClick={() => sendKey("\x1b")} data-testid="term-key-esc">Esc</button>
                    <button className="btn small" onClick={() => sendKey("\t")}>Tab</button>
                    <button className="btn small" onClick={() => sendKey("\x1b[Z")}>Shift+Tab</button>
                  </div>
                  <div className="keys-row">
                    <button className="btn small" onClick={() => sendArrow("A")} aria-label="Up">↑</button>
                    <button className="btn small" onClick={() => sendArrow("B")} aria-label="Down">↓</button>
                    <button className="btn small" onClick={() => sendArrow("D")} aria-label="Left">←</button>
                    <button className="btn small" onClick={() => sendArrow("C")} aria-label="Right">→</button>
                    <button className="btn small" onClick={() => sendKey("\x1b[5~")}>PgUp</button>
                    <button className="btn small" onClick={() => sendKey("\x1b[6~")}>PgDn</button>
                    <button className="btn small" onClick={() => sendKey("\x1b[H")}>Home</button>
                    <button className="btn small" onClick={() => sendKey("\x1b[F")}>End</button>
                  </div>
                  <div className="muted small">
                    Any other combo: click <b>Ctrl</b> (or <b>Alt</b>) above, then type the key. Or use <b>Full screen</b>
                    {kbLockSupported ? ", which captures every shortcut (hold Esc to leave)." : " (this browser cannot capture reserved shortcuts; use these buttons)."}
                  </div>
                </div>
              )}
            </span>
            <button className={`btn small toggle ${fullscreen ? "on" : ""}`} onClick={() => void toggleFullscreen()} title={kbLockSupported ? "Full screen with keyboard capture: Ctrl+T, Ctrl+W, Ctrl+N… go to the terminal. Hold Esc to leave." : "Full screen (this browser cannot capture Ctrl+T/W/N; use Keys ▾)"} data-testid="term-fullscreen">⛶ {fullscreen ? "Exit" : "Full screen"}</button>
            <button className="btn small" onMouseDown={(e) => e.preventDefault()} onClick={copyActiveSelection} title={`Copy the terminal selection (${isMac ? "⌘C" : "Ctrl+Shift+C"}); with nothing selected, copy the last terminal copy again. Dragging in the terminal copies automatically.`} data-testid="term-copy">Copy</button>
            <button className="btn small" onClick={() => void showHistory(active)} title="Snapshot of the tmux pane history (server-side), distinct from the browser scrollback">tmux history</button>
            <button className="btn small danger" onClick={() => void close(active)} data-testid="terminal-close">{active.state === "live" ? "Close terminal" : "Dismiss"}</button>
          </>
        )}
      </div>
      {info && !info.tmux_available && (
        <div className="banner warn">tmux is not installed on the server; terminals are disabled. {info.install_hint}</div>
      )}
      {info?.locale_warning && <div className="banner">{info.locale_warning}</div>}
      {fullscreen && (
        <div className="banner small" data-testid="term-kb-banner">
          {kbLocked ? "Keyboard captured: Ctrl+T, Ctrl+W, Ctrl+N and other shortcuts go to the terminal. Press and hold Esc to leave full screen." : "Full screen. This browser does not allow capturing Ctrl+T/W/N; use Keys ▾ or sticky Ctrl."}
        </div>
      )}
      {(mods.ctrl || mods.alt) && (
        <div className="banner small" data-testid="term-mods-banner">Next key is sent with {[mods.ctrl && "Ctrl", mods.alt && "Alt"].filter(Boolean).join("+")} — type it in the terminal.</div>
      )}
      {error && <div className="error-text pad">{error}</div>}
      <div className="term-views">
        {terminals.length === 0 && (
          <div className="muted pad">
            No terminals yet. Create one to run your shell, OpenCode, Codex or Git on the server. Sessions persist in tmux when the browser disconnects.
          </div>
        )}
        {terminals.map((t) => (
          <TerminalView
            key={t.id}
            terminal={t}
            active={visible && t.id === activeId}
            onState={(s) => setStates((m) => ({ ...m, [t.id]: s }))}
            onExited={() => void onChanged()}
            onDismiss={() => void close(t)}
            fontSize={fontSize}
            modsRef={modsRef}
            onModsUsed={() => setMods({ ctrl: false, alt: false })}
            onCopy={copy}
          />
        ))}
      </div>
      {copyNote && (
        <div className="toast term-copy-toast" data-testid="term-copy-toast" data-ok={copyNote.ok ? "true" : "false"}>
          {copyNote.hint ? (
            copyNote.hint
          ) : copyNote.ok ? (
            `Copied ${[...copyNote.text].length} character${[...copyNote.text].length === 1 ? "" : "s"} to the clipboard`
          ) : (
            <>
              The browser blocked the clipboard.{" "}
              <button className="link-btn toast-link" onClick={() => void copy(copyNote.text, "selection")} data-testid="term-copy-retry">Copy now</button>{" "}
              <button className="link-btn toast-link" onClick={() => setCopyNote(null)}>Dismiss</button>
            </>
          )}
        </div>
      )}
      {history && (
        <div className="modal-backdrop" onClick={() => setHistory(null)}>
          <div className="modal wide" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <span>tmux pane history snapshot</span>
              <span className="spacer" />
              <button className="btn small" onClick={() => void copy(history.text, "selection")} data-testid="term-history-copy">Copy all</button>
              <button className="icon-btn" onClick={() => setHistory(null)}>×</button>
            </div>
            <div className="muted small">{history.note} This is the server-side tmux buffer, not the browser's xterm scrollback. Inside the terminal you can also scroll (tmux copy mode) to view history.</div>
            <pre className="history-pre" data-testid="term-history-text">{history.text}</pre>
          </div>
        </div>
      )}
    </div>
  );
}

function wsUrl(id: string, cols: number, rows: number): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/api/v1/terminals/${id}/ws?cols=${cols}&rows=${rows}`;
}

function b64encode(bytes: Uint8Array): string {
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, Array.from(bytes.subarray(i, i + 0x8000)));
  return btoa(s);
}

function b64decode(s: string): Uint8Array {
  const bin = atob(s);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

export function TerminalView({ terminal, active, onState, onExited, onDismiss, fontSize, modsRef, onModsUsed, onCopy }: { terminal: TerminalInfo; active: boolean; onState: (s: ConnState) => void; onExited: () => void; onDismiss: () => void; fontSize: number; modsRef: React.MutableRefObject<StickyMods>; onModsUsed: () => void; onCopy: (text: string, source: CopySource) => void }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<Terminal | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const [state, setState] = useState<ConnState>("connecting");
  const [message, setMessage] = useState<string>("");
  const encoder = useRef(new TextEncoder());
  const onCopyRef = useRef(onCopy);
  onCopyRef.current = onCopy;

  const setConn = useCallback(
    (s: ConnState, msg = "") => {
      setState(s);
      setMessage(msg);
      onState(s);
    },
    [onState],
  );

  const connect = useCallback(() => {
    const term = termRef.current;
    if (!term) return;
    wsRef.current?.close();
    const csrf = getCsrf() ?? "";
    const ws = new WebSocket(wsUrl(terminal.id, term.cols, term.rows), [`waw-csrf.${csrf}`]);
    wsRef.current = ws;
    setConn("connecting");
    let writeChain: Promise<void> = Promise.resolve();
    ws.onopen = () => {
      ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
    };
    ws.onmessage = (ev) => {
      let msg: any;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (msg.type === "output") {
        const bytes = b64decode(msg.data);
        // write raw bytes; xterm.js decodes them as UTF-8. Wait for the callback to keep ordering.
        writeChain = writeChain.then(() => new Promise<void>((resolve) => term.write(bytes, resolve)));
      } else if (msg.type === "state") {
        setConn(msg.state as ConnState, msg.message ?? "");
        if (msg.state === "exited") onExited();
      }
    };
    ws.onclose = (ev) => {
      if (wsRef.current !== ws) return;
      setState((s) => {
        if (s === "exited" || s === "error") return s;
        const next: ConnState = ev.code === 4410 ? "exited" : ev.code === 4401 || ev.code === 4403 ? "error" : "detached";
        onState(next);
        if (next === "error") setMessage("Not authorized to attach (log in again)");
        else if (next === "detached") setMessage("Connection lost; the tmux session keeps running.");
        return next;
      });
    };
    ws.onerror = () => {
      /* onclose follows */
    };
  }, [terminal.id, setConn, onExited, onState]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const term = new Terminal({
      cursorBlink: true,
      allowProposedApi: true,
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace",
      fontSize,
      scrollback: 5000,
      theme: { background: "#0f1115", foreground: "#e6e6e6", cursor: "#9cdcfe", selectionBackground: "#3a5a85" },
      // tmux has the mouse: Shift+drag (Option+drag on macOS) makes a browser-side selection instead
      macOptionClickForcesSelection: true,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(host);
    termRef.current = term;
    fitRef.current = fit;
    const sendBytes = (bytes: Uint8Array) => {
      const ws = wsRef.current;
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      ws.send(JSON.stringify({ type: "input", data: b64encode(bytes) }));
    };
    handles.set(terminal.id, {
      send: sendBytes,
      focus: () => term.focus(),
      appCursor: () => !!(term as any).modes?.applicationCursorKeysMode,
      selection: () => term.getSelection(),
    });
    // OSC 52 "set clipboard": tmux sends it when you finish a mouse/copy-mode selection (set-clipboard on);
    // programs such as vim or OpenCode may send it too. Payload: "<targets>;<base64>" ("?" = query, ignored).
    const osc52 = term.parser.registerOscHandler(52, (data) => {
      const semi = data.indexOf(";");
      const payload = semi >= 0 ? data.slice(semi + 1) : data;
      if (!payload || payload === "?") return true;
      const text = decodeBase64Utf8(payload);
      if (text) onCopyRef.current(text, "tmux");
      return true;
    });
    // Copy shortcuts: Ctrl+Shift+C (⌘C on macOS) copies the browser-side selection; plain Ctrl+C copies too while
    // text is selected (like Windows Terminal) and is otherwise sent to the shell as an interrupt.
    term.attachCustomKeyEventHandler((ev) => {
      if (ev.type !== "keydown") return true;
      const k = ev.key.toLowerCase();
      const copyCombo = (ev.ctrlKey && ev.shiftKey && !ev.altKey && k === "c") || (isMac && ev.metaKey && !ev.ctrlKey && k === "c");
      const plainCtrlC = ev.ctrlKey && !ev.shiftKey && !ev.altKey && !ev.metaKey && k === "c";
      if (copyCombo || (plainCtrlC && term.hasSelection())) {
        if (term.hasSelection()) onCopyRef.current(term.getSelection(), "selection");
        term.clearSelection();
        ev.preventDefault(); // Ctrl+Shift+C would otherwise open the browser's inspector
        return false;
      }
      // Ctrl+Shift+V / ⌘V: let the browser paste (xterm.js receives the paste event) instead of sending ^V
      if ((ev.ctrlKey && ev.shiftKey && k === "v") || (isMac && ev.metaKey && k === "v")) return false;
      // Home / End. xterm.js sends them for the Home/End keys (and Fn+←/→ on a Mac), but sends nothing for ⌘← / ⌘→,
      // which is how macOS moves to the start / end of a line: send Home / End for those too.
      if (isMac && ev.metaKey && !ev.ctrlKey && !ev.altKey && !ev.shiftKey && (ev.key === "ArrowLeft" || ev.key === "ArrowRight")) {
        const app = term.modes.applicationCursorKeysMode;
        sendBytes(encoder.current.encode(ev.key === "ArrowLeft" ? (app ? "\x1bOH" : "\x1b[H") : (app ? "\x1bOF" : "\x1b[F")));
        ev.preventDefault();
        return false;
      }
      // ⌘⌫ on a Mac deletes to the start of the line (Ctrl+U), like in a text box
      if (isMac && ev.metaKey && !ev.ctrlKey && !ev.altKey && ev.key === "Backspace") {
        sendBytes(encoder.current.encode("\x15"));
        ev.preventDefault();
        return false;
      }
      return true;
    });
    // Like a desktop terminal: releasing a Shift+drag browser selection copies it.
    let lastAutoCopied = "";
    const onMouseUp = (ev: MouseEvent) => {
      if (ev.button !== 0) return;
      window.setTimeout(() => {
        const sel = term.hasSelection() ? term.getSelection() : "";
        if (sel && sel !== lastAutoCopied) onCopyRef.current(sel, "selection");
        lastAutoCopied = sel;
      }, 0);
    };
    host.addEventListener("mouseup", onMouseUp);
    term.onData((data) => {
      const m = modsRef.current;
      if (m.ctrl || m.alt) {
        const combined = applyMods(data, m);
        onModsUsed();
        if (combined !== null) {
          sendBytes(encoder.current.encode(combined));
          return;
        }
      }
      sendBytes(encoder.current.encode(data));
    });
    term.onBinary((data) => {
      // binary strings: copy each char's low 8 bits (e.g. mouse reports are not UTF-8 text)
      const ws = wsRef.current;
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      const bytes = new Uint8Array(data.length);
      for (let i = 0; i < data.length; i++) bytes[i] = data.charCodeAt(i) & 0xff;
      ws.send(JSON.stringify({ type: "input", data: b64encode(bytes) }));
    });
    term.onResize(({ cols, rows }) => {
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "resize", cols, rows }));
    });
    if (terminal.state === "live") connect();
    else setConn(terminal.state === "closed" ? "closed" : "exited", `This terminal is ${terminal.state}; its tmux session no longer exists. Create a new terminal.`);
    const ping = window.setInterval(() => {
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "ping" }));
    }, 25000);
    return () => {
      window.clearInterval(ping);
      osc52.dispose();
      host.removeEventListener("mouseup", onMouseUp);
      handles.delete(terminal.id);
      wsRef.current?.close();
      term.dispose();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [terminal.id]);

  useEffect(() => {
    const term = termRef.current;
    if (!term) return;
    term.options.fontSize = fontSize;
    if (active) {
      try {
        fitRef.current?.fit();
      } catch {
        /* hidden */
      }
    }
  }, [fontSize, active]);

  // Fit when shown / resized; the most recently focused client controls the tmux size.
  useEffect(() => {
    if (!active) return;
    const doFit = () => {
      try {
        fitRef.current?.fit();
      } catch {
        /* hidden */
      }
    };
    const id = window.setTimeout(() => {
      doFit();
      termRef.current?.focus();
    }, 30);
    const ro = new ResizeObserver(doFit);
    if (hostRef.current) ro.observe(hostRef.current);
    return () => {
      window.clearTimeout(id);
      ro.disconnect();
    };
  }, [active]);

  return (
    <div className={`term-view ${active ? "active" : ""}`} data-testid={`terminal-view-${terminal.id}`} data-state={state}>
      <div className={`term-status ${state}`}>
        <span>{statusText(state)}</span>
        {message && <span className="muted small"> · {message}</span>}
        <span className="spacer" />
        {(state === "detached" || state === "connecting") && terminal.state === "live" && (
          <button className="btn small" onClick={connect} data-testid="terminal-reconnect">Reconnect</button>
        )}
        {(state === "exited" || state === "closed") && (
          <button className="btn small" onClick={onDismiss} data-testid="terminal-dismiss-status">Dismiss</button>
        )}
      </div>
      <div className="xterm-host" ref={hostRef} onFocus={() => fitRef.current?.fit()} />
    </div>
  );
}

function statusText(s: ConnState): string {
  switch (s) {
    case "connecting":
      return "Connecting…";
    case "connected":
      return "Connected (tmux)";
    case "detached":
      return "Detached";
    case "exited":
      return "Exited";
    case "closed":
      return "Closed";
    case "error":
      return "Error";
  }
}
