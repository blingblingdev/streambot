/**
 * What the job is doing, as a timeline.
 *
 * Each event is a dot on a spine. Anything timed also draws the phases it was
 * made of, proportionally, so "where did the time go" is answerable without
 * reading four numbers — and clicking it opens those numbers in a panel that
 * floats rather than pushing the list around.
 */

import { useEffect, useLayoutEffect, useRef, useState } from "react";

import type { FlowEvent } from "../types";
import { fmtClock, msGrade } from "../lib/format";
import {
  hasTiming,
  phases,
  placePopover,
  totalMs,
  type Placement,
} from "../lib/timeline";
import { Pill } from "./ui";

const ROW_TONE: Record<string, string> = {
  click: "bg-ok/8 border-l-ok text-text",
  cycle: "bg-blue/8 border-l-blue text-[#bcd9ff]",
  start: "bg-live/8 border-l-live text-[#9ff0e3]",
  "config-changed": "bg-live/8 border-l-live text-[#9ff0e3]",
  done: "bg-bad/8 border-l-bad text-[#ffc4b8]",
  stopped: "bg-bad/8 border-l-bad text-[#ffc4b8]",
  warn: "bg-warn/8 border-l-warn text-[#f0d9a8]",
};

const WARN_KINDS = new Set([
  "frame-skip",
  "click-skip",
  "poll-error",
  "classify-skip",
  "job-error",
]);

const DOT_TONE: Record<string, string> = {
  click: "bg-ok",
  cycle: "bg-blue",
  start: "bg-live",
  "config-changed": "bg-live",
  done: "bg-bad",
  stopped: "bg-bad",
  warn: "bg-bad",
  perceive: "bg-faint",
};

function toneOf(event: FlowEvent): string {
  if (WARN_KINDS.has(event.event)) return "warn";
  return event.event;
}

function PhaseBar({ event }: { event: FlowEvent }) {
  const parts = phases(event);
  const end = totalMs(event);
  if (!parts.length || end == null) return null;
  return (
    <>
      <span className="flex h-[7px] w-[62px] shrink-0 gap-px overflow-hidden rounded-sm">
        {parts.map((phase) => (
          <i
            key={phase.key}
            className="block min-w-px"
            style={{ flex: phase.ms, background: phase.color }}
          />
        ))}
      </span>
      <span
        className={`w-[34px] shrink-0 text-right tabular-nums text-${msGrade(Math.round(end), 300, 800)}`}
      >
        {Math.round(end)}
      </span>
    </>
  );
}

function label(event: FlowEvent) {
  switch (event.event) {
    case "perceive":
      return event.screen || "unknown";
    case "click":
      return <span className="font-semibold">{event.element ?? "?"}</span>;
    case "cycle":
      return `✓ cycle ${event.completed ?? "?"} complete`;
    case "start":
      return "▶ job started";
    case "done":
    case "stopped":
      return `■ ${event.event === "done" ? "finished" : "stopped"}: ${event.reason ?? ""}${
        event.clicks != null ? ` · ${event.clicks} clicks` : ""
      }`;
    case "doing":
      return `${event.what ?? "working"}${
        event.wait_s != null
          ? ` ${event.wait_s}s`
          : event.until_s != null
            ? ` ${event.until_s}s left`
            : ""
      }`;
    case "config-changed":
      return `⚙ ${Object.entries(event.changed ?? {})
        .map(([key, value]) => `${key}=${String(value)}`)
        .join(" ")}`;
    default:
      if (WARN_KINDS.has(event.event)) {
        return `⚠ ${event.what ?? event.event}${event.error ? ` · ${event.error}` : ""}`;
      }
      return event.event;
  }
}

