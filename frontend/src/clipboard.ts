/**
 * Write text to the clipboard. Uses the async Clipboard API where the page is a secure context (https / localhost);
 * on plain http (e.g. http://<lan-ip>:8765) falls back to execCommand("copy") on a hidden textarea, which works
 * shortly after a user gesture. Returns false when neither worked so the caller can offer a manual copy.
 */
export async function writeClipboard(text: string): Promise<boolean> {
  if (window.isSecureContext && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      /* fall through */
    }
  }
  const prev = document.activeElement as HTMLElement | null;
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.position = "fixed";
  ta.style.top = "0";
  ta.style.left = "0";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  ta.remove();
  prev?.focus?.();
  return ok;
}

/** Decode a base64 payload as UTF-8 (OSC 52 carries the clipboard text this way). */
export function decodeBase64Utf8(b64: string): string | null {
  try {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new TextDecoder().decode(bytes);
  } catch {
    return null;
  }
}
