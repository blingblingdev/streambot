/**
 * The four metric charts (plus, on a narrow screen, the live frame card —
 * on a wide one the frame lives at the top of the rail instead).
 */

import { useEffect, useRef, useState } from "react";

import { api } from "../api";
import type { History, JobMetrics, Status } from "../types";
import { GRADE_TEXT, latencyGrade, scoreGrade, type Grade } from "../lib/format";
import { NARROW_QUERY, useMediaQuery } from "../lib/useMediaQuery";
import { clampWindow, jobColor, panWindow } from "../lib/timerange";
import { Chart, type ChartLine } from "./Charts";
import { LivePanel } from "./LivePanel";
import { TimePicker } from "./TimePicker";
import { Led } from "./ui";

function Card({
  title,
  headline,
  grade = "",
  header,
  footer,
  children,
}: {
  title: string;
  headline?: string;
  grade?: Grade;
  header?: React.ReactNode;
  footer?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="flex min-h-0 flex-col overflow-hidden rounded-[10px] border border-line bg-panel">
      <div className="flex items-center gap-2 border-b border-line px-3 py-2 text-[11px] tracking-wide text-faint uppercase">
        {title}
        {header}
        {headline !== undefined ? (
          <span className={`ml-auto font-mono text-[12.5px] ${GRADE_TEXT[grade]}`}>
            {headline}
          </span>
        ) : null}
      </div>
      <div className="relative min-h-0 flex-1">{children}</div>
      {footer}
    </div>
  );
}

