/** Small formatters shared by the panel. Pure, so they are tested directly. */

export type Grade = "ok" | "warn" | "bad" | "";

export function fmtUptime(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  const total = Math.max(0, Math.trunc(seconds));
  const h = Math.trunc(total / 3600);
  const m = Math.trunc((total % 3600) / 60);
  const s = total % 60;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}

export function fmtClock(t: number | undefined): string {
  return t ? new Date(t * 1000).toTimeString().slice(0, 8) : "";
}

/** Faster is better: under `okMax` is healthy, over `warnMax` is wrong. */
export function msGrade(ms: number, okMax: number, warnMax: number): Grade {
  return ms <= okMax ? "ok" : ms <= warnMax ? "warn" : "bad";
}

/** Higher is better — match confidence, where low means we nearly missed. */
export function scoreGrade(score: number | null | undefined): Grade {
  if (score == null) return "";
  return score >= 0.9 ? "ok" : score >= 0.8 ? "warn" : "bad";
}

export function latencyGrade(
  ms: number | null | undefined,
  okMax: number,
  warnMax: number,
): Grade {
  return ms == null ? "" : msGrade(ms, okMax, warnMax);
}

export const GRADE_TEXT: Record<Grade, string> = {
  ok: "text-ok",
  warn: "text-warn",
  bad: "text-bad",
  "": "",
};

/** How fresh the last frame is, in the words the rail uses. */
export function freshness(ageMs: number | null | undefined): {
  text: string;
  grade: Grade;
} {
  if (ageMs == null) return { text: "—", grade: "" };
  if (ageMs < 1500) return { text: "Live", grade: "ok" };
  if (ageMs < 5000) return { text: `${ageMs} ms`, grade: "warn" };
  return { text: `Lagging ${(ageMs / 1000).toFixed(1)} s`, grade: "bad" };
}
