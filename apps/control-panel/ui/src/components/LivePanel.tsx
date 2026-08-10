/**
 * The live worker frame with the stream's vitals — the worker's eye. It sits
 * at the top of the rail rather than among the charts, because it is about
 * now, not about history; on a narrow screen it joins the stacked cards
 * instead (`fill`), where the grid row provides its height.
 */

import { useEffect, useRef, useState } from "react";

import { api } from "../api";
import type { SceneControl, Status } from "../types";
import { GRADE_TEXT, freshness } from "../lib/format";
import { Led } from "./ui";

const SCENE_SIZE = [1280, 720] as const;
const CADENCES = [
  { label: "1 s", rate: 1000 },
  { label: "1 min", rate: 60_000 },
  { label: "Pause", rate: 0 },
];

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
    // Paused fetches nothing: the PAUSED veil covers the last frame, so a
    // refresh would cost the host a JPEG encode nobody can see.
    if (rate === 0) return;
    let live = true;
    const tick = () => {
      const probe = new Image();
      probe.onload = () => live && setSrc(probe.src);
      probe.onerror = () => live && setSrc(null);
      probe.src = `/api/snapshot?t=${Date.now()}`;
    };
    tick();
    const timer = setInterval(tick, rate);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [rate]);

  // Letterbox to 16:9 inside whatever the panel gives us.
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
        {rate === 0 ? (
          // Veils the stale frame (and swallows clicks on control markers)
          // the moment Pause is selected.
          <div className="absolute inset-0 z-10 flex items-center justify-center bg-black/90">
            <span className="font-mono text-[13px] tracking-[0.35em] text-muted select-none">
              PAUSED
            </span>
          </div>
        ) : null}
      </div>
    </div>
  );
}

export function LivePanel({
  status,
  fill = false,
}: {
  status: Status | null;
  /** In a grid cell the row provides the height; in the rail the 16:9 body does. */
  fill?: boolean;
}) {
  const [rate, setRate] = useState(60_000);
  const scene = status?.scene;
  const connection = status?.connection;
  const fresh = freshness(connection?.frame_age_ms);

  return (
    <div
      className={
        `flex min-h-0 flex-col overflow-hidden border-line bg-panel ` +
        (fill ? "rounded-[10px] border" : "shrink-0 border-b")
      }
    >
      <div className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2 text-[11px] tracking-wide text-faint uppercase">
        Live frame
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
      </div>
      <div className={`relative ${fill ? "min-h-0 flex-1" : "aspect-video"}`}>
        <LiveFrame
          controls={scene?.controls ?? []}
          recommended={scene?.recommended_control_id ?? null}
          rate={rate}
        />
      </div>
      {/* The stream's vitals belong on the stream's own picture: what state
          it is in, how fresh this frame is, how many times it has had to
          reconnect, and what it can see. */}
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
    </div>
  );
}