export function Stage({
  status,
  jobNames,
  runningName,
  metrics,
}: {
  status: Status | null;
  jobNames: string[];
  runningName: string | null;
  metrics: JobMetrics | null;
}) {
  const [history, setHistory] = useState<History | null>(null);
  // The chart window: how much history, and whether it follows now (live) or
  // stays where the operator dragged it (pinned).
  const [range, setRange] = useState(3600);
  const [pinnedEnd, setPinnedEnd] = useState<number | null>(null);
  // Every job with data in the window draws as its own line; a chip in the
  // History bar hides one when it is in the way.
  const [hidden, setHidden] = useState<Set<string>>(new Set());

  useEffect(() => {
    let alive = true;
    const load = async () => {
      const result = await api.history(range, pinnedEnd ?? undefined);
      if (alive && result.ok === true) setHistory(result);
    };
    load();
    // Live slides with now; a pinned window is history and history holds still.
    if (pinnedEnd !== null) return () => void (alive = false);
    const timer = setInterval(load, 3000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [range, pinnedEnd]);

  const present = Object.keys(history?.jobs ?? {}).sort();
  const lines = (metric: keyof import("../types").JobSeries): ChartLine[] =>
    present
      .filter((name) => !hidden.has(name))
      .map((name) => ({
        name,
        color: jobColor(name, jobNames),
        values: history?.jobs[name]?.[metric] ?? [],
      }));

  // A drag lands as percentages of the shown window; map it back to absolute
  // time and ask the server for that window. Debounced, because a drag is a
  // stream of gestures and only where it settles matters.
  const shownRef = useRef<History | null>(null);
  shownRef.current = history;
  const panTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // A shift-drag selection arrives in absolute seconds and zooms straight in.
  const onSelect = (startSec: number, endSec: number) => {
    const landed = clampWindow(startSec, endSec, Math.floor(Date.now() / 1000));
    setRange(landed.end - landed.start);
    setPinnedEnd(landed.live ? null : landed.end);
  };

  const onPan = (startPct: number, endPct: number) => {
    if (panTimer.current) clearTimeout(panTimer.current);
    panTimer.current = setTimeout(() => {
      const shown = shownRef.current;
      if (!shown) return;
      if (Math.abs(startPct) < 0.5 && Math.abs(endPct - 100) < 0.5) return;
      const now = Math.floor(Date.now() / 1000);
      const landed = panWindow(shown.start, shown.end, startPct, endPct, now);
      setRange(landed.end - landed.start);
      setPinnedEnd(landed.live ? null : landed.end);
    }, 350);
  };

  const narrow = useMediaQuery(NARROW_QUERY);
  const columns = narrow ? 1 : 2;

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-1.5 text-[11px]">
        <span className="tracking-wide text-faint uppercase">History</span>
        {present.map((name) => (
          <button
            key={name}
            onClick={() =>
              setHidden((held) => {
                const next = new Set(held);
                if (next.has(name)) next.delete(name);
                else next.add(name);
                return next;
              })
            }
            title={hidden.has(name) ? "Show this job" : "Hide this job"}
            className={
              `flex cursor-pointer items-center gap-1.5 rounded-full border border-line ` +
              `px-2 py-0.5 font-mono text-[10px] ` +
              (hidden.has(name) ? "text-faint opacity-50" : "text-muted")
            }
          >
            <span
              className="size-[7px] rounded-full"
              style={{ background: jobColor(name, jobNames) }}
            />
            {name}
            {name === runningName ? <Led grade="ok" /> : null}
          </button>
        ))}
        {/* Fixed on the right, so the legend growing on the left never moves
            the time controls out from under the pointer. */}
        <div className="ml-auto flex shrink-0 items-center gap-2">
          {pinnedEnd !== null ? (
            <button
              onClick={() => setPinnedEnd(null)}
              className="flex cursor-pointer items-center gap-1.5 rounded-md border border-warn/45 bg-warn/10 px-2 py-1 font-mono text-[10.5px] text-warn"
              title="Viewing history — click to follow now again"
            >
              resume live
            </button>
          ) : (
            <span className="flex items-center gap-1.5 font-mono text-[10.5px] text-live">
              <span className="size-[6px] animate-pulse rounded-full bg-live" />
              live
            </span>
          )}
          <TimePicker
            range={range}
            pinnedEnd={pinnedEnd}
            onApply={(rangeSeconds, end) => {
              setRange(rangeSeconds);
              setPinnedEnd(end);
            }}
          />
        </div>
      </div>
      <div
        className={
          `grid min-h-0 flex-1 gap-2.5 p-2.5 ` +
          (narrow ? "scroll-thin overflow-y-auto" : "")
        }
        style={{
          gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))`,
          // Stacked, the cards cannot share the height; give each its own
          // and let the column scroll.
          ...(narrow ? { gridAutoRows: "260px" } : {}),
        }}
      >
        {narrow ? <LivePanel status={status} fill /> : null}
        <Card
          title="Capture → Detect"
          headline={metrics?.perceive_ms != null ? `${metrics.perceive_ms} ms` : "—"}
          grade={latencyGrade(metrics?.perceive_ms, 300, 600)}
        >
          <Chart
            lines={lines("perceive")}
            window={history}
            onPan={onPan}
            onSelect={onSelect}
            format={(value) => `${Math.round(value)} ms`}
          />
        </Card>
        <Card
          title="Locate control"
          headline={metrics?.resolve_ms != null ? `${metrics.resolve_ms} ms` : "—"}
          grade={latencyGrade(metrics?.resolve_ms, 150, 400)}
        >
          <Chart
            lines={lines("resolve")}
            window={history}
            onPan={onPan}
            onSelect={onSelect}
            format={(value) => `${Math.round(value)} ms`}
          />
        </Card>
        <Card
          title="Click confidence"
          headline={
            (metrics?.last_score ?? metrics?.mean_score) != null
              ? (metrics!.last_score ?? metrics!.mean_score)!.toFixed(2)
              : "—"
          }
          grade={scoreGrade(metrics?.last_score ?? metrics?.mean_score)}
        >
          <Chart
            lines={lines("score")}
            window={history}
            onPan={onPan}
            onSelect={onSelect}
            format={(value) => value.toFixed(2)}
          />
        </Card>
        <Card
          title="Clicks / min"
          headline={metrics?.clicks_per_min != null ? String(metrics.clicks_per_min) : "—"}
          grade="ok"
        >
          <Chart
            lines={lines("cpm")}
            window={history}
            onPan={onPan}
            onSelect={onSelect}
            format={(value) => `${value} /min`}
          />
        </Card>
      </div>
    </div>
  );
}
