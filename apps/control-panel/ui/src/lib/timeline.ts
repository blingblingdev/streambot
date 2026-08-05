/**
 * What one step of a job is made of, and how the feed is bounded.
 *
 * A poll is four things: the IPC round trip to the worker, the worker deciding
 * which screen this is, the worker locating the control, and — when it acts —
 * the click. `perceive_ms` is the whole round trip as the job measured it, and
 * the worker's own `classify_ms`/`resolve_ms` sit inside it, so what is left
 * over is transport.
 */

import type { FlowEvent } from "../types";

/** The browser accumulates rows for as long as a job runs; this is the guard. */
export const FEED_MAX_ROWS = 250;

export interface Phase {
  key: "tp" | "cl" | "lo" | "ac";
  label: string;
  ms: number;
  color: string;
}

export const PHASE_COLORS = {
  tp: "#3d444d",
  cl: "#2dd4bf",
  lo: "#a78bfa",
  ac: "#3fb950",
} as const;

export function phases(event: FlowEvent): Phase[] {
  const total = event.perceive_ms;
  if (total == null) return [];
  const classify = event.classify_ms ?? 0;
  const locate = event.resolve_ms ?? 0;
  const act = event.act_ms ?? 0;
  // Clamped, because a clock that disagrees with itself must not draw a
  // negative segment.
  const transport = Math.max(0, total - classify - locate);
  const all: Phase[] = [
    { key: "tp", label: "transport", ms: transport, color: PHASE_COLORS.tp },
    { key: "cl", label: "classify", ms: classify, color: PHASE_COLORS.cl },
    { key: "lo", label: "locate", ms: locate, color: PHASE_COLORS.lo },
    { key: "ac", label: "click", ms: act, color: PHASE_COLORS.ac },
  ];
  return all.filter((phase) => phase.ms > 0 || phase.key === "tp");
}

/** End to end: the round trip plus the click it led to. */
export function totalMs(event: FlowEvent): number | null {
  if (event.perceive_ms == null) return null;
  return event.perceive_ms + (event.act_ms ?? 0);
}

export function hasTiming(event: FlowEvent): boolean {
  return event.perceive_ms != null;
}

/**
 * Append what is new and keep the list bounded.
 *
 * The server stamps each line with a monotonic `i`, so "new" is anything above
 * the highest we hold. A restarted job starts its numbering again, which is
 * why a lower `i` than we have means: this is a different session, start over.
 */
export function mergeEvents(
  held: FlowEvent[],
  incoming: FlowEvent[],
  max = FEED_MAX_ROWS,
): FlowEvent[] {
  if (!incoming.length) return held;
  const highest = held.length ? held[held.length - 1]!.i : 0;
  const last = incoming[incoming.length - 1]!;
  if (held.length && last.i < highest) return incoming.slice(-max);
  const fresh = incoming.filter((event) => event.i > highest);
  if (!fresh.length) return held;
  return [...held, ...fresh].slice(-max);
}

/**
 * Which job the feed follows: the running one, else whichever stopped job the
 * server still serves events for (it picks the most recently active log). A
 * job ending therefore does not blank the feed — the worker outlives its
 * jobs, and so does what streambot recorded through them.
 */
export function feedSource<T extends { running: boolean; events: unknown[] }>(
  jobs: T[],
): T | null {
  return (
    jobs.find((job) => job.running) ??
    jobs.find((job) => job.events.length > 0) ??
    null
  );
}

export interface Placement {
  left: number;
  top: number;
}

/**
 * Where the breakdown goes: beside the pointer, flipped and clamped so it is
 * never partly off screen.
 */
export function placePopover(
  mouse: { clientX: number; clientY: number },
  size: { width: number; height: number },
  viewport: { width: number; height: number },
  margin = 8,
): Placement {
  let left = mouse.clientX + 14;
  if (left + size.width > viewport.width - margin) {
    left = mouse.clientX - size.width - 14;
  }
  if (left < margin) left = margin;
  let top = mouse.clientY - 10;
  if (top + size.height > viewport.height - margin) {
    top = viewport.height - size.height - margin;
  }
  if (top < margin) top = margin;
  return { left, top };
}
