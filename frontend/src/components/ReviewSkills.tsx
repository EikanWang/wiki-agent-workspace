import { useCallback, useEffect, useState } from "react";
import { ApiError, get, put } from "../api";

/**
 * Reviews ▸ settings ▸ Skills: the skills an agent review of a repository gets (its connected local checkout, its
 * wiki, the open and connected folders), review-related ones first. One opens in an editor; a save is checked
 * against the version read and the list is read again, which is what the next review gets (skills are discovered
 * for every turn, nothing else needs reloading).
 */

export interface ReviewSkill {
  name: string; description: string; path: string; folder: string;
  review: boolean; has_frontmatter: boolean; tracked_by: string | null; checkout_of: string | null;
}
interface Listing { repo: string | null; wiki: string | null; checkout: string | null; folders: string[]; skills: ReviewSkill[]; repos: string[] }
interface SkillFile { path: string; text: string; sha256: string; writable: boolean }

const REPO_KEY = "waw.reviews.skillsRepo";

export function ReviewSkills() {
  const [repo, setRepo] = useState<string>(() => localStorage.getItem(REPO_KEY) ?? "");
  const [l, setL] = useState<Listing | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [all, setAll] = useState(false);
  const [editing, setEditing] = useState<ReviewSkill | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const load = useCallback(async (r: string) => {
    setErr(null);
    try {
      const x = await get<Listing>(`/github/review-skills${r ? `?repo=${encodeURIComponent(r)}` : ""}`);
      setL(x);
      if (!r && x.repo) setRepo(x.repo);
    } catch (e) {
      setErr((e as ApiError).message);
    }
  }, []);
  useEffect(() => { void load(repo); }, [load, repo]);
  useEffect(() => { if (repo) localStorage.setItem(REPO_KEY, repo); }, [repo]);
  if (!l) return <div className="muted small">{err ?? "Loading…"}</div>;
  const shown = l.skills.filter((s) => all || s.review);
  return (
    <div className="learning" data-testid="review-skills">
      <div className="muted small">
        Skills an agent review gets. Its prompt lists each one's name and description, and the agent reads the matching
        SKILL.md. A saved edit is used from the next review on; reviews already running keep what they read.
      </div>
      {err && <div className="error-text small" role="alert">{err}</div>}
      <div className="row small learning-model">
        Reviews of
        <select value={l.repo ?? ""} onChange={(e) => setRepo(e.target.value)} data-testid="review-skills-repo">
          {!l.repos.length && <option value="">(no repository with a wiki or local checkout)</option>}
          {l.repos.map((r) => <option key={r} value={r}>{r}</option>)}
        </select>
        <button className="btn small" onClick={() => void load(repo)} title="Look for skills again (new or removed SKILL.md files)" data-testid="review-skills-refresh">↻</button>
      </div>
      <div className="muted small" title={l.folders.join("\n")}>
        Looked for in {l.folders.length} folder(s): {l.checkout ? <>the local checkout <code>{l.checkout}</code>, </> : null}
        {l.wiki ? "the repository's wiki" : "this window's wiki (the repository has none yet)"} and the other open or connected folders.
        A name defined twice: the first one wins.
      </div>
      <label className="small"><input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} data-testid="review-skills-all" /> Show all {l.skills.length} skills (the agent may pick any of them; those about reviews are shown by default)</label>
      {note && <div className="small" data-testid="review-skills-note">{note}</div>}
      {shown.length === 0 && <div className="muted small">{l.skills.length ? "No skill mentions reviews in its name or description (tick “Show all”)." : "No skills in these folders (.claude/skills, .opencode/skills, .agents/skills, .codex/skills)."}</div>}
      {shown.map((s) => (
        <div key={s.path} className="learning-lesson" data-testid="review-skill" data-name={s.name}>
          <div className="row nowrap small">
            <b>{s.name}</b>
            {s.review && <span className="chip">review</span>}
            <span className="spacer" />
            <button className="btn small" onClick={() => { setNote(null); setEditing(s); }} data-testid="review-skill-edit">Edit</button>
          </div>
          <div className="small muted">{s.description || "(no description: the agent cannot tell when to use it)"}</div>
          <div className="small muted mono" title={s.path}>{s.path}</div>
          {!s.has_frontmatter && <div className="warn small">No <code>---</code> header: the agent sees it as “{s.name}” without a description.</div>}
          {s.checkout_of && <div className="warn small" data-testid="review-skill-tracked">Part of the {s.checkout_of} checkout (tracked by git): an edit makes the checkout dirty, so the app stops fast-forwarding it before reviews, and a pull may conflict. A copy under the wiki's <code>.claude/skills</code> with another name avoids that.</div>}
          {!s.checkout_of && s.tracked_by && <div className="muted small">Tracked by git in <code>{s.tracked_by}</code>: commit the edit there to keep it.</div>}
        </div>
      ))}
      {editing && <SkillEditor skill={editing} repo={l.repo} onClose={() => setEditing(null)} onSaved={(x) => {
        setL(x);
        const now = x.skills.find((s) => s.path === editing.path);
        setNote(now ? `Saved. The next review gets “${now.name}”: ${now.description || "(no description)"}`
          : "Saved, but another skill with the same name comes first, so reviews do not get this one: give it its own name.");
      }} />}
    </div>
  );
}