function Breakdown({ event, at }: { event: FlowEvent; at: Placement }) {
  const parts = phases(event);
  const end = totalMs(event) ?? 0;
  const context = [
    event.screen,
    event.element,
    event.center ? `${event.center[0]},${event.center[1]}` : null,
    event.score != null ? `score ${event.score}` : null,
  ].filter(Boolean);
  return (
    <div
      className="fixed z-55 max-w-[280px] min-w-[200px] rounded-lg border border-line bg-panel px-[11px] py-[9px] font-mono text-[11px] text-muted shadow-[0_12px_32px_rgba(0,0,0,.5)]"
      style={{ left: at.left, top: at.top }}
      onClick={(mouse) => mouse.stopPropagation()}
    >
      {parts.map((phase) => (
        <div key={phase.key} className="flex items-center gap-2 py-0.5">
          <i
            className="size-2 shrink-0 rounded-sm"
            style={{ background: phase.color }}
          />
          <span className="flex-1">{phase.label}</span>
          <span className="tabular-nums text-text">{phase.ms.toFixed(1)} ms</span>
        </div>
      ))}
      <div className="mt-1.5 flex items-center gap-2 border-t border-line pt-1.5">
        <i className="size-2 shrink-0" />
        <span className="flex-1 font-medium text-text">end to end</span>
        <span className="font-medium tabular-nums text-text">{end.toFixed(1)} ms</span>
      </div>
      {context.length ? (
        <div className="mt-1.5 text-[10.5px] leading-relaxed break-words text-faint">
          {context.join(" · ")}
        </div>
      ) : null}
    </div>
  );
}

export function Timeline({ events }: { events: FlowEvent[] }) {
  const box = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const [open, setOpen] = useState<{ i: number; at: Placement } | null>(null);

  // Follow new events unless the operator has scrolled up to read something.
  useLayoutEffect(() => {
    const element = box.current;
    if (element && follow.current) element.scrollTop = element.scrollHeight;
  }, [events]);

  useEffect(() => {
    const dismiss = () => setOpen(null);
    const escape = (key: KeyboardEvent) => key.key === "Escape" && setOpen(null);
    document.addEventListener("click", dismiss);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("click", dismiss);
      document.removeEventListener("keydown", escape);
    };
  }, []);

  // The breakdown belongs to a line; if that line has scrolled out of the
  // feed's history it is describing something no longer there.
  const selected = open ? events.find((event) => event.i === open.i) : undefined;

  return (
    <div
      ref={box}
      onScroll={() => {
        const element = box.current;
        if (!element) return;
        follow.current =
          element.scrollHeight - element.scrollTop - element.clientHeight < 40;
      }}
      className="timeline scroll-thin relative min-h-0 flex-1 overflow-y-auto overscroll-contain px-2.5 pt-0.5 pb-3"
    >
      {events.length === 0 ? (
        <div className="text-[13px] text-faint">
          Perceive and click events scroll here once a job is running.
        </div>
      ) : (
        events.map((event) => {
          const tone = toneOf(event);
          const timed = hasTiming(event);
          return (
            <div
              key={event.i}
              onClick={(mouse) => {
                if (!timed) return;
                mouse.stopPropagation();
                if (open?.i === event.i) return setOpen(null);
                setOpen({
                  i: event.i,
                  at: placePopover(
                    mouse,
                    { width: 240, height: 150 },
                    { width: window.innerWidth, height: window.innerHeight },
                  ),
                });
              }}
              className={
                `my-px flex items-baseline gap-2 rounded border-l-2 border-transparent ` +
                `px-2 py-0.5 font-mono text-[11.5px] leading-[1.55] text-muted ` +
                (ROW_TONE[tone] ?? "") +
                (timed ? " cursor-pointer hover:bg-white/4" : "") +
                (open?.i === event.i ? " bg-white/7" : "")
              }
            >
              <span
                className={`size-[7px] shrink-0 rounded-full shadow-[0_0_0_3px_var(--color-panel)] ${DOT_TONE[tone] ?? "bg-line"}`}
              />
              <span className="shrink-0 text-[10.5px] text-faint">
                {fmtClock(event.t)}
              </span>
              <span className="min-w-0 flex-1 truncate">{label(event)}</span>
              {event.event === "click" && event.score != null ? (
                <Pill grade={event.score >= 0.9 ? "ok" : event.score >= 0.8 ? "warn" : "bad"}>
                  {event.score.toFixed(2)}
                </Pill>
              ) : null}
              {timed ? <PhaseBar event={event} /> : null}
            </div>
          );
        })
      )}
      {selected && open ? <Breakdown event={selected} at={open.at} /> : null}
    </div>
  );
}
