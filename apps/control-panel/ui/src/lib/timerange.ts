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
  { label: "30m", seconds: 30 * 60 },
  { label: "1h", seconds: 3600 },
  { label: "6h", seconds: 6 * 3600 },
  { label: "24h", seconds: 24 * 3600 },
  { label: "3d", seconds: 3 * 24 * 3600 },
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

export interface Window {
  start: number;
  end: number;
  live: boolean;
}

/**
 * Clamp any requested window to one that can exist: the future does not exist
 * yet, history ends at the retention horizon, and a window can never collapse
 * below a minute. Landing against now reads as "follow now again".
 */
export function clampWindow(start: number, end: number, now: number): Window {
  start = Math.round(start);
  end = Math.round(end);
  if (end < start) [start, end] = [end, start];
  if (end - start < 60) end = start + 60;
  if (end > now) {
    start -= end - now;
    end = now;
  }
  const oldest = now - MAX_RANGE_SECONDS;
  if (start < oldest) start = oldest;
  if (end - start < 60) end = start + 60;
  return { start, end, live: now - end < 10 };
}

/**
 * Where a pan/zoom gesture lands, in absolute seconds. The chart reports the
 * gesture as percentages of the window it was showing.
 */
export function panWindow(
  shownStart: number,
  shownEnd: number,
  startPct: number,
  endPct: number,
  now: number,
): Window {
  const width = shownEnd - shownStart;
  return clampWindow(
    shownStart + (startPct / 100) * width,
    shownStart + (endPct / 100) * width,
    now,
  );
}

/** "Last 1h", or the span in the largest unit that reads cleanly. */
export function humanizeRange(seconds: number): string {
  const preset = RANGE_PRESETS.find((p) => p.seconds === seconds);
  if (preset) return preset.label;
  if (seconds % 86400 === 0) return `${seconds / 86400}d`;
  if (seconds % 3600 === 0) return `${seconds / 3600}h`;
  if (seconds >= 3600) return `${(seconds / 3600).toFixed(1)}h`;
  return `${Math.max(1, Math.round(seconds / 60))}m`;
}

function stamp(t: number, withDate: boolean): string {
  const d = new Date(t * 1000);
  const hm = d.toTimeString().slice(0, 5);
  if (!withDate) return hm;
  return `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${hm}`;
}

/** What the picker button says: a relative label live, absolute when pinned. */
export function windowLabel(
  rangeSeconds: number,
  pinnedEnd: number | null,
  now: number,
): string {
  if (pinnedEnd === null) return `Last ${humanizeRange(rangeSeconds)}`;
  const start = pinnedEnd - rangeSeconds;
  const days = new Date(start * 1000).toDateString() !== new Date(pinnedEnd * 1000).toDateString()
    || new Date(start * 1000).toDateString() !== new Date(now * 1000).toDateString();
  return `${stamp(start, days)} → ${stamp(pinnedEnd, days)}`;
}