function SkillEditor({ skill, repo, onClose, onSaved }: { skill: ReviewSkill; repo: string | null; onClose: () => void; onSaved: (l: Listing) => void }) {
  const [f, setF] = useState<SkillFile | null>(null);
  const [text, setText] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const dirty = f !== null && text !== f.text;
  const read = useCallback(async () => {
    setErr(null);
    try {
      const x = await get<SkillFile>(`/github/review-skills/file?path=${encodeURIComponent(skill.path)}`);
      setF(x);
      setText(x.text);
    } catch (e) {
      setErr((e as ApiError).message);
    }
  }, [skill.path]);
  useEffect(() => { void read(); }, [read]);
  const save = async () => {
    if (!f || !dirty || busy) return;
    setBusy(true);
    setErr(null);
    try {
      const r = await put<Listing & { sha256: string }>("/github/review-skills/file", { path: f.path, text, base_sha256: f.sha256, repo });
      setF({ ...f, text, sha256: r.sha256 });
      onSaved(r);
    } catch (e) {
      setErr((e as ApiError).code === "FILE_CHANGED" ? "The file changed on disk since it was opened. Reload it (your edits here are lost) or copy them first." : (e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  const reload = () => { if (!dirty || window.confirm("Discard your unsaved edits and read the file from disk again?")) void read(); };
  const close = () => { if (!dirty || window.confirm("Close without saving your edits?")) onClose(); };
  return (
    <div className="modal-backdrop" onClick={close}>
      <div className="modal wide" role="dialog" aria-label={`Edit skill ${skill.name}`} onClick={(e) => e.stopPropagation()} data-testid="review-skill-editor"
        onKeyDown={(e) => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); void save(); } if (e.key === "Escape") close(); }}>
        <div className="modal-head"><span>Skill <b>{skill.name}</b>{dirty ? " •" : ""}</span><button className="icon-btn small" onClick={close} aria-label="Close">×</button></div>
        <div className="muted small mono">{skill.path}</div>
        {skill.checkout_of && <div className="warn small">This file belongs to the {skill.checkout_of} checkout: saving makes it dirty.</div>}
        {err && <div className="error-text small" role="alert" data-testid="review-skill-error">{err}</div>}
        {f && !f.writable && <div className="warn small">The file is read-only for the server.</div>}
        {f === null ? <div className="muted small">Loading…</div> : (
          <textarea className="mono skill-text" rows={24} spellCheck={false} value={text} onChange={(e) => setText(e.target.value)} data-testid="review-skill-text" />
        )}
        <div className="row small">
          <span className="muted">Keep the <code>---</code> header with <code>name:</code> and <code>description:</code>: reviews choose skills by them.</span>
          <span className="spacer" />
          <button className="btn small" onClick={reload} title="Read the file from disk again (after editing it elsewhere)" data-testid="review-skill-reload">Reload from disk</button>
          <button className="btn small primary" disabled={!dirty || busy || !f?.writable} onClick={() => void save()} title="Save (Ctrl+S); the next agent review uses it" data-testid="review-skill-save">{busy ? "Saving…" : "Save"}</button>
        </div>
      </div>
    </div>
  );
}
