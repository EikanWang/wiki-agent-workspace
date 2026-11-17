import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, HostKeyHop, MachineCaps, MachineCheck, MachineInfo, SshKeyInfo, TerminalInfo, del, get, patch, post, subscribeGlobal } from "../api";
import { writeClipboard } from "../clipboard";
import { confirmAction } from "./Confirm";
import { PlusIcon } from "./Icons";
import { StickyMods, TerminalView } from "./TerminalPanel";

/**
 * Machines: this server and the SSH machines (and the bastions they are reached through) the app works on — those of
 * `[[development.hosts]]` in the config file (read-only here) and those added in the app. Each can be checked hop by
 * hop, probed (OS, CPU, GPUs, tools), have its host keys confirmed and this server's key installed (ssh-copy-id in a
 * terminal: the password is typed there, never seen by the app).
 */

const STATUS_TEXT: Record<string, string> = {
  online: "online", unknown: "not checked", auth_failed: "login refused", hostkey_unknown: "host key not trusted",
  hostkey_changed: "host key changed", dns: "name does not resolve", timeout: "no answer", refused: "connection refused",
  unreachable: "unreachable", shell_failed: "remote shell failed", unknown_bastion: "bastion missing", failed: "failed", skipped: "skipped",
};

export function useMachines(enabled: boolean): { machines: MachineInfo[]; allowed: string[]; reload: () => Promise<void> } {
  const [machines, setMachines] = useState<MachineInfo[]>([]);
  const [allowed, setAllowed] = useState<string[]>([]);
  const reload = useCallback(async () => {
    try {
      const r = await get<{ machines: MachineInfo[]; allowed_options: string[] }>("/machines");
      setMachines(r.machines);
      setAllowed(r.allowed_options);
    } catch {
      /* shown where it is used */
    }
  }, []);
  useEffect(() => {
    if (!enabled) return;
    void reload();
    return subscribeGlobal((ev) => { if (ev.type === "machine_changed") void reload(); });
  }, [enabled, reload]);
  return { machines, allowed, reload };
}

function statusClass(s: string): string {
  return s === "online" ? "ok" : s === "unknown" || s === "skipped" ? "" : "bad";
}

export function MachinesPanel({ machines, active, onOpen, onAdd }: {
  machines: MachineInfo[];
  active: string | null;
  onOpen: (name: string) => void;
  onAdd: () => void;
}) {
  const group = (kind: "machine" | "bastion") => machines.filter((m) => m.kind === kind);
  const item = (m: MachineInfo) => (
    <div key={m.name} className={`list-item terminals-item ${m.name === active ? "selected" : ""}`} onClick={() => onOpen(m.name)} role="button"
      title={m.ssh_command ?? "this server"} data-testid="machines-item" data-name={m.name} data-status={m.status}>
      <span className={`machine-dot ${statusClass(m.status)}`} aria-hidden="true" />
      <span className="terminals-text">
        <span className="terminals-label">{m.label ?? m.name}{m.source === "config" ? <span className="muted small"> · config</span> : null}</span>
        <span className="muted small mono terminals-cwd">{m.ssh ? `${m.via.length ? m.via.join(" → ") + " → " : ""}${m.ssh}` : "local"} · {STATUS_TEXT[m.status] ?? m.status}</span>
      </span>
    </div>
  );
  return (
    <div className="terminals-panel" data-testid="machines-panel">
      <div className="local-head">
        <b>Machines</b>
        <span className="spacer" />
        <button className="icon-btn" onClick={onAdd} title="Add a machine or a bastion" aria-label="Add machine" data-testid="machines-add"><PlusIcon /></button>
      </div>
      <div className="terminals-list">
        {group("machine").map(item)}
        {group("bastion").length > 0 && <div className="muted small pad-x machines-group">Bastions</div>}
        {group("bastion").map(item)}
      </div>
    </div>
  );
}

const kb = (n: number | null | undefined) => (n == null ? "—" : n >= 1024 ** 3 ? `${(n / 1024 ** 3).toFixed(1)} TiB` : n >= 1024 ** 2 ? `${(n / 1024 ** 2).toFixed(1)} GiB` : `${Math.round(n / 1024)} MiB`);

