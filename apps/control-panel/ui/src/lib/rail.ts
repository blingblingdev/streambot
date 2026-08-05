/**
 * The rail's width is the operator's to set: the timeline lives there, and how
 * much of it you want depends on the screen and the job. Dragged at the
 * divider, remembered across reloads, double-click to go back to the default.
 */

export const RAIL_DEFAULT = 340;
export const RAIL_MIN = 240;
export const RAIL_MAX = 640;

const STORAGE_KEY = "railWidth";

export function clampRailWidth(width: number): number {
  if (!Number.isFinite(width)) return RAIL_DEFAULT;
  return Math.min(RAIL_MAX, Math.max(RAIL_MIN, Math.round(width)));
}

export function loadRailWidth(): number {
  try {
    const stored = Number(localStorage.getItem(STORAGE_KEY));
    return stored > 0 ? clampRailWidth(stored) : RAIL_DEFAULT;
  } catch {
    return RAIL_DEFAULT;
  }
}

export function storeRailWidth(width: number): void {
  try {
    localStorage.setItem(STORAGE_KEY, String(clampRailWidth(width)));
  } catch {
    /* private mode: the width just resets next visit */
  }
}
