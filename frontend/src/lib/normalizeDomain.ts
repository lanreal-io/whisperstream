/**
 * Strip scheme/www/path/port, lowercase — UI-layer mirror of the backend
 * `extract_registrable_domain` rule (browser_domain.py).
 *
 * Matching is HOST-ONLY: `mail.google.com` stays `mail.google.com` and is its
 * own site, separate from `google.com`. Only leading `www.` is stripped; no
 * eTLD+1 reduction is performed (that would require a public-suffix-list
 * dependency and would incorrectly fold `news.ycombinator.com` -> `ycombinator.com`).
 *
 * The backend is authoritative on save; this mirrors it for instant validation
 * and display in the UI.
 *
 * Returns null if:
 * - the input is empty / whitespace-only
 * - the input contains interior whitespace (search text / omnibox guard)
 * - the result has no dot (bare word, e.g. `localhost`)
 */
export function normalizeDomain(raw: string): string | null {
  let s = raw.trim().toLowerCase();
  if (!s) return null;
  // Reject search text / bare words with interior whitespace (omnibox guard)
  if (/\s/.test(s)) return null;
  s = s.replace(/^[a-z]+:\/\//, "");       // strip scheme (http://, https://, etc.)
  s = s.split("/")[0].split("?")[0].split("#")[0]; // strip path / query / fragment
  s = s.split(":")[0];                     // strip :port
  s = s.replace(/^www\./, "");             // strip leading www.
  if (!s.includes(".")) return null;       // no dot -> not a valid host (omnibox guard)
  return s;
}
