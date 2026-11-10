/** GitHub's URL for an account: users and organizations at github.com/<login>, apps ("name[bot]") at github.com/apps/<name>. */
export function githubProfileUrl(login: string): string {
  const bot = login.match(/^(.+)\[bot\]$/);
  return bot ? `https://github.com/apps/${encodeURIComponent(bot[1])}` : `https://github.com/${encodeURIComponent(login)}`;
}

/**
 * A GitHub login that opens the account's page (new tab), like on github.com. `inButton`: inside a clickable row
 * (a link cannot sit inside a <button>), so it is a span that opens the page and keeps the row from reacting.
 */
export default function GhUser({ login, bold = true, inButton = false }: { login: string | null | undefined; bold?: boolean; inButton?: boolean }) {
  if (!login) return <>?</>;
  const url = githubProfileUrl(login);
  const text = bold ? <b>{login}</b> : <>{login}</>;
  if (inButton) {
    return (
      <span className="gh-user" role="link" tabIndex={-1} title={`Open ${login} on GitHub`} data-testid="gh-user" data-login={login}
        onClick={(e) => { e.stopPropagation(); e.preventDefault(); window.open(url, "_blank", "noopener,noreferrer"); }}>
        {text}
      </span>
    );
  }
  return <a className="gh-user" href={url} target="_blank" rel="noopener noreferrer" title={`Open ${login} on GitHub`} data-testid="gh-user" data-login={login}>{text}</a>;
}
