// frontend/src/lib/format.ts

const MEBIBYTE = 1024 ** 2;
const GIBIBYTE = 1024 ** 3;

/**
 * On-disk size of a model artifact, at the granularity a person reads off
 * Windows Explorer: binary units, whole MB below 1 GB and one decimal above.
 *
 *   629145600 -> "600 MB"    1610612736 -> "1.5 GB"
 *
 * Rounds a nonzero size UP to "1 MB" rather than reporting "0 MB", because a
 * directory that still holds bytes is exactly what the cleanup action offers
 * to reclaim. Non-finite and negative inputs read as zero instead of throwing:
 * this is a display helper for a number the backend computed, and a bad value
 * should not take the storage row down with it.
 */
export function formatModelBytes(bytes: number): string {
  const safe = Number.isFinite(bytes) && bytes > 0 ? bytes : 0;
  if (safe >= GIBIBYTE) return `${(safe / GIBIBYTE).toFixed(1)} GB`;
  if (safe === 0) return "0 MB";
  return `${Math.max(1, Math.round(safe / MEBIBYTE))} MB`;
}
