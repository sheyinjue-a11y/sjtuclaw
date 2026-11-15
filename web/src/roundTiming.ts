export function formatRoundDuration(durationMs: number | null): string | null {
  if (durationMs === null || durationMs < 1_000) return null;
  return String(Math.floor(durationMs / 1_000)) + "s";
}