function CapsView({ caps }: { caps: MachineCaps }) {
  const cpu = caps.cpu;
  return (
    <div className="machine-caps" data-testid="machine-caps">
      {caps.error && <div className="warn small">The probe was cut short: {caps.error}</div>}
      <table className="machine-table">
        <tbody>
          <tr><th>System</th><td>{caps.os.pretty ?? caps.os.kernel} · {caps.os.release} · {caps.os.arch} · {caps.os.hostname}</td></tr>
          <tr><th>CPU</th><td>{cpu.model ?? "—"} · {cpu.threads ?? "?"} threads{cpu.sockets ? ` · ${cpu.sockets} socket(s) × ${cpu.cores_per_socket} cores` : ""}{cpu.numa_nodes ? ` · ${cpu.numa_nodes} NUMA node(s)` : ""}</td></tr>
          <tr><th>Memory</th><td>{kb(caps.memory_kb)}</td></tr>
          <tr><th>Disk</th><td>{kb(caps.disk.free_kb)} free of {kb(caps.disk.total_kb)} <span className="muted mono">({caps.disk.path ?? caps.work_dir})</span></td></tr>
          <tr><th>sudo</th><td>{caps.sudo === "passwordless" ? "password-less" : caps.sudo === "password" ? "asks for a password" : "not available"}{caps.package_manager ? ` · ${caps.package_manager}` : ""}</td></tr>
          {caps.missing_required.length > 0 && <tr><th>Missing</th><td className="error-text">{caps.missing_required.join(", ")} (needed by the app)</td></tr>}
        </tbody>
      </table>
      <h4>GPUs</h4>
      {caps.gpus.length === 0 ? <div className="muted small">None found.</div> : (
        <table className="machine-table" data-testid="machine-gpus">
          <thead><tr><th>#</th><th>Vendor</th><th>Name</th><th>PCI</th><th>Driver</th><th>Memory</th><th>From</th></tr></thead>
          <tbody>{caps.gpus.map((g) => <tr key={g.pci + g.source}><td>{g.index ?? ""}</td><td>{g.vendor}</td><td>{g.name}</td><td className="mono">{g.pci}</td><td>{g.driver ?? ""}</td><td>{g.memory ?? ""}</td><td className="muted">{g.source}</td></tr>)}</tbody>
        </table>
      )}
      <h4>Tools</h4>
      <table className="machine-table" data-testid="machine-tools">
        <tbody>
          {Object.entries(caps.tools).map(([n, t]) => (
            <tr key={n} className={t.ok ? "" : "missing"}><th>{n}</th><td>{t.ok ? <>{t.version ?? "found"} <span className="muted mono">{t.path}</span></> : <span className={t.required ? "error-text" : "muted"}>missing — {t.for}</span>}</td></tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function HostKeys({ name, onDone }: { name: string; onDone: () => void }) {
  const [hops, setHops] = useState<HostKeyHop[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const load = useCallback(async () => {
    setErr(null);
    try {
      setHops((await get<{ hops: HostKeyHop[] }>(`/machines/${name}/hostkeys`)).hops);
    } catch (e) {
      setErr((e as ApiError).message);
    }
  }, [name]);
  useEffect(() => { void load(); }, [load]);
  const trust = async (h: HostKeyHop) => {
    const changed = h.keys.some((k) => k.state === "changed");
    if (changed && !(await confirmAction({ title: `Trust the new host key of ${h.name}?`, message: "Its key changed since it was trusted. That happens when a machine is reinstalled — or when someone intercepts the connection. Only continue if you know why it changed.", confirmLabel: "Trust the new key", danger: true }))) return;
    setBusy(h.name);
    setErr(null);
    try {
      await post(`/machines/${name}/hostkeys`, { hop: h.name, fingerprints: h.keys.filter((k) => k.state !== "trusted").map((k) => k.fingerprint) });
      await load();
      onDone();
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="machine-section" data-testid="machine-hostkeys">
      <h4>Host keys <button className="btn small" onClick={() => void load()}>Scan again</button></h4>
      <div className="muted small">Compare each fingerprint with the one your administrator gives you (on the machine: <span className="mono">ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub</span>) before trusting it.</div>
      {!hops && !err && <div className="muted small">Scanning…</div>}
      {hops?.map((h) => (
        <div key={h.name} className="machine-hop">
          <div><b>{h.name}</b> <span className="muted small">{h.known_name ?? ""} {h.scanned ?? ""}</span> <span className={`chip ${h.status === "trusted" ? "" : "warn"}`}>{h.status}</span></div>
          {h.error && <div className="error-text small">{h.error}</div>}
          {h.keys.map((k) => <div key={k.fingerprint} className={`mono small hostkey ${k.state}`}>{k.type} {k.fingerprint} — {k.state}{k.files.length ? ` (${k.files.join(", ")})` : ""}</div>)}
          {h.status !== "trusted" && h.keys.length > 0 && (h.can_trust
            ? <button className={`btn small ${h.status === "changed" ? "danger" : "primary"}`} disabled={busy === h.name} onClick={() => void trust(h)} data-testid="hostkey-trust">{busy === h.name ? "Trusting…" : "Trust"}</button>
            : <div className="muted small">From the config file: log in once with <span className="mono">ssh</span> in a terminal to accept it.</div>)}
        </div>
      ))}
      {err && <div className="error-text small" role="alert">{err}</div>}
    </div>
  );
}

function InstallKey({ machine, onDone }: { machine: MachineInfo; onDone: () => void }) {
  const [keys, setKeys] = useState<SshKeyInfo[] | null>(null);
  const [key, setKey] = useState("");
  const [hop, setHop] = useState(machine.name);
  const [term, setTerm] = useState<TerminalInfo | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const modsRef = useRef<StickyMods>({ ctrl: false, alt: false });
  const load = useCallback(async () => {
    try {
      const r = await get<{ keys: SshKeyInfo[] }>("/ssh-keys");
      setKeys(r.keys);
      setKey((k) => k || r.keys[0]?.public_key_file || "");
    } catch (e) {
      setErr((e as ApiError).message);
    }
  }, []);
  useEffect(() => { void load(); }, [load]);
  const create = async () => {
    setErr(null);
    try {
      await post("/ssh-keys");
      await load();
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };
  const start = async () => {
    setErr(null);
    try {
      const r = await post<{ terminal: TerminalInfo }>(`/machines/${machine.name}/install-key`, { hop, public_key_file: key });
      setTerm(r.terminal);
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };
  const finish = async () => {
    if (term) await del(`/terminals/${term.id}`).catch(() => undefined);
    setTerm(null);
    onDone();
  };
  return (
    <div className="machine-section" data-testid="machine-install-key">
      <h4>Install this server's key</h4>
      <div className="muted small">Runs <span className="mono">ssh-copy-id</span> in a terminal below. Type the password there when asked: the app never sees it.</div>
      {keys && keys.length === 0 && <div className="row"><span className="small">This server has no SSH key yet.</span><button className="btn small" onClick={() => void create()} data-testid="ssh-key-create">Create key (ed25519)</button></div>}
      {keys && keys.length > 0 && !term && (
        <div className="row">
          <label className="small">Key <select value={key} onChange={(e) => setKey(e.target.value)}>{keys.map((k) => <option key={k.public_key_file} value={k.public_key_file}>{k.public_key_file.split("/").pop()} — {k.fingerprint.slice(0, 20)}…</option>)}</select></label>
          {machine.via.length > 0 && <label className="small">on <select value={hop} onChange={(e) => setHop(e.target.value)}>{[...machine.via, machine.name].map((h) => <option key={h} value={h}>{h}</option>)}</select></label>}
          <button className="btn small primary" onClick={() => void start()} data-testid="install-key-start">Open terminal</button>
        </div>
      )}
      {term && (
        <>
          <div className="machine-terminal">
            <TerminalView terminal={term} active fontSize={13} modsRef={modsRef} onState={() => undefined} onExited={() => void finish()} onDismiss={() => void finish()}
              onModsUsed={() => { modsRef.current = { ctrl: false, alt: false }; }} onCopy={(text) => void writeClipboard(text)} />
          </div>
          <div className="row"><span className="spacer" /><button className="btn small" onClick={() => void finish()}>Close and check again</button></div>
        </>
      )}
      {err && <div className="error-text small" role="alert">{err}</div>}
    </div>
  );
}

export default function MachineView({ machine, machines, allowed, onOpenTerminal, onDeleted, onChanged }: {
  machine: MachineInfo;
  machines: MachineInfo[];
  allowed: string[];
  onOpenTerminal: (t: TerminalInfo) => void;
  onDeleted: () => void;
  onChanged: () => Promise<void> | void;
}) {
  const [check, setCheck] = useState<MachineCheck | null>(machine.check ?? null);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [panel, setPanel] = useState<"hostkeys" | "install" | null>(null);
  const [edit, setEdit] = useState(false);
  const isLocal = machine.source === "server";
  const isBastion = machine.kind === "bastion";
  useEffect(() => { setCheck(machine.check ?? null); setPanel(null); setErr(null); }, [machine.name]); // eslint-disable-line react-hooks/exhaustive-deps
  const run = async (what: string, fn: () => Promise<void>) => {
    setBusy(what);
    setErr(null);
    try {
      await fn();
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(null);
    }
  };
  const doCheck = () => run("check", async () => {
    const r = await post<MachineCheck>(`/machines/${machine.name}/check`);
    setCheck(r);
    if (r.status === "hostkey_unknown" || r.status === "hostkey_changed") setPanel("hostkeys");
    await onChanged();
  });
  const doProbe = () => run("probe", async () => { await post(`/machines/${machine.name}/probe`); await onChanged(); });
  const openTerm = () => run("terminal", async () => { onOpenTerminal(await post<TerminalInfo>(`/machines/${machine.name}/terminals`, {})); });
  const remove = async () => {
    if (!(await confirmAction({ title: `Remove ${machine.name}?`, message: "Only its entry in the app goes; nothing on the machine changes.", confirmLabel: "Remove", danger: true }))) return;
    await run("delete", async () => { await del(`/machines/${machine.name}`); onDeleted(); });
  };
  const used = [...machine.used_by.workspaces.map((w) => `workspace ${w}`), ...machine.used_by.machines.map((m) => `machine ${m}`)];
  return (
    <div className="machine-view" data-testid="machine-view">
      <div className="machine-head">
        <h2>{machine.label ?? machine.name}</h2>
        <span className="chip">{isLocal ? "this server" : machine.kind}</span>
        {machine.source === "config" && <span className="chip" title="Defined in [[development.hosts]] of the config file: edit it there">config file</span>}
        <span className={`chip machine-status ${statusClass(machine.status)}`} data-testid="machine-status">{STATUS_TEXT[machine.status] ?? machine.status}</span>
        <span className="spacer" />
        {!isLocal && <button className="btn small" disabled={!!busy} onClick={() => void doCheck()} data-testid="machine-check">{busy === "check" ? "Checking…" : "Check"}</button>}
        {!isBastion && <button className="btn small" disabled={!!busy} onClick={() => void doProbe()} data-testid="machine-probe">{busy === "probe" ? "Probing…" : "Probe"}</button>}
        {!isLocal && <button className="btn small" onClick={() => setPanel(panel === "hostkeys" ? null : "hostkeys")} data-testid="machine-hostkeys-btn">Host keys</button>}
        {!isLocal && <button className="btn small" onClick={() => setPanel(panel === "install" ? null : "install")} data-testid="machine-install-key-btn">Install key</button>}
        {!isBastion && <button className="btn small" disabled={!!busy} onClick={() => void openTerm()} data-testid="machine-terminal">Terminal</button>}
        {machine.editable && <button className="btn small" onClick={() => setEdit(true)} data-testid="machine-edit">Edit</button>}
        {machine.editable && <button className="btn small danger" disabled={!!busy || used.length > 0} title={used.length ? `In use by ${used.join(", ")}` : "Remove this machine"} onClick={() => void remove()} data-testid="machine-delete">Remove</button>}
      </div>
      {machine.shadowed && <div className="warn small">The config file has a host with the same name; that one is used.</div>}
      {machine.ssh_command && (
        <div className="machine-cmd mono small" title="The same login from a shell on this server">
          {machine.ssh_command} <button className="link-btn small" onClick={() => void writeClipboard(machine.ssh_command ?? "")}>copy</button>
        </div>
      )}
      {!isLocal && (
        <div className="muted small">
          Work folder <span className="mono">{machine.work_dir}</span>
          {machine.via.length > 0 && <> · reached through {machine.via.join(" → ")}</>}
          {used.length > 0 && <> · used by {used.join(", ")}</>}
        </div>
      )}
      {err && <div className="error-text" role="alert" data-testid="machine-error">{err}</div>}
      {check && !isLocal && (
        <div className="machine-section" data-testid="machine-check-result">
          <h4>Connection <span className="muted small">{check.summary}</span></h4>
          <table className="machine-table">
            <tbody>
              {check.hops.map((h) => (
                <tr key={h.name} data-testid="machine-hop" data-status={h.status}>
                  <th>{h.name}</th><td className="muted">{h.kind}</td>
                  <td><span className={`machine-dot ${statusClass(h.status)}`} /> {STATUS_TEXT[h.status] ?? h.status}{h.ok ? ` (${h.ms} ms)` : ""}</td>
                  <td className="small">{h.message}{h.error ? <div className="mono muted small">{h.error}</div> : null}
                    {h.status === "auth_failed" && <button className="link-btn small" onClick={() => setPanel("install")}>Install key…</button>}
                    {(h.status === "hostkey_unknown" || h.status === "hostkey_changed") && <button className="link-btn small" onClick={() => setPanel("hostkeys")}>Host keys…</button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {panel === "hostkeys" && <HostKeys name={machine.name} onDone={() => void doCheck()} />}
      {panel === "install" && <InstallKey machine={machine} onDone={() => { setPanel(null); void doCheck(); }} />}
      {machine.capabilities ? <CapsView caps={machine.capabilities} /> : !isBastion && <div className="muted small">Not probed yet: <b>Probe</b> shows its OS, CPU, GPUs and tools.</div>}
      {edit && <MachineDialog existing={machine} machines={machines} allowed={allowed} onClose={() => setEdit(false)} onSaved={async () => { setEdit(false); await onChanged(); }} />}
    </div>
  );
}

export function MachineDialog({ existing, machines, allowed, onClose, onSaved }: {
  existing?: MachineInfo;
  machines: MachineInfo[];
  allowed: string[];
  onClose: () => void;
  onSaved: (m: MachineInfo) => void | Promise<void>;
}) {
  const [name, setName] = useState(existing?.name ?? "");
  const [kind, setKind] = useState<"machine" | "bastion">(existing?.kind ?? "machine");
  const [ssh, setSsh] = useState(existing?.ssh ?? "");
  const [port, setPort] = useState(existing?.port ? String(existing.port) : "");
  const [user, setUser] = useState(existing?.user ?? "");
  const [identity, setIdentity] = useState(existing?.identity_file ?? "");
  const [via, setVia] = useState<string[]>(existing?.via ?? []);
  const [opts, setOpts] = useState(Object.entries(existing?.options ?? {}).map(([k, v]) => `${k}=${v}`).join("\n"));
  const [workDir, setWorkDir] = useState(existing?.work_dir ?? "");
  const [keys, setKeys] = useState<SshKeyInfo[]>([]);
  const [resolved, setResolved] = useState<{ hostname: string; port: number; user: string | null; proxyjump: string } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const bastions = machines.filter((m) => m.kind === "bastion" && m.name !== existing?.name);
  useEffect(() => { void get<{ keys: SshKeyInfo[] }>("/ssh-keys").then((r) => setKeys(r.keys)).catch(() => undefined); }, []);
  const body = () => {
    const options: Record<string, string> = {};
    for (const line of opts.split("\n")) {
      const t = line.trim();
      if (!t) continue;
      const i = t.search(/[=\s]/);
      if (i < 1) throw new Error(`ssh option "${t}": write Name=value`);
      options[t.slice(0, i)] = t.slice(i + 1).trim();
    }
    return { ...(existing ? {} : { name: name.trim() }), kind, ssh: ssh.trim(), port: port.trim() ? Number(port) : null, user: user.trim(), identity_file: identity.trim(),
      via: kind === "bastion" ? [] : via, options, ...(workDir.trim() ? { work_dir: workDir.trim() } : {}) };
  };
  const resolve = async () => {
    setErr(null);
    try {
      setResolved(await post("/machines/resolve", { ...body(), name: name.trim() || "draft" }));
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  const save = async () => {
    setErr(null);
    setBusy(true);
    try {
      const m = existing ? await patch<MachineInfo>(`/machines/${existing.name}`, body()) : await post<MachineInfo>("/machines", body());
      await onSaved(m);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal dev-setup" onClick={(e) => e.stopPropagation()} role="dialog" aria-label={existing ? `Edit ${existing.name}` : "Add machine"} data-testid="machine-dialog">
        <div className="modal-head"><span>{existing ? `Edit ${existing.name}` : "Add machine"}</span><button className="icon-btn" onClick={onClose} aria-label="Close">×</button></div>
        <div className="dev-pair">
          <label className="dev-field grow"><span className="label">Name</span>
            <input value={name} disabled={!!existing} onChange={(e) => setName(e.target.value)} placeholder="gpu-box" data-testid="machine-name" /></label>
          <label className="dev-field"><span className="label">Kind</span>
            <select value={kind} onChange={(e) => setKind(e.target.value as "machine" | "bastion")} data-testid="machine-kind">
              <option value="machine">Machine (work runs there)</option>
              <option value="bastion">Bastion (jump host)</option>
            </select></label>
        </div>
        <label className="dev-field"><span className="label">SSH destination</span>
          <input value={ssh} onChange={(e) => setSsh(e.target.value)} placeholder="host, user@host or an alias of ~/.ssh/config" className="mono" data-testid="machine-ssh" /></label>
        <div className="dev-pair">
          <label className="dev-field"><span className="label">Port</span><input value={port} onChange={(e) => setPort(e.target.value)} placeholder="22" inputMode="numeric" /></label>
          <label className="dev-field grow"><span className="label">User</span><input value={user} onChange={(e) => setUser(e.target.value)} placeholder="from the destination / ssh config" /></label>
        </div>
        <label className="dev-field"><span className="label">Key</span>
          <select value={identity} onChange={(e) => setIdentity(e.target.value)} data-testid="machine-identity">
            <option value="">ssh's default keys</option>
            {keys.filter((k) => k.identity_file).map((k) => <option key={k.identity_file!} value={k.identity_file!}>{k.identity_file} ({k.type})</option>)}
            {identity && !keys.some((k) => k.identity_file === identity) && <option value={identity}>{identity}</option>}
          </select></label>
        {kind === "machine" && (
          <div className="dev-field">
            <span className="label">Reached through (bastions, first hop first)</span>
            <span className="row">
              {via.map((v, i) => <span key={v} className="chip">{i + 1}. {v} <button className="chip-btn" onClick={() => setVia(via.filter((x) => x !== v))} aria-label={`Remove ${v}`}>×</button></span>)}
              <select value="" onChange={(e) => { if (e.target.value) setVia([...via, e.target.value]); }} data-testid="machine-via">
                <option value="">{bastions.length ? "+ add a bastion" : "no bastions yet (add one with kind: bastion)"}</option>
                {bastions.filter((b) => !via.includes(b.name)).map((b) => <option key={b.name} value={b.name}>{b.name} ({b.ssh})</option>)}
              </select>
            </span>
          </div>
        )}
        {kind === "machine" && (
          <label className="dev-field"><span className="label">Work folder on the machine</span>
            <input value={workDir} onChange={(e) => setWorkDir(e.target.value)} placeholder="~/.cache/wiki-agent-workspace" className="mono" /></label>
        )}
        <label className="dev-field"><span className="label">Extra ssh options (one Name=value per line)</span>
          <textarea value={opts} onChange={(e) => setOpts(e.target.value)} rows={2} className="mono" placeholder="ServerAliveInterval=15" title={`Allowed: ${allowed.join(", ")}`} /></label>
        {resolved && <div className="small" data-testid="machine-resolved">ssh connects to <span className="mono">{resolved.user ? `${resolved.user}@` : ""}{resolved.hostname}:{resolved.port}</span>{resolved.proxyjump ? <> through <span className="mono">{resolved.proxyjump}</span> (ProxyJump of your ssh config)</> : null}</div>}
        {err && <div className="error-text" role="alert" data-testid="machine-dialog-error">{err}</div>}
        <div className="row">
          <button className="btn small" disabled={!ssh.trim()} onClick={() => void resolve()}>Resolve</button>
          <span className="spacer" />
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={busy || !ssh.trim() || (!existing && !name.trim())} onClick={() => void save()} data-testid="machine-save">{busy ? "Saving…" : existing ? "Save" : "Add"}</button>
        </div>
      </div>
    </div>
  );
}
