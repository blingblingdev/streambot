import { useEffect, useMemo, useState } from "react";

import { api, fetchOnce, isShotMode, useEventStream } from "./api";
import type { FlowEvent, JobRow, StreamPayload } from "./types";
import { fmtUptime, latencyGrade, scoreGrade } from "./lib/format";
import { mergeEvents } from "./lib/timeline";
import { Button, Cell, Led, Section } from "./components/ui";
import { Timeline } from "./components/Timeline";
import { Stage } from "./components/Stage";
import { JobsDrawer } from "./components/JobsDrawer";
import { SettingsDialog } from "./components/SettingsDialog";
import { LogsView } from "./components/LogsView";

/** Why the worker is not simply running, in the worker's own words. Never
 *  guessed from process state: the old heuristic showed a frightening
 *  permission error whenever the worker was merely stopped. */
const BANNERS: Record<string, string> = {
  permission_blocked:
    "This launch environment cannot reach the local network, so the system blocked the worker. Start it from a terminal that holds local-network permission.",
  waiting_desktop_session:
    "No active Desktop session on the host; the worker is waiting and will connect automatically once it recovers.",
  host_busy:
    "Another application owns the host streaming session; the worker will not displace it and connects automatically when that session ends.",
  waiting_host:
    "The Sunshine host is not visible right now (asleep or offline); the worker is waiting and connects as soon as it reappears.",
  failed:
    "The worker stopped after repeated reconnect failures. Check last_error on the Logs tab, fix the cause, and start it again.",
  detached:
    'Stream disconnected by operator: the worker is alive but holds no host connection. Use "Connect stream" to reattach.',
};

function useToast() {
  const [message, setMessage] = useState<string | null>(null);
  useEffect(() => {
    if (!message) return;
    const timer = setTimeout(() => setMessage(null), 2200);
    return () => clearTimeout(timer);
  }, [message]);
  return [message, setMessage] as const;
}

