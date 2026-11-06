import type { KeyboardEvent } from "react";

/**
 * Composer key handling used by every question/reply box:
 *  - Enter            → send
 *  - Ctrl/⌘ + Enter   → insert a new paragraph (newline) at the caret
 *  - Shift + Enter    → newline as well (common habit)
 *  - Enter during IME composition (Chinese/Japanese input) → never sends
 *  - Escape           → optional cancel
 */
export function composerKeyDown(
  e: KeyboardEvent<HTMLTextAreaElement>,
  send: () => void,
  setValue: (v: string) => void,
  onEscape?: () => void,
) {
  if (e.key === "Escape" && onEscape) {
    onEscape();
    return;
  }
  if (e.key !== "Enter") return;
  if (e.nativeEvent.isComposing || (e.nativeEvent as any).keyCode === 229) return;
  if (e.ctrlKey || e.metaKey || e.shiftKey) {
    e.preventDefault();
    const ta = e.currentTarget;
    const { selectionStart: s, selectionEnd: en, value } = ta;
    const next = value.slice(0, s) + "\n" + value.slice(en);
    setValue(next);
    requestAnimationFrame(() => {
      ta.selectionStart = ta.selectionEnd = s + 1;
    });
    return;
  }
  e.preventDefault();
  send();
}

export const COMPOSER_HINT = "Enter to send · Ctrl/⌘+Enter for a new paragraph";
