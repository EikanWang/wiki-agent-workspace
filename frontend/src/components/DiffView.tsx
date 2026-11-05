export default function DiffView({ unified }: { unified: string }) {
  const lines = unified.split("\n");
  return (
    <pre className="diff" data-testid="diff-view">
      {lines.map((l, i) => {
        let cls = "ctx";
        if (l.startsWith("+++") || l.startsWith("---")) cls = "file";
        else if (l.startsWith("@@")) cls = "hunk";
        else if (l.startsWith("+")) cls = "add";
        else if (l.startsWith("-")) cls = "del";
        return (
          <div key={i} className={`diff-line ${cls}`}>
            {l || " "}
          </div>
        );
      })}
    </pre>
  );
}
