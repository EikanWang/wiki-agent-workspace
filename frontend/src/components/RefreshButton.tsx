import { useEffect, useRef, useState } from "react";

/**
 * ↻ button that spins while `onRefresh` runs. The icon always completes at least one full turn and stops at the
 * end of a turn (never snapping back mid-rotation), so even an instant refresh gives visible feedback.
 */
export default function RefreshButton({
  onRefresh,
  className = "icon-btn",
  title = "Refresh",
  testId,
}: {
  onRefresh: () => Promise<unknown> | void;
  className?: string;
  title?: string;
  testId?: string;
}) {
  const [spinning, setSpinning] = useState(false);
  const pending = useRef(0); // refreshes still running (clicking again while spinning is allowed)
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);

  const click = async () => {
    pending.current += 1;
    setSpinning(true);
    try {
      await onRefresh();
    } catch {
      /* the caller shows its own error */
    } finally {
      pending.current -= 1;
    }
  };

  // stop only at the end of a turn, once nothing is pending any more
  const onIteration = () => {
    if (pending.current === 0 && alive.current) setSpinning(false);
  };

  return (
    <button className={`${className} refresh-btn`} onClick={() => void click()} title={title} aria-label={title} aria-busy={spinning} data-testid={testId} data-spinning={spinning ? "true" : "false"}>
      <span className={`refresh-icon ${spinning ? "spinning" : ""}`} onAnimationIteration={onIteration} aria-hidden="true">↻</span>
    </button>
  );
}
