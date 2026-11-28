/** Local files paths: `/abs/path` on the server, `<machine>:/abs/path` on a machine of the Machines view (like scp). */
const REMOTE = /^([A-Za-z0-9._-]+):(\/.*)$/;

export function splitLocal(p: string): { machine: string | null; path: string } {
  const m = REMOTE.exec(p);
  return m ? { machine: m[1], path: m[2] } : { machine: null, path: p };
}

export const onMachine = (machine: string | null, path: string) => (machine ? `${machine}:${path}` : path);

/** The folder holding `p` (on the same machine). */
export function parentOfLocal(p: string): string {
  const { machine, path } = splitLocal(p);
  return onMachine(machine, path.slice(0, path.lastIndexOf("/")) || "/");
}
