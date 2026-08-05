/**
 * The chart window: a span of history, either live (sliding with now) or
 * pinned where the operator panned to.
 */

export interface RangePreset {
  label: string;
  seconds: number;
}

export const RANGE_PRESETS: RangePreset[] = [
  { label: "15m", seconds: 15 * 60 },
  { label: "1h", seconds: 3600 },
  { label: "6h", seconds: 6 * 3600 },
  { label: "24h", seconds: 24 * 3600 },
  { label: "7d", seconds: 7 * 24 * 3600 },
  { label: "30d", seconds: 30 * 24 * 3600 },
];

export const MAX_RANGE_SECONDS = 30 * 24 * 3600;

/**
 * One colour per job, stable across requests: assigned from the full job
 * list (alphabetical, which the registry already is), not from whichever
 * jobs happen to have data in the current window — so a job keeps its
 * colour when the window moves.
 */
export const JOB_COLORS = [
  "#2dd4bf",
  "#4c9aff",
  "#a78bfa",
  "#e3b341",
  "#f0553f",
  "#3fb950",
  "#ff7eb6",
  "#7ee3fd",
];

export function jobColor(name: string, allJobs: string[]): string {
  const index = allJobs.indexOf(name);
  return JOB_COLORS[
    (index >= 0 ? index : allJobs.length) % JOB_COLORS.length
  ]!;
}

/**
 * Where a pan/zoom gesture lands, in absolute seconds.
 *
 * The chart reports the gesture as percentages of the window it was showing;
 * this maps them back onto that window and clamps: the future does not exist
 * yet, history ends at the retention horizon, and a window can never collapse
 * below a minute.
 */
export function panWindow(
  shownStart: number,
  shownEnd: number,
  startPct: number,
  endPct: number,
  now: number,
): { start: number; end: number; live: boolean } {
  const width = shownEnd - shownStart;
  let start = Math.round(shownStart + (startPct / 100) * width);
  let end = Math.round(shownStart + (endPct / 100) * width);
  if (end - start < 60) end = start + 60;
  if (end > now) {
    start -= end - now;
    end = now;
  }
  const oldest = now - MAX_RANGE_SECONDS;
  if (start < oldest) start = oldest;
  if (end - start < 60) end = start + 60;
  // Panned right up against now reads as "follow now again".
  return { start, end, live: now - end < 10 };
}
