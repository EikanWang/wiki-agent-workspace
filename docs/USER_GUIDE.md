# User guide

How to use every part of Wiki Agent Workspace day to day. Installation is in [DEPLOYMENT.md](DEPLOYMENT.md), settings
in [CONFIGURATION.md](CONFIGURATION.md), troubleshooting in [OPERATIONS.md](OPERATIONS.md).

Contents: [Window layout](#window-layout) · [Folders (multi-root)](#folders-multi-root) ·
[Reading](#reading-documents) · [Asking about a passage](#asking-about-a-passage) ·
[Discussions panel](#discussions-panel) · [Agent sessions and models](#agent-sessions-and-models) ·
[Editing a file yourself](#editing-a-file-yourself) · [Editing through the agent](#editing-through-the-agent) · [Locks](#locks) · [Diff review](#diff-review) ·
[Reviews](#reviews-stash-then-submit) · [Committing, pushing, pull requests](#committing-pushing-pull-requests) ·
[Recent work](#recent-work) · [Terminal](#terminal) · [Keyboard](#keyboard) · [Mobile](#mobile)

## Window layout

```
┌ top bar: Files toggle · folder switcher · document session · Lock · Document | Terminal | Diff · Recent · Aa ┐
├ file tree (all folders) ┬ document / diff / terminal ───────────────────────┬ discussions panel ┤
│ ＋ add folder  ↻ refresh │ tabs: guide.md × │ README.md × │ notes.md ×       │ Individual | Flat  │
│                         │ header: path · format · branch · git status ·      │                    │
│ ▾ wiki-a  (main)  ±     │ Read | Discuss · Ask about selection              │                    │
│   ▸ docs                │ outline (foldable) │ rendered content              │                    │
│ ▾ wiki-b  (dev)   ± ✕   │                                                    │                    │
└─────────────────────────┴────────────────────────────────────────────────────┴────────────────────┘
```

- **Files** (top left) folds/unfolds the file tree; drag its edge to resize. The discussion panel folds with `›` and
  is resizable too; the outline folds with `▾ Outline`. The document always grows into the freed space.
- **Aa** — document font (sans/serif/mono), size, optional content-width cap (default: fill), terminal font size.
- All layout choices are remembered per browser.

## Folders (multi-root)

One window can hold several wiki folders side by side, like VS Code's "Add folder to workspace". Each folder is an
independent root: its own documents, discussions, agent sessions, locks, reviews, *Viewed* marks and Git
state. Nothing is shared between folders except your login, terminals and preferences.

- **Add a folder**: `＋` in the file-tree header opens a folder picker that browses the **server's** file system
  (where your wikis and agents live). Pick a folder (Git repositories are marked), optionally give it a display name,
  **Add folder**. It persists across restarts.
- **Switch folder**: click any file in another folder's tree, use the folder selector in the top bar, or open an
  entry from *Recent*. The header, Diff, Commit, Review and the discussion panel always follow the active folder; the
  last document of each folder is remembered.
- **Remove a folder** from the window: hover its header row in the tree and click `✕`. Its data is kept — add the
  same folder again and everything is back. Folders defined in `config.toml` can only be removed there.
- A folder that disappeared from disk is shown as *missing*; restore it or remove it.
- Folders can also be preconfigured: see `[[workspaces]]` in [CONFIGURATION.md](CONFIGURATION.md).

## Reading documents

- **Tabs**: every document you open gets a tab above the reader (like the terminal tabs); opening one that is
  already open switches to it, and a new tab opens right after the current one. Click a tab to switch (also across
  folders); close it with `×` or a middle-click — closing the active tab opens its neighbour. Drag tabs to reorder.
  Tabs are remembered in this browser. Two files with the same name show their parent folder (or the folder name
  when the same path is open in two wikis). Switching away from unsaved edits asks first.
- The tree lists every file with a kind icon (folder, Markdown, image, PDF, notebook, code, script, data, text,
  archive, binary; a small arrow marks symlinks, a broken-link icon marks dangling ones). Git status badges show
  `untracked` / `modified` / `clean`.
- The reader opens **Markdown**, **any text or source file** (C/C++, Python, YAML, XML, JSON, shell…, shown as
  numbered code), **Word .docx** (converted to HTML), **PowerPoint .pptx** (one card per slide: title, text with
  bullet levels, tables, pictures and speaker notes; the outline lists the slides) and **PDF** (browser viewer).
  Selecting text, discussions, review marks and diff comments work on Word and PowerPoint text like on Markdown.
  Older binary formats (`.doc`, `.ppt`) and other files are listed for orientation only.
- Links to sections (`#heading`, `other.md#heading`) scroll inside the reader; heading ids follow GitHub's rules.
- Your reading position is saved when you stop scrolling for a second and restored next time; nothing is marked
  reviewed by scrolling.
- When a file changes on disk (agent, terminal, Git), a banner offers **Refresh**; your position is kept.
- **↻** (file tree, diff view) re-reads from disk. Its icon spins until the refresh is done, at least one full turn.
  Normally not needed: the file watcher keeps the tree up to date. If a folder cannot be listed (server
  unreachable), the tree keeps the earlier listing with a **Retry** link.
- **Read | Discuss** in the document header: *Read* is a plain viewer — selecting text only shows a small copy icon;
  *Discuss* opens the question box on selection. **Ask about selection** works in both modes.

## Asking about a passage

1. In *Discuss* mode select a few words, a sentence or several lines. After a short pause the question box appears
   next to the selection (the copy icon on the quote copies it; Ctrl/⌘+C works too).
2. Type the question, pick a session (default: the document's session) and press **Enter**.
3. A discussion is created for exactly that passage; the answer streams into the discussions panel. Highlights mark
   discussed passages; click one to reopen its discussion.

Anchors survive edits: if the passage moves, the discussion follows; if the text changed or became ambiguous, the
discussion is flagged *orphaned* / *ambiguous* and keeps the original quote — use **Reanchor manually** to attach it
to the new text. **Mark reviewed** marks a passage reviewed; it turns *needs review* when that text changes.

## Discussions panel

- **Individual** — one discussion at a time: quoted passage, session (and its model), messages, **View diff**
  (changes since the discussion started), reply box.
- **Flat** — all discussions of the document as one chronological stream, grouped by passage; the reply box
  continues the selected discussion (default: newest). Useful when your questions are related.
- Failed answers keep your question; **Retry** with the same or another session. **Cancel** stops a running turn.

## Agent sessions and models

- Each document has a default session (top bar). The first question creates one with the default provider;
  **+ New session** makes a fresh one for future discussions; the session dialog can also **link an existing
  provider session** (e.g. the terminal conversation that wrote the page).
- **Model**: every session has a model and reasoning effort for its future turns — choose them in the session
  dialog, in the discussion's session box, or in the flat composer. OpenCode lists its real models; for Codex the
  configured default, profiles and common ids are offered, or type any id. Past answers keep their model.
- Jobs on the same session run one after another ("waiting for session"); different sessions run in parallel.

## Editing a file yourself

**✎ Edit** in the document header (Markdown and any text/source file; Word, PowerPoint and PDF are read-only) opens an editor
in place of the rendered view.

- **Ctrl/⌘+S** or **Save** writes the file; **Done** (or Esc) goes back to reading. *wrap* toggles soft wrapping
  (off shows line numbers); for Markdown, *preview* shows the rendered result side by side. Tab / Shift+Tab indent and
  outdent (the file's own indentation style is kept), and the file's line endings (LF/CRLF) are preserved.
- Saving is safe against concurrent changes: if the agent, your terminal or Git changed the file after you started,
  the save is refused and you choose **Overwrite with mine**, **Discard mine, reload**, or **Copy my text**. A banner
  warns as soon as such a change happens while you edit.
- Every save is recorded as a version, so **View diff** and the Diff view show it, discussions re-anchor to the new
  text and reviewed passages flag *needs review* if their text changed. Nothing is committed.
- Locked files cannot be edited. Leaving with unsaved changes (another document, another folder, closing the tab)
  asks first.

## Editing through the agent

- Ask explicitly ("please change …") in a discussion; the agent edits with its own configured permissions. The
  banner, **View diff** and the version history show exactly what changed. Nothing is committed automatically.
- **Preview change** (top bar) asks the agent to work in an isolated copy and shows a diff first; **Apply** writes it
  only if the file is still unchanged.

## Locks

`🔒` next to a file/folder in the tree, or **Lock** in the top bar (also for a whole folder root). A lock makes the
path read-only *for the app and app-driven agents*: preview Apply, relink, history deletion and commits are refused;
the agent is told the path is locked (Codex runs read-only); if a change happens anyway during a turn it is reported
as a *lock violation* for you to review. Your terminal is never restricted.

## Diff review

Open with **Diff** in the top bar (the current document), `±` on a changed file/folder, or `±` on a folder root (all
uncommitted changes of that folder).

- Choose **base** (HEAD, a branch, a recent commit) and **target** (working tree or a commit); **Unified** or
  **Side by side** layout; optionally *Markdown only*. Untracked drafts appear as additions. Word and PowerPoint
  files are compared by their text (slide titles, bullets, table cells, notes), never as raw bytes.
- **Viewed** checkbox per file (collapses it, counted in the header); it clears itself when the file changes again.
- Select lines to comment: click a line, shift-click or drag for a range. Then either
  - **Add to review** (Enter) — stash for the review (next section), or
  - **Ask now** — start a discussion immediately; the answer streams inline under the line, you can reply inline,
    and **Open discussion** jumps to the full thread.

## Reviews (stash, then submit)

Like a GitHub pull-request review:

1. **Add to review** stashes comments; dashed markers show them in the diff and **Review (n)** in the header counts
   them. Edit or delete them in the review tray; add an overall note.
2. **Submit review** sends all comments to the agent in **one** turn. For each comment it decides **address** (makes
   the change), **reply** (answers or disagrees, no edit) or **skip** (with a reason). Locked files are never edited.
3. Each comment becomes a discussion carrying the verdict; the diff shows *addressed / replied / skipped* badges and
   the tray's *Last review* summarises the outcome.

## Committing, pushing, pull requests

**Commit…** in the Diff header:

1. Tick the changed files to include (all are pre-selected; locked files are excluded).
2. **Draft with agent** writes a commit message from the actual diff of those files; edit it as you like.
3. **Commit** — only the ticked files, with your Git identity. Nothing is pushed.
4. **Push** — to `origin` (sets the upstream on the first push). The app never force-pushes; if the remote moved on,
   pull/rebase in the terminal first.
5. **Create pull request** — for GitHub remotes with the `gh` CLI logged in, from the current branch (create a feature
   branch in the terminal first if you are on the default branch).

## Recent work

**Recent** lists documents you worked on in **all folders**, newest first (folder name shown when you have several):
where you were, discussions and the last message. Opening an entry switches to that folder and restores the
document, position and open discussion. Filter by text; **hide** entries (history kept, *show hidden* to see them),
or **delete** a document's app history (discussions, versions, position — the file itself is untouched).

## Terminal

Real shells in tmux on the server, in as many tabs as you like; new terminals start in the active folder. Closing the
browser only detaches; reattach later and everything is still running, even across server restarts. **tmux
history** shows the server-side scrollback. Tabs whose shell exited show *(exited)*: dismiss them with `×` or
**Clear exited**; closing a live terminal asks first. **Close terminal** ends the tmux session: its shell and
programs get SIGHUP (as when an SSH connection closes) and nothing is left running, except programs started to
outlive the shell (`nohup`, `setsid`). New tabs are named with the lowest free number (close *Terminal 2* and the
next new tab is *Terminal 2* again); double-click a tab to rename it.

**Keys the browser keeps for itself.** In a normal browser tab, Chrome/Edge/Firefox never pass a few shortcuts to the
page — Ctrl+T, Ctrl+W, Ctrl+N, Ctrl+Tab and similar open/close tabs and windows instead. Three ways to send them:

- **⛶ Full screen** (terminal toolbar): in Chromium-based browsers (Chrome, Edge) full screen also *captures the
  keyboard*, so Ctrl+T, Ctrl+W, Ctrl+N… go straight to the terminal. Press and **hold** Esc to leave full screen.
  Firefox and Safari don't support keyboard capture.
- **Keys ▾**: one-click Ctrl+T/W/N/Q/R/L/J/K, Ctrl+C/D/Z, Ctrl+B (tmux prefix), Esc, Tab, Shift+Tab, arrows,
  PgUp/PgDn, Home/End — also handy on touch keyboards.
- **Ctrl** / **Alt** (sticky): click, then type any key — it is sent with that modifier once (Ctrl, T = Ctrl+T;
  Alt, x = Alt+x).

**Copying text out of the terminal.** A short *Copied N characters* note confirms every copy.

- **Drag** with the mouse (or double-click a word, triple-click a line): tmux selects the text and copies it to
  your clipboard when you release the button. Drag past the top/bottom edge to scroll while selecting.
- **Shift+drag** (Option+drag on macOS): a plain browser selection, independent of tmux — useful inside full-screen
  programs. It is copied on release, and **Ctrl+Shift+C** (⌘C), **Ctrl+C while text is selected**, or the **Copy**
  button copy it again. Without a selection Ctrl+C is the usual interrupt.
- Programs that set the clipboard themselves (vim/neovim with OSC 52, OpenCode, `printf '\e]52;c;…'`) also reach
  your clipboard. They can write it, never read it.
- **tmux history** → **Copy all** copies the whole server-side scrollback; you can also select part of it there.
- On plain `http://` (not localhost) browsers only allow clipboard writes right after a click or key press. If a
  copy is refused, the note offers **Copy now**; the toolbar **Copy** button also re-copies the last terminal copy.

## Keyboard

| Where | Key | Action |
| --- | --- | --- |
| Any question / reply / comment box | Enter | Send (diff comment box: *Add to review*) |
| | Ctrl/⌘+Enter, Shift+Enter | New line / paragraph |
| | Esc | Close the box (keeps nothing sent) |
| Selection box | Ctrl/⌘+C | Copy the selection |
| Editor | Ctrl/⌘+S · Esc · Tab / Shift+Tab | Save · back to reading · indent / outdent |
| Terminal | Ctrl+T, Ctrl+W, Ctrl+N… | Reserved by the browser: use ⛶ Full screen (Chrome/Edge), **Keys ▾** or sticky **Ctrl** |
| | drag · Shift+drag | Copy via tmux (on release) · browser selection (copied on release) |
| | Ctrl+Shift+C (⌘C) · Ctrl+C with a selection | Copy the selection (Ctrl+C without one interrupts) |
| | Ctrl+V / Ctrl+Shift+V | Paste |
| Diff | click / shift-click / drag | Select one line / a range |

IME composition (e.g. Chinese input) never sends on Enter.

## Mobile

The layout switches to bottom tabs (Files · Document · Discussion · Terminal). Select text natively and use the
**Ask** / copy buttons in the bar that appears, or **Ask about selection** in the header.
