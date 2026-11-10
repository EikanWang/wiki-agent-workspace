import { useEffect, useRef, useSyncExternalStore } from "react";

/**
 * In-app confirmation for actions that are easy to trigger by mistake (closing a folder, hiding a GitHub repository,
 * archiving a chat): a dialog with an explicit button naming the action. Cancel has the focus, so Enter or a stray
 * click does not confirm; Escape or a click outside cancels.
 */
interface Request { title: string; message: string; confirmLabel: string; danger: boolean; resolve: (ok: boolean) => void }

let current: Request | null = null;
const subs = new Set<() => void>();
const emit = () => subs.forEach((f) => f());

export function confirmAction(opts: { title: string; message: string; confirmLabel: string; danger?: boolean }): Promise<boolean> {
  current?.resolve(false);
  return new Promise((resolve) => {
    current = { danger: true, ...opts, resolve: (ok) => { current = null; emit(); resolve(ok); } };
    emit();
  });
}

export function ConfirmHost() {
  const req = useSyncExternalStore((f) => { subs.add(f); return () => { subs.delete(f); }; }, () => current);
  const cancel = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!req) return;
    cancel.current?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") req.resolve(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [req]);
  if (!req) return null;
  return (
    <div className="confirm-backdrop" onClick={() => req.resolve(false)}>
      <div className="confirm-box" role="alertdialog" aria-modal="true" aria-label={req.title} onClick={(e) => e.stopPropagation()} data-testid="confirm-dialog">
        <div className="confirm-title">{req.title}</div>
        <div className="confirm-message">{req.message}</div>
        <div className="row confirm-buttons">
          <span className="spacer" />
          <button ref={cancel} className="btn" onClick={() => req.resolve(false)} data-testid="confirm-cancel">Cancel</button>
          <button className={`btn ${req.danger ? "danger-solid" : "primary"}`} onClick={() => req.resolve(true)} data-testid="confirm-ok">{req.confirmLabel}</button>
        </div>
      </div>
    </div>
  );
}
