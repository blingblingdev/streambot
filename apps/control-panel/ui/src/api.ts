/** Every call the console makes to its own server. */

import { useEffect, useRef, useState } from "react";
import type {
  ApiResult,
  History,
  JobConfig,
  JobRow,
  Status,
  StreamPayload,
} from "./types";

async function post<T = { ok: true }>(
  path: string,
  body?: unknown,
): Promise<ApiResult<T>> {
  try {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    return (await response.json()) as ApiResult<T>;
  } catch (error) {
    return { ok: false, error: (error as Error).name || "NetworkError" };
  }
}

async function get<T>(path: string): Promise<ApiResult<T>> {
  try {
    const response = await fetch(path);
    return (await response.json()) as ApiResult<T>;
  } catch (error) {
    return { ok: false, error: (error as Error).name || "NetworkError" };
  }
}

export const api = {
  status: () => get<Status>("/api/status"),
  jobs: () => get<{ ok: true; jobs: JobRow[] }>("/api/jobs"),
  history: (name: string) =>
    get<History>(`/api/jobs/history?name=${encodeURIComponent(name)}`),
  jobConfig: (name: string) =>
    get<JobConfig>(`/api/jobs/config?name=${encodeURIComponent(name)}`),

  startWorker: () => post<{ ok: true; pid: number }>("/api/worker/start"),
  stopWorker: () => post("/api/worker/stop"),
  connectWorker: () => post("/api/worker/connect"),
  disconnectWorker: () => post("/api/worker/disconnect"),

  startJob: (name: string) =>
    post<{ ok: true; pid: number }>("/api/jobs/start", { name }),
  stopJob: (name: string) => post("/api/jobs/stop", { name }),
  setJobConfig: (name: string, values: Record<string, unknown>) =>
    post<JobConfig>("/api/jobs/config", { name, values }),

  setAutomation: (enabled: boolean) => post("/api/automation", { enabled }),
  dispatch: (control_id: string) => post("/api/dispatch", { control_id }),
};

/**
 * The once-a-second push of status + jobs.
 *
 * Held in a ref and opened once: the server dedicates a thread to each
 * connection for as long as it lives, so a second EventSource — which is
 * exactly what React's development double-mount would create — would leave a
 * 1 Hz thread running against a page nobody is watching.
 */
export function useEventStream(enabled = true): {
  payload: StreamPayload | null;
  connected: boolean;
} {
  const [payload, setPayload] = useState<StreamPayload | null>(null);
  const [connected, setConnected] = useState(false);
  const source = useRef<EventSource | null>(null);

  useEffect(() => {
    if (!enabled) return;
    if (source.current) return;
    const stream = new EventSource("/api/events");
    source.current = stream;
    stream.onmessage = (event) => {
      try {
        setPayload(JSON.parse(event.data) as StreamPayload);
        setConnected(true);
      } catch {
        /* one malformed frame must never break the stream */
      }
    };
    stream.onerror = () => setConnected(false);
    return () => {
      stream.close();
      source.current = null;
    };
  }, [enabled]);

  return { payload, connected };
}

/**
 * The one-shot mode: `?shot=1` renders a fully-populated page with no stream
 * and no timers, so a headless browser can screenshot it without racing a
 * live feed or waiting forever for an EventSource to go idle.
 */
export function isShotMode(): boolean {
  return typeof location !== "undefined" && location.search.includes("shot");
}

export async function fetchOnce(): Promise<StreamPayload | null> {
  const [status, jobs] = await Promise.all([api.status(), api.jobs()]);
  if (status.ok !== true || jobs.ok !== true) return null;
  return { status, jobs: jobs.jobs };
}
