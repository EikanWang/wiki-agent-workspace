# User guide

How to use every part of Wiki Agent Workspace day to day. Installation is in [DEPLOYMENT.md](DEPLOYMENT.md) — in
short, `scripts/install.sh` sets everything up and prints the address and the access token, and
`scripts/doctor.sh` tells you what is missing for a feature — settings in [CONFIGURATION.md](CONFIGURATION.md),
troubleshooting in [OPERATIONS.md](OPERATIONS.md).

Contents: [Window layout](#window-layout) · [Folders (multi-root)](#folders-multi-root) · [Local files](#local-files) ·
[Reviews (GitHub)](#reviews-github) · [PR Development](#pr-development) · [Agents (monitor)](#agents-monitor) ·
[Reading](#reading-documents) · [Asking about a passage](#asking-about-a-passage) ·
[Discussions panel](#discussions-panel) · [Agent sessions and models](#agent-sessions-and-models) ·
[Editing a file yourself](#editing-a-file-yourself) · [Editing through the agent](#editing-through-the-agent) · [Locks](#locks) · [Diff review](#diff-review) ·
[Reviews](#reviews-stash-then-submit) · [Committing, pushing, pull requests](#committing-pushing-pull-requests) ·
[Recent work](#recent-work) · [Terminal](#terminal) · [Keyboard](#keyboard) · [Mobile](#mobile)

## Signing in

**Continue with GitHub** works out of the box when the server's `gh` CLI is logged in (its account is the
server's owner): the first click takes you to GitHub's **Create GitHub App** page (one click, filled in), then to
GitHub's sign-in/authorize page, then into the app. Only the owner's GitHub account can complete that first setup
and sign in. (Signed in with the token, the account menu has the same **Set up “Continue with GitHub”…**.)

Open the app and click **Continue with Google** or **Continue with GitHub** (shown when the server has them set
up, see CONFIGURATION.md `[auth.google]` / `[auth.github]`), or enter the access token. Only the accounts the owner
listed are let in (Google: verified email addresses). A login lasts 30 days; **Sign out** (top right; it shows your
GitHub name or email) ends it. Pushes and pull requests always use the server's own
Git/GitHub credentials, whoever is signed in.

## Window layout

```
┌ top bar: Files toggle · ▣ workspace ▾ · folder switcher · document session · Lock · Document | Terminal | Diff · Recent · Aa ┐
├ file tree (all folders) ┬ document / diff / terminal ───────────────────────┬ discussions panel ┤
│ ＋ add folder  ↻ refresh │ tabs: guide.md × │ README.md × │ notes.md ×       │ Individual | Flat  │
│                         │ header: path · format · branch · git status ·      │                    │
│ ▾ wiki-a  (main)  ±     │ Read | Discuss · Ask about selection              │                    │
│   ▸ docs                │ outline (foldable) │ rendered content              │                    │
│ ▾ wiki-b  (dev)   ± ✕   │                                                    │                    │
└─────────────────────────┴────────────────────────────────────────────────────┴────────────────────┘
```

- The **activity bar** at the left edge switches the sidebar: **Files** (the file tree), **Reviews** (GitHub, when
  signed in as the server's owner), **PR Development**, **Agents** and **Terminal**. **Terminal** is the wiki's
  Terminal view on its own: the server's terminals are listed in the sidebar (**+** opens one in your home folder,
  **+ New terminal in a folder…** one where you choose; × closes one) and the page shows them as tabs, exactly like
  the wiki's Terminal (copy, keys, full screen, tmux history); the wiki's Document / Terminal / Chats / Diff tabs are
  hidden there. Clicking the current icon again (or **«** at the bottom
  of the activity bar) folds the list next to it — folders, projects, pull requests… — and leaves only the activity
  bar; the current icon again (or **»**) brings it back, another icon opens its own list. **Sidebar** (top left)
  hides both.
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
  **Add folder**. It persists across restarts. **＋ New folder** in the picker creates a folder in the one shown
  (with **git init** ticked it becomes a Git repository, with a `.gitignore` for the app's temporary files) — handy
  for starting a new wiki. **Clone from URL** (tab in the same dialog) clones a Git repository —
  `https://github.com/owner/repo.git` or `git@github.com:owner/repo.git` — into the folder shown (name editable;
  *latest commit only* for big repositories), with progress, using the server's Git credentials, then opens it.
- **Switch folder**: click any file in another folder's tree, use the folder selector in the top bar, or open an
  entry from *Recent*. The header, Diff, Commit, Review and the discussion panel always follow the active folder; the
  last document of each folder is remembered.
- **Browsing the server's folders** (Add folder, New workspace, **Browse…** for reference folders and a
  repository's local checkout): quick places (*~ home*, `/`, the folders open in this window and where they
  live), a path box (Enter opens it), clickable breadcrumbs, a filter for long folder lists, *hidden* to show dot
  folders, and a *git* badge on Git checkouts; click a folder to enter it, then choose the one shown.
- **Close a folder**: hover its header row in the tree and click `✕`, then **Close folder** in the confirmation
  (Cancel has the focus, so Enter or a stray click does nothing; Esc cancels). It does not show again — also after a
  restart, also for folders defined in `config.toml`. Its data is kept: add the same folder again with `＋` and
  everything is back. The window needs at least one folder.
- A folder that disappeared from disk is shown as *missing*; restore it or remove it.
- Folders can also be preconfigured: see `[[workspaces]]` in [CONFIGURATION.md](CONFIGURATION.md).

### Workspaces (named sets of folders)

Like VS Code windows: a **workspace** is a named set of folders with its own **document tabs** and **terminals**.
The **▣ name ▾** menu in the top bar lists them:

- **＋ New workspace…** asks for a name and its first folder (browse, or create one with **＋ New folder**);
  *include this window's folders* starts from the current folders instead. The window switches to it.
- Click a workspace to **switch**: the file tree, tabs and terminals change to that workspace's. Terminals of the
  other workspaces keep running in tmux and are back when you switch back.
- **✎** renames, **🗑** deletes a workspace (not the current one or *Default*). Deleting never touches folders or
  their data; its terminal tabs move to *Default*.
- Folders added (`＋`) or removed (`✕`) change the current workspace only.
- *Default* holds the folders you had before workspaces existed and always contains the folders of `config.toml`;
  in other workspaces those can be removed like any other folder.
- A folder's discussions, sessions, history and Recent entries belong to the folder, so they are the same in every
  workspace that shows it.
- The current workspace is the server's: switching in one browser switches your other open browsers too (they
  reload with a notice).

### Chats: talk with a project directly

**Chats** (top bar, next to Terminal; or the **chat** icon on a project's row in the file tree) opens a page of its
own: a bar with the **Project** (any folder of the window) and the **Chat** menu (*＋ New chat* or one of the
project's earlier chats), and the conversation below. To start, pick the project and just type — the first
message starts a chat (titled after it). It is an open conversation with the agent
about the whole project, not about one document: the agent searches and reads the files to answer and cites the
ones it used. Reference folders (**⧉**) and locks apply as for documents. When the conversation is worth
keeping, under **Turn into wiki content**:
- **Distill into a new page** — give the page path (`folder/page.md`, suggested from the title) and an optional
  focus; the agent writes a standalone article from the conversation, linking the pages it came from, and touches
  no other file;
- **Update the pages it came from** — when the content mostly came from existing pages, the agent folds the
  conclusions and corrections back into those pages in place (optional note: which pages, what to leave alone).

Both are ordinary changes in the project: **Review changes** opens the Diff view to keep, edit or revert them
before committing. **Archive** (confirmed) removes a chat from the list; its conversation is kept. Chats are
stored by the app, never as files in the project; opening one from *Recent* goes to the Chats page, which also
comes back after a reload. Change summaries of a GitHub repository (⎇ in a PR tab) open here too.

### Creating files and folders

Hover a folder (or a root folder) in the tree: **＋🗎** creates a file, **＋🗀** a folder, with a name box right
in the tree (Enter creates, Esc cancels). A path such as `notes/2026/today.md` creates the folders on the way. A new
Markdown or text file opens in the editor (a Markdown file starts with a heading from its name). Existing names,
locked folders and paths through links that leave the folder are refused, with the reason under the name box.

## Local files

**Local files** (the folder icon of the activity bar) browses and edits any folder or file on the server, like a file
manager — no wiki features (discussions, locks, versions), just the files. **Open a folder** (or type a path) adds
it to the list; open folders stay there for every browser; ✕ closes one (nothing is deleted). Hover a folder for
**New file** / **New folder**, a file or folder for **Rename / move** and **Delete**. Files open in tabs in VS Code's
editor (Monaco): syntax highlighting, find / replace, outline (`Ctrl+Shift+O`), `Ctrl/⌘+S` saves — a file that
changed on disk in the meantime is never overwritten silently, and clean tabs follow changes on disk. Markdown has
a preview, images are shown, other binary files can be downloaded.

## Reviews (GitHub)

When you sign in with **Continue with GitHub** as the account the server's `gh` CLI is logged in as (the server's
owner), the sidebar gets an **activity bar**: **Files** (the file tree) and **Reviews** (a pull-request icon with
the number of PRs waiting for you, drafts not counted). Token logins and other GitHub accounts never see it — the
queue can include private repositories.

**Reviews** lists the open pull requests that request your review, across every repository your GitHub account
can see, grouped by repository (most PRs first). Inside each repository the PRs are in three sections by where you
stand (their order and names are yours to change, ⚙ → Priority → Categories); inside each section the **most urgent
priority level first**, then your sort:
1. **Not reviewed yet** (*top*) — you have not reviewed or commented on it;
2. **New activity since your comments** (*high*) — since your latest review, review comment, reply or comment,
   someone else reviewed, replied or commented, new commits were pushed, or your review was requested again (bots and
   GitHub Actions do not count); the row says what (↻ new comments, new commits…);
3. **Commented, nothing new** (*medium*) — ✓ commented, and nothing happened since.

**Priority levels.** Every row ends with its level badge (*P0 Urgent*, *P1 High*, *P2 Normal*, *P3 Low* by default; a
pale badge is the default level, ✎ set by hand, 🤖 decided by the agent). Hover it for why; click it to **set the
level by hand**, go **back to rules / agent**, or **ask the agent again**. A PR's level is:
1. the one you set by hand; otherwise
2. the **more urgent** of the **hard rules'** level and the **agent's** level (hard rules *or* agent); otherwise
3. the default level.

**⚙ → Priority** edits it all (Save to apply; **Reset to defaults** undoes everything):
- **Levels** — add, remove, rename, recolour, reorder (most urgent first), pick the default; each level has any
  number of **descriptions in plain language** ("Fixes a crash or memory error in the XPU runtime", "Docs-only
  changes"…) that the agent judges PRs against.
- **Hard rules** — *if* conditions (all must match) *then* a level: review asked by, asked by a PyTorch Meta
  maintainer, author, repository, label, title contains, size, days since opened / last updated, CI (failing,
  passing, running, none), draft, review decision, your activity (todo / news / quiet). When several match, the
  most urgent wins. (The older "PyTorch Meta maintainers" / priority people became such rules, ★ / un-★ became
  levels set by hand.)
- **The agent** (off by default) — when on, it decides the level of new and changed PRs in the background
  ("🤖 deciding levels of N PRs…" above the list). It reads the PR's details, description, changed files and diff:
  a small diff (≤ 12k characters) in full; a larger one is first **summarized file by file** (one turn per ~25k
  characters, a bigger file in parts, the 60 biggest files; lock / vendored / generated / binary files only named),
  then it decides from the summaries. Summaries are kept until a file's change changes; editing your descriptions
  re-runs only the decisions. Pick the agent and model for the **summaries** and for the **decision** separately
  (e.g. a cheaper model for summaries). Its turns only read (Codex: read-only sandbox) and its sessions are hidden
  from the session pickers. **Re-classify all** asks it again for every PR. In a PR tab, **🤖 Agent's view of the
  changes** shows its reason and the summary of each file.
- **Categories** — the order and names of the three sections.
The level filter next to the sort (*only P0*, *P0–P1*…) shows only the more urgent PRs.
**Opening a PR quickly (⚙ → Cache).** PR Review keeps a local copy of each pull request — details, files and diffs,
viewed marks, checks, review threads, conversation, contributors, and the files you expand — in a folder
(`<state_dir>/github-cache` by default; private: mode 0700/0600). A PR's tab opens at once from it, shows **cached ·
checking…** next to ↻, and GitHub is checked right away: **up to date**, or the tab **updates itself** (**updated**).
While you are writing a comment it does not reload: a note offers **Reload** (your text stays). Everything else works
exactly as before — ↻, auto-refresh, the reloads after your actions, your drafts and the agent's suggestions always
come from GitHub / the app; your actions mark the cached copy stale. With **prefetch** on (default) the PRs of your
review list that are new or changed are fetched in the background after each refresh of the list (2 at a time,
pausing when fewer than 1000 GitHub requests remain), so most open from the cache. ⚙ → Cache shows its size, moves it
to another folder (**Move here**; not inside a wiki), sets the size limit (2048 MB by default: the PRs opened longest
ago go first; closed and merged ones after a week), turns prefetch off and **clears** it.

**auto-refresh** (in the line above the list) sets how often the list and its badge refresh from GitHub on their
own: off (only ↻), 1, 2, 5 (default), 10, 15, 30 minutes, or 1, 2, 4, 8 hours; ↻ refreshes now at any time. An open pull request has its own **auto-refresh**
next to ↻ in its toolbar (off by default) — the same setting refreshes PR Development's checks in its Pull request
tab. Refreshes run only while the page is visible (coming back to the page after the interval refreshes at once);
the choice is kept per browser.
**🤖 agent reviewed (N)** shows only the PRs the agent reviewed whose suggested comments wait for you to apply or
discard (drafts included); such rows say "🤖 N suggestions to confirm" (or "🤖 reviewing…" while it runs), and the
count follows as you decide in the PR tab. The
line above the list says how many are requested, how many are shown and how many drafts are hidden.
- each PR shows a CI dot (green passed, red failing, amber running), title, #number, author, last update,
  size (+/−), draft flag and labels; hover for details;
- **filter** by repository, title, author, requester or label; **drafts** shows or hides draft PRs; sort by
  *recently updated*, *waiting longest* or *smallest first*; click a repository to fold it; closed or merged PRs
  are dropped even while GitHub's search still lists them;
- **PRs you approved with nothing new since are left out**: no commits after the commit you approved, no review
  or comment from a person since (bots such as pytorchmergebot, pytorch-bot, facebook-github-bot, EasyCLA and
  GitHub Actions do not count, nor do your own comments), and your review was not requested again. A note says how
  many; tick **approved (n)** to list them too. Your standing verdict is the latest approve / request changes (a
  later plain comment or a draft review on github.com does not change it); a PR row shows *✓ approved* — with what
  happened since (*new commits*, *new comments*, *review requested again*) when there is news — or *changes requested*.
- Each PR shows its status next to the CI dot: **Ready** (open, ready for review) or **Draft**; search results
  can also be **Merged** or **Closed**.
- the **level badge** at the end of a row (see *Priority levels* above); the PR tab's toolbar has the same menu.
- **🔍 Search pull requests on GitHub…**: GitHub's search syntax (`xpu repo:pytorch/pytorch`, `author:…`,
  `label:…`), `owner/repo#123` or a PR link; **Open** PRs by default — merged and closed ones only with *Closed /
  merged* (or *All*). Click a result to open it as a tab. Esc closes the search.
- each PR shows who *asked* you for the review;
- **⚙ → Repositories**: **watch** a repository (`owner/name`) to also list all its open PRs (newest 40,
  in a *Watched repositories* section), stop watching it, and **show again** a
  hidden repository. Hover a repository and click **×** to **hide** it (after a confirmation; stopping watching
  a repository is confirmed too). Kept on the server (`kv_settings`);
- clicking a PR opens it as a **tab** next to your documents (remembered like them):
  - The header shows **Ready for review** or **Draft — not ready for review** next to the state, and acts on
    GitHub at once: **Ready for review** / **Convert to draft**, **Close PR** (confirmed), **Reopen**.
  - An **issue** link (`#123` that is not a PR) opens the issue: description, comments (add one), assignees, and
    **Close as completed** / **Close as not planned** (confirmed) or **Reopen**.
  - **Overview**: description, checks (failing first, linked), reviews and requested reviewers, labels;
    **Open on GitHub**; ↻ reloads. Below the description the **Conversation**: every reviewer's review
    (approved / changes requested / commented, with its summary and line comments with their code) and general
    comments, oldest first; replies are shown in the thread of the comment they answer, as on GitHub. The box at
    the end posts a general comment on GitHub. **Ask agent…** under any review, comment or line comment asks the
    agent about it — the comment (with its code and replies for a line comment) goes with your question, e.g.
    "Is the reviewer right?"; the answer is in the discussions panel. Threads in **Files** have the same button. `#123`, `owner/repo#123` and links to pull requests in the
    description and comments **open as PR tabs here** (Ctrl/⌘-click: on GitHub; an issue number shows a note
    with a link to GitHub).
  - The Overview's right column, top to bottom: **Contributors**, **Reviewers**, **Checks**, **Labels**.
  - **Reviewers**: the reviews given and the pending requests. **＋ Request a review…** — type a name or login;
    GitHub's *suggested* reviewers (who changed these files) come first, then the repository's users; ↑/↓ and
    Enter (or a click) request the review on GitHub at once. **×** on a pending request cancels it (confirmed).
    **↻** next to someone who already reviewed **re-requests** their review (as on GitHub, e.g. after you addressed
    their comments); they show *re-requested* until they review again, with **×** to cancel.
  - **GitHub logins open the account's page** (new tab), as on github.com: authors, reviewers, commenters,
    contributors, *asked by* in Reviews, and `@mentions` in descriptions and comments (apps such as
    `pytorch-bot[bot]` open their app page).
  - **Checks** → **▶ Run CI…** triggers CI on GitHub: the PR's workflow runs (newest attempt each) with **re-run
    failed**, **re-run all** or **cancel** (confirmed); the repository's **`ciflow/*` labels** as checkboxes
    (adding one makes PyTorch start those workflows, e.g. `ciflow/trunk`, `ciflow/xpu`; unticking removes the
    label); and **Start a workflow by hand** on the PR's branch (workflows with a manual trigger; not possible for
    a PR from a fork). All as the server's `gh` account.
  - **Checks** → **Ask agent about the failures…** (or **Ask** next to one failing check): the server downloads
    the failing GitHub Actions jobs' logs (up to 8; external CI is listed with its link) into its state folder and
    gives the agent the log paths and an excerpt — the output leading up to GitHub's `##[error]` marks. The agent
    reads the logs, finds the root cause of each failure and says whether it comes from the PR (pointing at the
    change), is flaky or known, or is an infrastructure problem, and suggests a fix or a re-run. Optional: what to
    focus on. Logs GitHub no longer keeps (older runs) are reported as gone.
  - **Contributors** (Overview, right): the author, commit authors and co-authors with their commit count and a
    guessed affiliation (Intel, Meta, NVIDIA, … from company, emails and organizations). Click a person for
    details: commit emails, company, organizations, location, website, bio, joined; tick **details** to also load
    each public email (one GitHub request per person).
  - **Files**: the diff of every changed file, **Unified** or **Side by side** (remembered); long files start
    folded. *n / N files viewed*, unresolved threads and **Jump to file…** at the top.
  - **All reviewers' threads** at their lines; comments on a whole file at the top of it; threads on code that
    changed since (outdated) above the file. **Reply…** and **Resolve conversation** / **Unresolve** act on GitHub
    at once; resolved threads are folded. **Ask agent…** on a thread asks the agent about it (e.g. "is the
    reviewer right?").
  - **Viewed** per file, the same as on github.com: ticking folds the file; *changed since viewed* when new
    commits touched it.
  - **Comment on file** writes a pending comment on the whole file.
  - **Click a line** (drag or shift-click for several, either side) to open a box: **Add review comment** keeps
    it *pending* here (edit/delete it; it survives reloads), **Ask agent** asks the agent about exactly those
    lines (answer in the discussions panel). **Suggest change** inserts the selected new lines as a GitHub
    *suggested change* block to edit into the replacement.
  - **⎇ Connect local repo** (next to *Agent wiki*): connect the repository to a **local git checkout** on the
    server (e.g. `pytorch/pytorch` → `/home/me/pytorch`; type the path or **Browse…** — only a Git checkout can be
    chosen). It becomes the agent's **default reference** for that
    repository's PRs: opening a PR fetches its head into the checkout as `refs/waw/pr/<n>` — never checked out, your
    working tree and branches are not changed — and the agent is told to read the PR's code there
    (`git show refs/waw/pr/<n>:<file>`, `git diff <merge-base> refs/waw/pr/<n>`) and the surrounding code, callers,
    tests and history, without modifying the checkout. **Update the local project first** (on by default) also
    fetches the PR's base branch and fast-forwards it when the checkout is on that branch with no local changes;
    otherwise the base is only fetched (`refs/waw/base/<branch>`). The button shows the state (*fetching the PR…*,
    *PR at abc1234 · main updated*); **Fetch again**, **Disconnect** (confirmed; the checkout is untouched). The PR
    is fetched from the checkout's remote that points at the GitHub repository, else from `https://github.com/<repo>.git`.
  - **Summarize key changes** (in the same ⎇ box): a **day**, **week**, **month**, **quarter** or **year** (ending
    today or on a chosen date), with an optional focus (e.g. *XPU*). The server reads the checkout's git log of the
    default branch for that window (brought up to date first when *Update* is on) — commits, subjects, files
    changed per area — and starts a **chat** in the repository's agent wiki with it; the agent groups the key
    changes by area (features and APIs, behaviour changes, performance, deprecations, notable fixes, build/CI),
    cites PRs/commits and can read any commit in the checkout. Continue the chat, or **Distill** it into a wiki page.
  - **@-mentions**: in every box that posts to GitHub (review comments, replies, file comments, the conversation,
    the review summary) typing `@` lists people — the PR's author, reviewers, requested reviewers, contributors and
    commenters first, then the repository's users matching what you type; ↑/↓ choose, Enter or Tab inserts, Esc
    closes the list.
  - **Ask agent** on a file's header asks about all of that file's changes (its diff goes with the question);
    the same box can instead add a review comment on the whole file.
  - **Agent review**: the agent reviews the whole PR (the question is prefilled; edit it to focus the review).
    Its line comments appear in **Files** at their lines as **Agent suggestions** — nothing goes to GitHub. For
    each choose **Apply** (published with your review), **Undecided** or **Discard** (not published, dropped when
    the review is submitted), edit it, or **Discuss with agent…** to ask about it first. Comments on lines outside
    the diff become comments on the file. The diff in the agent's prompt carries line numbers so its comments
    land on the right lines.
  - **Finish review (n)** (n = your pending comments plus applied suggestions): an optional summary and *Comment* / *Approve* / *Request changes*, then **Submit**
    publishes everything to GitHub as one review, as the server's `gh` account (shown in the box). If GitHub
    refuses (e.g. the PR changed and a line no longer exists), nothing is left half-published on GitHub and the
    pending comments are kept. A pending review you started on github.com is submitted together with yours.
  - The agent also reads what reviewers said (conversation and threads) when you ask about the PR.
  - Line numbers are those of the old / new file, like on GitHub (also in the wiki diff), in fixed columns that
    never wrap; code lines are 20 px like on GitHub. Side by side, a comment sits under its own half.
  - **Ask about this PR** asks about the whole pull request. Answers and follow-ups are in the discussions
    panel, like for documents, and the PR appears in *Recent*.
  - The agent works in a **wiki of your choice per repository** (asked the first time you open one of its PRs;
    change it with *Agent wiki* in the PR tab), so it can use your notes; it gets the PR's description and diff
    and is told never to comment on, approve or push to the PR. **+ folders** next to it adds **reference
    folders** for that repository's PRs (e.g. a checkout of the repository): absolute server paths the agent is
    told to read. A wiki folder has its own (**⧉** on the folder in the file tree), used for its documents.
- **↻** reloads from GitHub. The list is kept for 2 minutes and refreshed in the background, so it opens at once;
  a full fetch of ~80 PRs takes about 30 s.
- PRs in organizations that require **SAML single sign-on** are counted but hidden until you authorize the
  `gh` token for that organization (the note in the panel says how).

## PR Development

Next to reading (Files) and reviewing (Reviews), **PR Development** (the `</>` icon of the activity bar) is where you
write code with the agent — a new pull request, or changes to an existing one. Each piece of work is a
**development workspace**:

- its own **Git worktree** created by the app (`git worktree add`): your checkout is never switched, reset or
  committed to; several workspaces of one repository work side by side;
- an **editor** in the page — the built-in VS Code-like **web editor** (default) or **VS Code** (code-server) —
  with the worktree and the reference wiki (read-only);
- the **agent**, working in the worktree, with a whole **wiki folder as its reference**;
- **Build & test**, **Changes** (commit, push) and **Pull request** below the editor.
- **«** in the list of workspaces folds it (**»** at the start of the workspace's toolbar shows it again); **»** in the
  agent panel folds it (the **Agent** strip at the right edge, or **Agent** in the toolbar, shows it again).

### Creating one

**+** (or *New development workspace*) asks:

- **Runs on** — this server, or an SSH host from `[[development.hosts]]` (editor, agent, builds and tests then run
  there; the wiki is copied there read-only).
- **Work on** — *New branch* (name, optional base, the GitHub repository for the PR — detected from `origin`) or an
  *Existing pull request* (`owner/repo#123` or its URL; its head is fetched and checked out on the local branch
  `waw/pr-123`; pushes go back to the PR's branch, also on a fork when it allows edits by maintainers). **Open in
  Development** in a PR tab fills this in. Or a *Local folder*: a Git checkout or any folder, used **as it is** — no
  worktree of its own, so its files and branch change (optionally a branch to create there right away; a folder not
  in a Git repository is made one with `git init` unless you untick it). Archiving never deletes that folder.
- **My pull requests** — at the top of the sidebar, above the workspaces: your pull requests (author: the server's `gh`
  login), **grouped by repository**, each group showing its local folder (📁 path · current branch). The groups are
  the repositories you develop here plus any you add — by name (`owner/repo`, **Add**) or from a local checkout
  (**Folder…**; × removes an added one); each folds on its own; *open* or *all*; ↻ refreshes; CI dot, draft, review
  state. Click one to open
  it: its workspace if it already has one; otherwise, when the repository is connected to a local folder (a *Local
  folder* workspace, or a known checkout of it — after a confirmation), that folder **switches to the PR's branch**
  — no clone, no extra worktree; otherwise the setup dialog, prefilled. In a new-branch worktree of the same
  repository, **Check out here** does the same in that worktree. The PR's head is on its branch, compared with its
  base; **Push** updates the PR. **Close** (hover a PR in the list) or **Close pull request…** in the workspace's
  *Pull request* tab closes it on GitHub, with an optional comment; **Reopen** brings it back. The branch and your
  workspaces stay. **×** (hover) only **removes a PR from this list** — nothing changes on GitHub; "N removed from
  this list · show" in its group shows them again with **Restore** (kept per browser).
- **Removing a workspace** — × on a workspace in the list (hover), or ⋯ in its toolbar, asks how:
  **Remove from the list** archives it (the editor stops; its folder or worktree, branch and conversation stay; ⋯
  then offers to delete an app-made worktree), or **Clear everything locally** deletes everything it has on this
  machine — terminals, build / test runs and logs, the conversation with the agent, its settings and, for an
  app-made worktree, the worktree and its local branch (uncommitted and unpushed work is lost). A folder opened
  directly is never deleted. Nothing changes on GitHub either way; the agents' own sessions stay. Switching that checkout to another branch leaves the PR (the toolbar's #N goes).
- **Branches** — the branch chip in the workspace's toolbar (⎇ name ▾) creates a branch from the current one
  (uncommitted changes go along) or switches to another; in a folder opened directly, a new branch is compared with
  the one it was created from. A branch switched outside the app (a terminal) is followed. A pull request's
  workspace keeps the PR's branch.
- **Repository checkout** — your clone on that host (for a PR, the repository's connected local checkout is offered).
- **Reference wiki** — a folder of this window; the agent reads the whole folder.
- Optional: name, Python interpreter (a virtual environment's `python`), `compile_commands.json` folder, build and
  test commands. Defaults are detected: CMake (`cmake … -DCMAKE_EXPORT_COMPILE_COMMANDS=ON`, `ctest`), Make, and
  Python projects (`pytest`).

### The editor

Each workspace has one of two editors (switch with the **Web editor / VS Code** menu in its toolbar):

**Web editor** (default) — a VS Code-like workbench in the page, nothing to install in the browser:

- **Explorer** (the worktree, plus the reference wiki read-only; Git changes coloured; new file / folder, rename,
  delete, and **±** — the changes (diff) of that file or folder — on hover; ± in its header: all changes), **Search** across the worktree or the wiki (`Ctrl+Shift+F`; match case, regex, file filter),
  **Problems** (`Ctrl+Shift+M`), editor **tabs** (● = unsaved; middle-click closes; reopened after a reload),
  **Go to file** (`Ctrl+P`), the command palette (`F1`), and a status bar (branch, errors/warnings, cursor,
  language, language server).
- The editor is Monaco, VS Code's own editor: multi-cursor, find/replace, folding, minimap, sticky scroll, bracket
  pairs.
- **Language intelligence** comes from real language servers started on the workspace's host in the worktree:
  **clangd** for C/C++ (uses the workspace's `compile_commands.json` folder) and **basedpyright / pyright / pylsp**
  for Python (also found next to the workspace's Python interpreter). Completion, hover, signature help, go to
  definition / declaration / type / implementation (`F12`, also into other files), references (`Shift+F12`),
  rename (`F2`), outline (`Ctrl+Shift+O`), format, quick fixes (`Ctrl+.`) and diagnostics. The status-bar item shows
  the server's state; click it to restart. When none is installed it says what to install.
- `Ctrl+S` saves; a save is refused when the file changed on disk since you opened it (agent, command, Git), and you
  choose to overwrite or reload. Files the agent or a command changes reload by themselves in clean tabs.
- **Discuss code like a wiki passage**: select code and click **💬 Discuss** next to the selection (or
  `Ctrl+Alt+D`, or right-click → *Agent: Discuss the selection…*), type the question, choose what to do (Ask,
  Explain, Fix, Write tests, Implement) and whether to add the current file / all open files / the selected files.
  The discussion becomes a task in the agent panel showing the quoted lines (click them to jump there), and the lines
  get a marker in the editor's margin (click it to open the discussion). Follow-ups in that task stay about those
  lines.
- **Discuss files**: `Ctrl`/`⌘`-click files in the explorer to select several (**N selected · Discuss**). The agent
  panel's composer has a file context too — *No files*, *Current file*, *Open files* or *Selected files*; their
  content goes with the request while it fits (else their paths).
- Right-click → **Agent: Ask… / Explain this code / Fix problems here / Write tests for this code / Implement a
  change…** (`Ctrl+Alt+A`, `Ctrl+Alt+F`), and **Fix with the agent** in the quick fixes of any error or warning. Open
  files are saved first; the file, selection, language and problems go with the request; the answer appears in the
  agent panel.
- **AI inline completion** (ghost text, `Tab` accepts) when `[development.completion]` is configured; toggle it with
  **AI on/off** in the status bar.
- **Chat about any file**: **💬 Chat about this file** next to the path of every open file starts a discussion about
  it (also in the agent panel's *This file* view). Files that are not text open in a viewer: **images** are shown and
  the picture goes to the agent; **PDFs** are shown with PDF.js — discuss the whole PDF, a page or an area (its
  picture and text go with the question); **other files** (archives, libraries, models…) show *Discuss this file*,
  and the agent is told what kind of file it is and inspects it with its tools.
- Not in the web editor: debugging and VS Code extensions — use the VS Code editor for those.

**VS Code** — code-server (VS Code in the browser) through the app's login: it starts by itself (**Stop editor**
frees it; **Open in tab** shows it full-window, where more keyboard shortcuts work). **Install language support**
(once per host) adds C/C++ (clangd: completion, go to definition, references,
rename, diagnostics, clang-tidy; CodeLLDB: debugging) and Python (Python + basedpyright: completion and types;
debugpy: debugging; Ruff). The workspace has the tasks **waw: build** / **waw: test** (Ctrl+Shift+B) and debug
configurations (*Python: current file*, *Python: pytest current file*, *C++: debug program* once you set the program
in **Settings**). Changing the settings updates the open editor.

From VS Code itself (the integration extension; status bar item **Agent**):

- right-click → **Ask the agent…**, **Explain this code**, **Fix problems here**, **Write tests for this code**
  (`Ctrl+Alt+A`, `Ctrl+Alt+F`): the file, the selection, the language and the language servers' diagnostics go
  with the request; the editor saves all files first (setting `waw.saveBeforeAsk`; off: unsaved buffers are sent
  instead);
- the **Fix with the agent** quick fix (light bulb) on errors and warnings;
- *Agent: Implement a change…*, *Verify the branch against the wiki*, *Address the PR's review comments*, *Fix the
  PR's failing CI* in the command palette;
- **AI inline completion** (ghost text, Tab to accept) for C/C++/Python when `[development.completion]` is
  configured; *Agent: Toggle AI inline completion* turns it off.

When the agent is done, a notification offers **Open answer** and **Review changes** (Source Control); files the
agent changed reload in the editor.

### The agent panel

Choose what to do — **Implement**, **Ask**, **Fix**, **Write tests**, **Explain**, **Verify against wiki**, and for a
PR **Address review comments** / **Fix failing CI** — and describe it. Each request is a *task*; *continue task N*
adds to the latest one (the agent keeps its context), otherwise a new task starts. Requests made in the editor appear
here too. The agent searches the wiki before it changes code, follows it (and says where the code or the request
contradicts it), ends with **Wiki references** (the pages it relied on), runs the build and tests, and never commits,
pushes or switches branches — that stays with you. The session's model and effort are chosen at the top.

### Chat about a project, a folder or a file

- **Files** — 💬 on a folder or file of the tree starts a chat about it (Chats page; the root folder's 💬 is the
  whole project); a document's discussions panel is the chat about that file.
- **PR Review** — *Ask about this PR* (the project), *Ask about a folder / file…*, a file's *Ask agent*, or lines.
- **PR Development** — the agent panel's *All* view (the project), 💬 on a folder in the explorer, the *This file*
  view (each file has its own discussions and agent session), or a selection.

The agent gets the skills of the project the folder or file belongs to first (that folder, then its parents up to the
repository root), then those of every open folder and every connected folder: reference folders (⧉ / *+ folders*),
repositories' local checkouts and PR repositories' wikis.

### Skills

Every agent turn — wiki discussions, folder chats, PR tabs and PR Development — gets the **skills of the current
folder and of every connected folder and project**: every folder open in the window (and their project roots, e.g.
the repository a wiki sub-folder belongs to), plus for PR Development the worktree, its source checkout and the
reference wiki, and for a PR tab the PR's connected local checkout. Skills are the `SKILL.md` files (any depth) under
`.opencode/skills`, `.claude/skills`, `.agents/skills` and `.codex/skills`.

- **OpenCode** loads them as its own skills for the turn (`skills.paths` in its configuration for that run).
- **Codex** has no setting for extra skill folders; the turn's prompt lists them — name, description, path of the
  `SKILL.md` — the same way Codex lists its own skills, with the instruction to read a matching one first.
- On an **SSH host** they are copied there read-only (`<work_dir>/skills/…`, refreshed when they change) and loaded
  from the copies.

**N skills** in the discussion panel, the chat header and the PR Development agent panel lists them.

### Terminal, build & test, changes, pull request

- **Terminal** — shells in the worktree (on an SSH host: an ssh login that starts there), as tabs (**+** for
  another). They are tmux sessions: they keep running when the page reloads and are listed in the Terminal view too.

- **Build & test** — run the build, the tests or any command in the worktree; the output streams in; a failed run
  has **Fix with the agent** (the output goes with the request).
- **Changes** — like the wiki's Diff view: every changed file with its own diff (new files included), **Unified** or
  **Side by side**, ↑/↓/↕ and **Expand all** for more context, **Viewed** to fold a file you have checked (the mark
  goes when its change changes). Choose what to compare: **Uncommitted changes** (default), **Everything on this
  branch** or **Committed on this branch** (against where the branch started), one commit, or the worktree against
  another branch. Click a line (drag or shift-click for several) to **Ask agent** about it — the discussion is anchored
  on those lines and the agent gets the diff — or **Open in editor** (double-click a line does the same); **Ask
  agent** in a file's header asks about all of its changes. **Commit…** in its header opens the commit panel at the top, as on
  the wiki's Diff page: tick the files to commit (there or in each file's header; build outputs and caches are
  unticked), **Draft with agent** writes the message from the diff of the ticked files, **Commit**, then **Push** under
  *Share* (to the branch's remote; for a PR, to its head branch; never forced). The branch's commits are listed at
  the bottom of the panel: click one to see its changes. **Diff** in the toolbar (or **⤢ Expand**) shows all the
  changes in the editor area; **±** on a file or folder of the explorer shows only its changes ("only <path>" — × for
  all of them again). **Back to editor** returns the view to the bottom panel.
- **Pull request** — for new work: title and description (**Draft with agent**, **Verify against wiki first**),
  **Create pull request** (pushes first; draft by default). For a PR: its checks, **Address review comments**, **Fix
  failing CI**, **Verify against wiki**.

**⋯ → Archive** stops the editor and hides the workspace; you may also remove its worktree (refused while it has
uncommitted or unpushed work).

## Agents (monitor)

The **Agents** icon of the activity bar watches OpenCode, Codex and Claude Code sessions — also those you started in
a terminal or another tool — and tells you when one needs you.

- **Folders** (sidebar): the wiki folders, plus any local folder you add (**+ add folder**). The sessions found in the
  checked folders (and their sub-folders) are listed; check the ones to monitor.
- **Add a session by its id** when you cannot tell sessions apart by their titles: choose OpenCode, Codex or Claude
  Code, paste the id (`ses_…` / the UUID), optionally give it a name to show.
- **The main page** shows every monitored session with its state: **needs you** (red, first, counted on the Agents
  icon and in the browser tab title; optional browser notification), **working**, **error**, **stalled**, **idle**,
  **not found**. "Needs you" means: a question / approval request is pending, a tool call has been waiting without
  progress (usually a permission prompt in a terminal), or the agent's finished turn ends with a question. It
  refreshes every few seconds; **Refresh all** re-reads everything.
- **Click a session** to put it on stage (80 % of the page): its conversation (tool calls optional), live while it
  runs, and a box to **continue it** — the message runs the provider's CLI on that session in its folder
  (`opencode run --session`, `codex exec resume`, `claude -p --resume`). **Open in terminal** opens the provider's own
  interactive UI on the session in a terminal tab — the place to answer permission prompts.

Sessions are read where the agents keep them: `opencode session list` / `opencode export`, `$CODEX_HOME/sessions`,
`$CLAUDE_CONFIG_DIR/projects` (default `~/.claude`). `[agents.claude] bin` sets the Claude Code executable.

## After a reload or closing the browser

The window comes back as you left it: the folders and document tabs, the active document with its reading
position and open discussion, the **Terminal** or **Diff** view (and which diff), an open **PR tab** with its
*Overview* / *Files* sub-tab, and where long views were scrolled (PR overview and files, the diff, the file tree,
Reviews). Text typed but not sent yet is kept too: replies in the discussions panel, *Ask about this document*,
folder chats and the PR conversation box. This is stored in the browser (per browser and named workspace);
discussions, reading positions and settings are on the server. A closed tab stays closed.

## Reading documents

- **Tabs**: every document you open gets a tab above the reader (like the terminal tabs); opening one that is
  already open switches to it, and a new tab opens right after the current one. Click a tab to switch (also across
  folders); close it with `×` or a middle-click — closing the active tab opens its neighbour. Drag tabs to reorder.
  Tabs are remembered in this browser. Two files with the same name show their parent folder (or the folder name
  when the same path is open in two wikis). Switching away from unsaved edits asks first.
- The tree lists every file with a kind icon (folder, Markdown, image, PDF, Word, PowerPoint, Visio diagram,
  notebook, code, script, data, text, archive, binary; a small arrow marks symlinks, a broken-link icon marks dangling ones). Git status badges show
  `untracked` / `modified` / `clean`.
- The reader opens **Markdown**, **any text or source file** (C/C++, Python, YAML, XML, JSON, shell…, shown as
  numbered code), **Word**, **PowerPoint**, **Visio**, **PDF** (browser viewer) and **images** (click for actual
  size).
- **Word, PowerPoint and Visio** (`.docx`, `.pptx`, `.vsdx`) have two views, switched with **Layout | Text** in
  the document header (your choice is remembered):
  - **Layout** (default) shows the original: pages, slides and diagrams exactly as their authors made them,
    rendered once by LibreOffice on the server and shown in the browser's PDF viewer (zoom, page navigation,
    search). The first opening of a file takes a few seconds; afterwards it opens at once until the file changes.
    **Download the original** fetches the file itself.
  - **Text** shows selectable text — Word as a document, PowerPoint one card per slide (title, bullets, tables,
    pictures, speaker notes), Visio one section per page with its shapes' text. Selecting text, discussions,
    review marks and diff comments work here like on Markdown. **Ask about a passage…** in the Layout view and
    opening a discussion from the panel switch to Text.
  - Older and OpenDocument formats (`.doc`, `.ppt`, `.pps(x)`, `.vsd`, `.rtf`, `.odt`, `.odp`, `.odg`) have the
    Layout view only; like PDFs, discussions on them are about the whole document (**Ask about this document**).
  - Layout needs LibreOffice on the server (see DEPLOYMENT.md); without it the Text view still works.
- Links to sections (`#heading`, `other.md#heading`) scroll inside the reader; heading ids follow GitHub's rules.
- Your reading position is saved when you stop scrolling for a second and restored next time; nothing is marked
  reviewed by scrolling.
- When a file changes on disk (agent, terminal, Git), a banner offers **Refresh**; your position is kept.
- **↻** (file tree, diff view) re-reads from disk. Its icon spins until the refresh is done, at least one full turn.
  Normally not needed: the file watcher keeps the tree up to date. If a folder cannot be listed (server
  unreachable), the tree keeps the earlier listing with a **Retry** link.
- **Refresh one folder**: hover a folder (or a root folder) in the tree and click its **↻**. Only that folder and
  the folders open inside it are re-read. Expanding a folder always re-reads it too.
- Row actions (↻, ±, lock, ✕) appear on hover at the right end of the row, so file names keep their full width;
  a click anywhere else on a row opens the file or expands/collapses the folder.
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

**Asking about text, a page, a slide or a picture (PDF, Word, PowerPoint, Visio).** PDFs and the *Layout* view
of Office files are shown by the app's own viewer, which fills the reading area (zoom with − / Fit / +; *Fit*
fits pages to the width and whole slides to the window):
- **Select text** on a page → **Ask about selection** (the page number goes with the question).
- **Ask about this page** / **Ask about this slide** above each page sends an image of it to the agent.
- **Select area**, then drag a rectangle around a figure, diagram or table → only that part is sent as an image.
- A page with discussions shows *n discussions* (click to open); clicking a page/slide discussion in the panel scrolls to it. The panel
  names them (*📑 Slide 3*, *🔲 Area of page 2*) and shows the attached image.

In the *Text* view, hover a picture for **Ask about this image** and, in PowerPoint, a slide for **Ask about this
slide** (a normal anchored discussion on the whole slide's text). Selecting text works as everywhere.

**Asking about the whole document.** The discussions panel has a question box at the top (*Ask about this
document…*, also in Flat mode; **＋ New question** from an open discussion brings you back to it). Type and press
Enter: the agent reads the whole file. **Ask about this document** in the document header does the same. Works for
every format the reader opens — Markdown, text/source, PDF, Word, PowerPoint, Visio (also in Layout view) and
**images** (PNG, JPEG, GIF, WebP, BMP, AVIF), whose picture is attached to the question so the agent sees it. Such discussions are listed as *📄 whole document*, highlight
nothing, and never become *orphaned* when the file changes; replies stay about the whole document.

## Discussions panel

Every question and answer — in discussions, chats, PR Development tasks, diff comments and the Agents monitor —
shows its date and time in **UTC+8** (e.g. `2026-11-12 15:03:21 UTC+8`): when you asked, and when the answer started.

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
- **Ask agent** on a file's header asks about all of that file's changes (like GitHub's per-file ask): the
  question and the file's diff go to the agent; the discussion appears at the top of the file in the diff and
  in the document's discussions.


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
