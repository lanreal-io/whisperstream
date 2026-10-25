/**
 * Format a millisecond duration as a short relative-time string for the
 * Updates settings row. No date library; no localization.
 *
 * Boundaries:
 *   < 60_000 ms          -> "just now"
 *   < 60 minutes         -> "N min ago"
 *   < 24 hours           -> "1 hour ago" / "N hours ago"
 *   >= 24 hours          -> "1 day ago" / "N days ago"
 *
 * Note: a "yesterday" case is intentionally NOT included — it reads too
 * colloquial for settings copy.
 */
export function formatRelative(ms: number): string {
  if (ms < 60_000) return "just now";
  const min = Math.floor(ms / 60_000);
  if (min < 60) return `${min} min ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr} hour${hr === 1 ? "" : "s"} ago`;
  const d = Math.floor(hr / 24);
  return `${d} day${d === 1 ? "" : "s"} ago`;
}
