import { useCallback, useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { ApiError, Bootstrap, TerminalInfo, del, get, getCsrf, patch, post } from "../api";

type ConnState = "connecting" | "connected" | "detached" | "exited" | "error" | "closed";

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
    if (!window.confirm(`Close terminal "${t.label}"? This terminates its shell and any running processes. Disconnecting the browser never closes terminals.`)) return;
    try {
      await del(`/terminals/${t.id}`);
      await onChanged();
      if (activeId === t.id) onActive(null);
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
    <div className="terminal-panel" data-testid="terminal-panel">
      <div className="term-tabs">
        {terminals.map((t) => (
          <button key={t.id} className={`term-tab ${t.id === activeId ? "active" : ""} ${t.state}`} onClick={() => onActive(t.id)} onDoubleClick={() => void rename(t)} title={`${t.cwd} · tmux ${t.tmux_session}`} data-testid="terminal-tab">
            <span className={`dot ${states[t.id] ?? t.state}`} />
            {t.label}
            {t.state !== "live" && <span className="muted small"> ({t.state})</span>}
          </button>
        ))}
        <button className="btn small" onClick={() => void create()} disabled={!info?.tmux_available} title={info?.install_hint ?? "Create a new tmux-backed terminal"} data-testid="terminal-new">
          + New terminal
        </button>
        <span className="spacer" />
        {active && (
          <>
            <button className="btn small" onClick={() => void move(active, -1)} title="Move tab left">◂</button>
            <button className="btn small" onClick={() => void move(active, 1)} title="Move tab right">▸</button>
            <button className="btn small" onClick={() => void showHistory(active)} title="Snapshot of the tmux pane history (server-side), distinct from the browser scrollback">tmux history</button>
            <button className="btn small danger" onClick={() => void close(active)} data-testid="terminal-close">Close terminal</button>
          </>
        )}
      </div>
      {info && !info.tmux_available && (
        <div className="banner warn">tmux is not installed on the server; terminals are disabled. {info.install_hint}</div>
      )}
      {info?.locale_warning && <div className="banner">{info.locale_warning}</div>}
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
            fontSize={fontSize}
          />
        ))}
      </div>
      {history && (
        <div className="modal-backdrop" onClick={() => setHistory(null)}>
          <div className="modal wide" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <span>tmux pane history snapshot</span>
              <button className="icon-btn" onClick={() => setHistory(null)}>×</button>
            </div>
            <div className="muted small">{history.note} This is the server-side tmux buffer, not the browser's xterm scrollback. Inside the terminal you can also scroll (tmux copy mode) to view history.</div>
            <pre className="history-pre">{history.text}</pre>
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

function TerminalView({ terminal, active, onState, onExited, fontSize }: { terminal: TerminalInfo; active: boolean; onState: (s: ConnState) => void; onExited: () => void; fontSize: number }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<Terminal | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const [state, setState] = useState<ConnState>("connecting");
  const [message, setMessage] = useState<string>("");
  const encoder = useRef(new TextEncoder());

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
      theme: { background: "#0f1115", foreground: "#e6e6e6", cursor: "#9cdcfe" },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(host);
    termRef.current = term;
    fitRef.current = fit;
    term.onData((data) => {
      const ws = wsRef.current;
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      ws.send(JSON.stringify({ type: "input", data: b64encode(encoder.current.encode(data)) }));
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
