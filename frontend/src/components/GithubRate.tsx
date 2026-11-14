import { useEffect, useState } from "react";
import { get } from "../api";

/** GitHub's API quota as the app sees it: shown only when it is not plain RUNNING (throttled, waiting for the
 * rate limit, resuming, failed), with what is affected and when it goes on. */
interface Bucket { limit: number; remaining: number; reset_at: string | null }
interface Rate {
  state: "RUNNING" | "THROTTLED" | "WAITING_FOR_RATE_LIMIT" | "RESUMING" | "FAILED";
  buckets: Record<string, Bucket>; waiting: { op: string; resource: string; why: string; until_at: string }[];
  next_retry_at: string | null; failed: { why: string } | null; queued: { kind: string }[];
}
const LABEL: Record<string, string> = { THROTTLED: "GitHub: slowed down", WAITING_FOR_RATE_LIMIT: "GitHub: waiting for the rate limit", RESUMING: "GitHub: resuming", FAILED: "GitHub: not working" };
const hhmm = (iso: string | null) => (iso ? new Date(new Date(iso).getTime() + 8 * 3600_000).toISOString().slice(11, 16) : "");

export default function GithubRate() {
  const [r, setR] = useState<Rate | null>(null);
  useEffect(() => {
    let alive = true;
    const load = () => void get<Rate>("/github/rate").then((x) => alive && setR(x)).catch(() => undefined);
    load();
    const t = setInterval(load, 30_000);
    return () => { alive = false; clearInterval(t); };
  }, []);
  if (!r || r.state === "RUNNING") return null;
  const main = ["core", "graphql", "search"].filter((k) => r.buckets[k]).map((k) => `${k} ${r.buckets[k].remaining}/${r.buckets[k].limit}`).join(" · ");
  const why = r.failed?.why ?? r.waiting[0]?.why ?? "";
  const title = [why, main && `left: ${main}`, r.waiting.length ? `waiting: ${r.waiting.map((w) => w.op).join(", ")}` : "",
    r.queued.length ? `${r.queued.length} queued job(s) resume by themselves` : ""].filter(Boolean).join("\n");
  return (
    <span className={`gh-rate ${r.state === "FAILED" ? "bad" : "warn"}`} title={title} data-testid="gh-rate" data-state={r.state}>
      {LABEL[r.state]}{r.next_retry_at ? ` · goes on at ${hhmm(r.next_retry_at)}` : ""}
    </span>
  );
}
