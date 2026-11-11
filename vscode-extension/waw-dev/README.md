# Wiki Agent Workspace — PR Development

Installed automatically into the editor that Wiki Agent Workspace starts for a development workspace. It connects
the editor to the app's agent:

- **Agent: Ask / Explain / Fix / Write tests / Implement** (editor context menu, `Ctrl+Alt+A`, `Ctrl+Alt+F`): sends
  the current file, selection, language and diagnostics; the agent reads the reference wiki first and works in the
  worktree. Answers appear in a notification, the **Agent** output channel and the app's agent panel.
- **Fix with the agent** quick fix on errors and warnings.
- **Verify the branch against the wiki**, **Address review comments**, **Fix failing CI**.
- **AI inline completion** for C/C++/Python when the server has `[development.completion]` configured.

It only works inside an editor started by the app (it needs `WAW_DEV_ID` / `WAW_DEV_TOKEN`).
