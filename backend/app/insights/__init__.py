"""Repo Insights: summaries of what happens in GitHub repositories — issues and pull requests day by day, commits,
RFCs, and XPU highlights — over a day, week, month, quarter, year or any dates; a daily scheduler (off by default).

Folder layout (settings `data_dir`, default <state_dir>/insights):
  summary/<owner>/<repo>/summary_<owner>_<repo>_<N>D_<YYYY-MM-DD>_<HH-MM-SS>.md   first line: "Summary of o/r from A to B:"
  highlight/<owner>/<repo>/highlight_<owner>_<repo>_<YYYY-MM-DD>_<HH-MM-SS>.json   [{"File Information": …}, items…]
  reports/<owner>/<repo>/<id>.md + <id>.json   reports you create
  blocks/<owner>/<repo>/…                       cached building blocks (week roll-ups, commit summaries)
  items/<owner>/<repo>/…                        local copies of issues / PRs / RFCs (as fetched from GitHub)
  repos/<owner>/<repo>.git                      app-owned clones for the commit history (when no checkout is connected)
"""
