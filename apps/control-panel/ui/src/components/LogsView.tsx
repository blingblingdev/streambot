/**
 * Two views of the same machine: the system's turning points as the console
 * narrates them (stream drops, reconnects, IPC silences, worker and job
 * lifecycle), and the worker log's raw tail for when the narration is not
 * enough. The narration exists because the raw log is health snapshots — the
 * moments are only visible as differences between them, and nobody should
 * have to diff JSON by eye.
 */

import { useEffect, useState } from "react";

import { api } from "../api";
import type { SystemEvent } from "../types";
import { fmtClock } from "../lib/format";

const KIND_TONE: Record<string, string> = {
  worker: "text-blue",
  stream: "text-live",
  ipc: "text-warn",
  job: "text-ok",
};

export function LogsView({ workerTail }: { workerTail: string[] }) {
  const [events, setEvents] = useState<SystemEvent[]>([]);

  useEffect(() => {
    let live = true;
    const load = async () => {
      const result = await api.syslog();
      if (live && result.ok === true) setEvents(result.events);
    };
    load();
    const timer = setInterval(load, 2000);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, []);

  return (
    <div className="flex min-h-0 flex-1 gap-4 p-4">
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-[10px] border border-line bg-panel">
        <div className="border-b border-line px-4 py-2.5 text-[11px] tracking-wide text-faint uppercase">
          System events
        </div>
        <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-4 py-2 font-mono text-[12px] leading-[1.8]">
          {events.length === 0 ? (
            <div className="text-faint">
              Nothing yet — turning points (stream drops, reconnects, IPC
              silences, worker and job lifecycle) appear here as they happen.
            </div>
          ) : (
            events.map((event, index) => (
              <div key={`${event.t}-${index}`} className="flex gap-3">
                <span className="shrink-0 text-faint">{fmtClock(event.t)}</span>
                <span
                  className={`w-[52px] shrink-0 text-[10.5px] uppercase ${KIND_TONE[event.kind] ?? "text-muted"}`}
                >
                  {event.kind}
                </span>
                <span className="text-muted">{event.text}</span>
              </div>
            ))
          )}
        </div>
      </div>
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-[10px] border border-line bg-panel">
        <div className="border-b border-line px-4 py-2.5 text-[11px] tracking-wide text-faint uppercase">
          Worker log · tail
        </div>
        <pre className="scroll-thin min-h-0 flex-1 overflow-auto px-4 py-2 font-mono text-[11.5px] leading-[1.7] whitespace-pre-wrap text-muted">
          {workerTail.join("\n") || "—"}
        </pre>
      </div>
    </div>
  );
}