export function App() {
  const shot = isShotMode();
  const { payload: streamed } = useEventStream(!shot);
  const [once, setOnce] = useState<StreamPayload | null>(null);
  const [toast, setToast] = useToast();
  const [tab, setTab] = useState<"dash" | "logs">("dash");
  const [drawer, setDrawer] = useState(false);
  const [settingsFor, setSettingsFor] = useState<JobRow | null>(null);
  const [events, setEvents] = useState<FlowEvent[]>([]);
  const [feedJob, setFeedJob] = useState<string | null>(null);

  useEffect(() => {
    if (shot) fetchOnce().then(setOnce);
  }, [shot]);

  const payload = streamed ?? once;
  const status = payload?.status ?? null;
  const jobs = useMemo(() => payload?.jobs ?? [], [payload]);
  const running = jobs.find((job) => job.running) ?? null;
  const connection = status?.connection;
  const worker = status?.worker;
  const metrics = running?.metrics ?? null;

  // The feed belongs to one job; a different one starts it over.
  useEffect(() => {
    const name = running?.name ?? null;
    if (name !== feedJob) {
      setFeedJob(name);
      setEvents(name ? (running?.events ?? []) : []);
      return;
    }
    if (running) setEvents((held) => mergeEvents(held, running.events));
  }, [running, feedJob]);

  const streaming = connection?.state === "observing" || connection?.state === "acting";
  const detached = connection?.state === "detached";
  const banner =
    (status && BANNERS[status.situation]) ||
    (connection?.ipc_error && worker?.pid != null
      ? `The worker is not responding over IPC (${connection.ipc_error}). Check the Logs tab for details or restart the worker.`
      : null);

  const act = async (
    call: () => Promise<{ ok: boolean; error?: string }>,
    good: string,
    bad: string,
  ) => {
    const result = await call();
    setToast(result.ok ? good : `${bad}: ${result.error ?? "error"}`);
  };

  return (
    <div className="flex h-full flex-col bg-bg text-text">
      <header className="flex h-[54px] shrink-0 items-center gap-3 border-b border-line bg-panel px-4">
        <div className="flex items-center gap-2 text-[13px] font-semibold tracking-[.14em]">
          <Led grade={connection?.state === "observing" ? "ok" : worker?.pid != null ? "warn" : ""} />
          STREAMBOT
          <span className="text-[11px] font-normal tracking-normal text-faint">console</span>
        </div>
        <div className="flex items-center gap-2 rounded-md border border-line bg-panel2 px-2.5 py-1 text-[11.5px]">
          <span className="text-faint">worker</span>
          <span className="font-mono">
            {worker?.pid != null
              ? `${connection?.state ?? "connected"} · pid ${worker.pid}`
              : worker?.socket_present
                ? "connected (external)"
                : "stopped"}
          </span>
        </div>
        <div className="flex-1" />
        <div className="flex overflow-hidden rounded-md border border-line text-[12px]">
          {(["dash", "logs"] as const).map((view) => (
            <button
              key={view}
              onClick={() => setTab(view)}
              className={
                `cursor-pointer px-3 py-1.5 ` +
                (tab === view ? "bg-blue/20 text-blue" : "text-muted hover:text-text")
              }
            >
              {view === "dash" ? "Dashboard" : "Logs"}
            </button>
          ))}
        </div>
        <Button small onClick={() => setDrawer((open) => !open)}>
          Jobs{" "}
          <span className="font-mono">
            {jobs.length ? `${jobs.filter((job) => job.running).length}/${jobs.length}` : "0"}
          </span>
        </Button>
        {worker?.socket_present && streaming ? (
          <Button
            onClick={() =>
              act(api.disconnectWorker, "Stream disconnecting", "Disconnect failed")
            }
          >
            Disconnect stream
          </Button>
        ) : null}
        {worker?.socket_present && detached ? (
          <Button onClick={() => act(api.connectWorker, "Stream connecting", "Connect failed")}>
            Connect stream
          </Button>
        ) : null}
        <Button
          kind="primary"
          disabled={worker?.pid != null || worker?.socket_present}
          onClick={() => act(api.startWorker, "Worker starting", "Start failed")}
        >
          Start worker
        </Button>
        <Button
          kind="danger"
          disabled={worker?.pid == null}
          onClick={() => act(api.stopWorker, "Worker stopped", "Stop failed")}
        >
          Stop worker
        </Button>
      </header>

      {banner ? (
        <div className="border-b border-warn/40 bg-warn/10 px-4 py-2.5 text-[12.5px] text-[#f0d9a8]">
          {banner}
        </div>
      ) : null}

      {tab === "logs" ? (
        <LogsView workerTail={status?.log_tail ?? []} />
      ) : (
        <div className="flex min-h-0 flex-1">
          <aside className="flex w-rail shrink-0 flex-col overflow-hidden border-r border-line bg-panel">
            <Section
              title="Running"
              className={running ? "" : "opacity-60"}
            >
              <div className="mb-2 flex items-center gap-2">
                <div className="min-w-0 flex-1 truncate text-[13px] font-medium">
                  {running ? running.title || running.name : "None"}
                </div>
                {running?.configurable ? (
                  <Button small onClick={() => setSettingsFor(running)}>
                    Settings
                  </Button>
                ) : null}
                {running ? (
                  <Button
                    small
                    kind="danger"
                    onClick={() =>
                      act(
                        () => api.stopJob(running.name),
                        `Stopped ${running.name}`,
                        "Stop failed",
                      )
                    }
                  >
                    Stop job
                  </Button>
                ) : null}
              </div>
              <div className="mb-2.5 flex items-center gap-2 text-[12px] text-muted">
                <Led grade={running ? "ok" : ""} />
                {running
                  ? `Running · pid ${running.pid}`
                  : "Idle — start one from the Jobs panel"}
              </div>
              <div className="grid grid-cols-3 gap-1.5">
                <Cell label="Uptime" value={fmtUptime(metrics?.uptime_s)} />
                <Cell label="Cycles" value={metrics?.cycles ?? "—"} />
                <Cell label="Total clicks" value={metrics?.clicks_total ?? "—"} />
                <Cell label="Clicks / min" value={metrics?.clicks_per_min ?? "—"} />
                <Cell
                  label="Confidence"
                  value={metrics?.mean_score != null ? metrics.mean_score.toFixed(2) : "—"}
                  grade={scoreGrade(metrics?.mean_score)}
                />
                <Cell
                  label="Errors (60s)"
                  value={metrics?.errors_recent ?? "—"}
                  grade={
                    metrics?.errors_recent
                      ? metrics.errors_recent > 3
                        ? "bad"
                        : "warn"
                      : "ok"
                  }
                />
                <Cell
                  label="Capture → Detect"
                  value={metrics?.perceive_ms ?? "—"}
                  unit={metrics?.perceive_ms != null ? "ms" : undefined}
                  grade={latencyGrade(metrics?.perceive_ms, 300, 600)}
                />
                <Cell
                  label="Locate control"
                  value={metrics?.resolve_ms ?? "—"}
                  unit={metrics?.resolve_ms != null ? "ms" : undefined}
                  grade={latencyGrade(metrics?.resolve_ms, 150, 400)}
                />
                <Cell
                  label="Detect → Click"
                  value={metrics?.act_ms ?? "—"}
                  unit={metrics?.act_ms != null ? "ms" : undefined}
                  grade={latencyGrade(metrics?.act_ms, 400, 800)}
                />
              </div>
            </Section>

            <section className="flex min-h-0 flex-1 flex-col">
              <h2 className="flex items-center gap-2 px-3.5 pt-3 pb-1.5 text-[10.5px] font-semibold tracking-[.09em] text-faint uppercase">
                Timeline
                <span className="flex items-center gap-2.5 text-[9.5px] font-normal tracking-normal">
                  {[
                    ["#3d444d", "transport"],
                    ["#2dd4bf", "classify"],
                    ["#a78bfa", "locate"],
                    ["#3fb950", "click"],
                  ].map(([color, name]) => (
                    <span key={name} className="flex items-center gap-1">
                      <i
                        className="block size-[7px] rounded-sm"
                        style={{ background: color }}
                      />
                      {name}
                    </span>
                  ))}
                </span>
              </h2>
              <Timeline events={events} />
            </section>
          </aside>

          <Stage status={status} jobName={running?.name ?? null} metrics={metrics} />
        </div>
      )}

      <JobsDrawer
        open={drawer}
        jobs={jobs}
        onClose={() => setDrawer(false)}
        onSettings={(job) => setSettingsFor(job)}
        onToast={setToast}
      />
      {settingsFor ? (
        <SettingsDialog
          job={settingsFor}
          onClose={() => setSettingsFor(null)}
          onToast={setToast}
        />
      ) : null}
      {toast ? (
        <div className="fixed bottom-5 left-1/2 z-40 -translate-x-1/2 rounded-[9px] border border-line bg-panel2 px-4 py-2.5 text-[13px]">
          {toast}
        </div>
      ) : null}
    </div>
  );
}
