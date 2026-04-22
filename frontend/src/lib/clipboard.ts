/**
 * Copy text to clipboard with a graceful fallback.
 *
 * The modern `navigator.clipboard.writeText` API is only available in a
 * "secure context" — HTTPS pages or `http://localhost`. This tool is served
 * over `http://192.168.200.109:8091` (intranet), so browsers treat it as
 * insecure and `navigator.clipboard` is `undefined`. We fall back to the
 * legacy `document.execCommand('copy')` which works in insecure contexts.
 *
 * Returns true on success, false otherwise.
 */
export async function writeClipboard(text: string): Promise<boolean> {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // fall through to the legacy path
    }
  }

  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "0";
    ta.style.left = "0";
    ta.style.opacity = "0";
    ta.style.pointerEvents = "none";
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, text.length);
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}
