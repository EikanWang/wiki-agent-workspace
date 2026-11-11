"""PR Development: agent-native development workspaces (Git worktree + VS Code-compatible editor + wiki reference).

Modules: targets (local / SSH execution), service (workspaces, worktrees, git, PR lifecycle), editor (code-server
lifecycle and the integration extension), proxy (editor HTTP/WebSocket through the app's login), tasks (build / test
runs), agent (prompts grounded in the reference wiki, editor context), completion (AI inline completion), routes.
"""

DEV_PREFIX = ".waw-dev/"


def is_dev_path(rel: str) -> bool:
    return rel.replace("\\", "/").lstrip("/").startswith(DEV_PREFIX)
