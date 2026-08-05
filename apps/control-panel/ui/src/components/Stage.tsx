/**
 * The live frame and the four charts.
 *
 * The cards tile the stage: the column count is whichever comes closest to
 * 16:9 cells, so however many cards there are they fill the space with no dead
 * band underneath, and the frame is letterboxed inside its card rather than
 * stretched.
 */

import { useEffect, useRef, useState } from "react";

import { api } from "../api";
import type { History, JobMetrics, SceneControl, Status } from "../types";
import {
  GRADE_TEXT,
  freshness,
  latencyGrade,
  scoreGrade,
  type Grade,
} from "../lib/format";
import { NARROW_QUERY, useMediaQuery } from "../lib/useMediaQuery";
import { RANGE_PRESETS, jobColor, panWindow } from "../lib/timerange";
import { fmtClock } from "../lib/format";
import { Chart, type ChartLine } from "./Charts";
import { Led } from "./ui";

const SCENE_SIZE = [1280, 720] as const;
const AUTO_CADENCE = 60_000;
const CADENCES = [
  { label: "1 s", rate: 1000 },
  { label: "1 min", rate: 60_000 },
  { label: "Pause", rate: 0 },
];

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

function LiveFrame({
  controls,
  recommended,
  rate,
}: {
  controls: SceneControl[];
  recommended: string | null;
  rate: number;
}) {
  const [src, setSrc] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });

  useEffect(() => {
    let live = true;
    const tick = () => {
      const probe = new Image();
      probe.onload = () => live && setSrc(probe.src);
      probe.onerror = () => live && setSrc(null);
      probe.src = `/api/snapshot?t=${Date.now()}`;
    };
    tick();
    // Paused still refreshes once a minute, so the view never goes stale
    // enough to mislead.
    const timer = setInterval(tick, rate > 0 ? rate : AUTO_CADENCE);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [rate]);

  // Letterbox to 16:9 inside whatever the card gives us.
  useEffect(() => {
    const element = box.current;
    if (!element) return;
    const measure = () => {
      const width = Math.min(element.clientWidth, (element.clientHeight * 16) / 9);
      setSize({ width: Math.floor(width), height: Math.floor((width * 9) / 16) });
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  return (
    <div ref={box} className="absolute inset-0 flex items-center justify-center p-2">
      <div
        className="relative overflow-hidden rounded-md bg-black"
        style={{ width: size.width || undefined, height: size.height || undefined }}
      >
        {src ? (
          <>
            <img src={src} alt="Live worker frame" className="block h-full w-full" />
            {controls.map((control, index) =>
              control.x == null || control.y == null ? null : (
                <button
                  key={control.control_id ?? index}
                  title={`Dispatch ${control.control_id ?? ""}`}
                  onClick={() =>
                    control.control_id && api.dispatch(control.control_id)
                  }
                  style={{
                    left: `${(control.x / SCENE_SIZE[0]) * 100}%`,
                    top: `${(control.y / SCENE_SIZE[1]) * 100}%`,
                  }}
                  className="absolute -translate-x-1/2 -translate-y-1/2 cursor-pointer"
                >
                  <span
                    className={
                      `block size-2.5 rounded-full border-2 ` +
                      (control.control_id === recommended
                        ? "border-live bg-live/40 shadow-[0_0_10px_rgba(45,212,191,.8)]"
                        : "border-blue bg-blue/30")
                    }
                  />
                  <span className="mt-0.5 block rounded bg-black/70 px-1 font-mono text-[9.5px] whitespace-nowrap text-text">
                    {control.label || control.control_id}
                  </span>
                </button>
              ),
            )}
          </>
        ) : (
          <div className="flex h-full w-full items-center justify-center px-6 text-center text-[12px] leading-relaxed text-faint">
            The live frame appears once the worker connects.
            <br />
            Detected controls overlay the frame; click a marker to dispatch it.
          </div>
        )}
      </div>
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
  const [rate, setRate] = useState(60_000);
  const [history, setHistory] = useState<History | null>(null);
  // The chart window: how much history, and whether it follows now (live) or
  // stays where the operator dragged it (pinned).
  const [range, setRange] = useState(3600);
  const [pinnedEnd, setPinnedEnd] = useState<number | null>(null);
  // Every job with data in the window draws as its own line; a chip in the
  // History bar hides one when it is in the way.
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const scene = status?.scene;

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
  const cards = 5;
  const columns = narrow ? 1 : cards <= 2 ? cards : Math.ceil(Math.sqrt(cards));
  const connection = status?.connection;
  const fresh = freshness(connection?.frame_age_ms);

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
        <div className="flex overflow-hidden rounded-md border border-line">
          {RANGE_PRESETS.map((preset) => (
            <button
              key={preset.label}
              onClick={() => {
                setRange(preset.seconds);
                setPinnedEnd(null);
              }}
              className={
                `cursor-pointer px-2 py-0.5 font-mono text-[10.5px] ` +
                (range === preset.seconds && pinnedEnd === null
                  ? "bg-blue/20 text-blue"
                  : "text-muted hover:text-text")
              }
            >
              {preset.label}
            </button>
          ))}
        </div>
        <span className="text-faint">drag charts to pan · wheel to zoom</span>
        <div className="flex-1" />
        {pinnedEnd !== null ? (
          <button
            onClick={() => setPinnedEnd(null)}
            className="flex cursor-pointer items-center gap-1.5 rounded-md border border-warn/45 bg-warn/10 px-2 py-0.5 font-mono text-[10.5px] text-warn"
            title="Viewing history — click to follow now again"
          >
            paused @ {fmtClock(pinnedEnd)} · resume live
          </button>
        ) : (
          <span className="flex items-center gap-1.5 font-mono text-[10.5px] text-live">
            <span className="size-[6px] animate-pulse rounded-full bg-live" />
            live
          </span>
        )}
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
        <Card
          title="Live frame"
          header={
            <>
              <span className="ml-1 font-mono normal-case text-muted">
                {scene?.primary_layout || "—"}
              </span>
              {scene?.primary_layout != null ? (
                <span
                  className={
                    `rounded-full border px-1.5 py-px text-[9.5px] ` +
                    (scene.actionable
                      ? "border-ok/45 bg-ok/10 text-ok"
                      : "border-line text-muted")
                  }
                >
                  {scene.actionable ? "Actionable" : "Observing"}
                </span>
              ) : null}
              <div className="ml-auto flex overflow-hidden rounded-md border border-line normal-case">
                {CADENCES.map((cadence) => (
                  <button
                    key={cadence.rate}
                    onClick={() => setRate(cadence.rate)}
                    className={
                      `cursor-pointer px-2 py-0.5 font-mono text-[10.5px] ` +
                      (rate === cadence.rate
                        ? "bg-blue/20 text-blue"
                        : "text-muted hover:text-text")
                    }
                  >
                    {cadence.label}
                  </button>
                ))}
              </div>
            </>
          }
          footer={
            // The stream's vitals belong on the stream's own picture: what
            // state it is in, how fresh this frame is, how many times it has
            // had to reconnect, and what it can see.
            <div className="flex flex-wrap items-center gap-x-4 gap-y-0.5 border-t border-line px-3 py-1.5 font-mono text-[11px] text-muted">
              <span className="flex items-center gap-1.5">
                <Led
                  grade={
                    connection?.state === "observing" || connection?.state === "acting"
                      ? "ok"
                      : connection?.state
                        ? "warn"
                        : ""
                  }
                />
                {connection?.state ?? "stopped"}
              </span>
              <span className={GRADE_TEXT[fresh.grade]}>{fresh.text}</span>
              <span className="ml-auto">
                reconnects <span className="text-text">{connection?.reconnects ?? "—"}</span>
              </span>
              <span>
                controls <span className="text-text">{scene?.controls.length ?? 0}</span>
              </span>
            </div>
          }
        >
          <LiveFrame
            controls={scene?.controls ?? []}
            recommended={scene?.recommended_control_id ?? null}
            rate={rate}
          />
        </Card>
        <Card
          title="Capture → Detect"
          headline={metrics?.perceive_ms != null ? `${metrics.perceive_ms} ms` : "—"}
          grade={latencyGrade(metrics?.perceive_ms, 300, 600)}
        >
          <Chart
            lines={lines("perceive")}
            window={history}
            onPan={onPan}
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
            format={(value) => `${value} /min`}
          />
        </Card>
      </div>
    </div>
  );
}
