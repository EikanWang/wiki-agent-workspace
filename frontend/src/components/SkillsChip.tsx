import { useEffect, useState } from "react";
import { getIn } from "../api";

interface Skill { name: string; description: string; path: string; folder: string }

/**
 * The skills an agent turn of this folder gets: those of the current folder and of every folder and project
 * connected to the window (and, for a PR tab, the PR's connected local checkout). Click to list them.
 */
export default function SkillsChip({ workspace, docId }: { workspace: string | null; docId?: string | null }) {
  const [skills, setSkills] = useState<Skill[] | null>(null);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!workspace) return;
    let alive = true;
    getIn<{ skills: Skill[] }>(workspace, `/agent-skills${docId ? `?doc_id=${encodeURIComponent(docId)}` : ""}`)
      .then((r) => { if (alive) setSkills(r.skills); })
      .catch(() => { if (alive) setSkills(null); });
    return () => { alive = false; };
  }, [workspace, docId]);
  if (!skills) return null;
  return (
    <span className="skills-chip-wrap">
      <button className="chip chip-btn" onClick={() => setOpen(!open)} title="Skills the agent gets from this folder and every connected folder and project" data-testid="skills-chip">
        {skills.length} skill{skills.length === 1 ? "" : "s"}
      </button>
      {open && (
        <div className="skills-pop" data-testid="skills-list" onClick={(e) => e.stopPropagation()}>
          <div className="row nowrap"><b>Skills loaded for the agent</b><span className="spacer" /><button className="icon-btn small" onClick={() => setOpen(false)}>×</button></div>
          {skills.length === 0 && <div className="muted small">None in the connected folders (<code>.opencode/skills</code>, <code>.claude/skills</code>, <code>.agents/skills</code>, <code>.codex/skills</code>).</div>}
          {skills.map((s) => (
            <div key={s.path} className="small" title={s.path}><b>{s.name}</b> <span className="muted">— {s.description || "no description"}</span> <span className="muted mono">{s.folder.split("/").pop()}</span></div>
          ))}
        </div>
      )}
    </span>
  );
}
